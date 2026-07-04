# --- SABSE PEHLE AUTO HEALER INITIALIZE HOGA ---
import os
import sys
try:
    from src.utils.auto_healer import setup_auto_healer, register_async_exception_handler
    setup_auto_healer()
    print("✅ AI Auto-Healer successfully initialize ho gaya hai!")
except Exception as e:
    print(f"⚠️ Auto-Healer initialize nahi ho paya (Ya file missing hai): {e}")
# -----------------------------------------------

import re
import uvicorn
import structlog
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response, status, Header
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    TypeHandler,
    ApplicationHandlerStop,
    filters,
)

from src.config.settings import settings
from src.database.connection import init_db_schema
from src.telegram.commands import start_command, help_command, clear_command, newproject_command
from src.telegram.handlers import core_message_handler, do_command_handler
from src.telegram.admin_commands import (
    upgrade_command_handler,
    restart_command_handler,
    addlink_command_handler,
    list_links_command_handler,
    sync_website_command_handler,
)
from src.telegram.scraper_commands import getlinks_command_handler
from src.services.website_sync_service import sync_website_links
from src.telegram.middleware import run_global_middleware, subscription_recheck_callback
from src.services.render_service import trigger_manual_deploy
from src.services.keepalive_service import self_ping

logger = structlog.get_logger(__name__)
scheduler = AsyncIOScheduler()


async def _global_middleware_entry(update: Update, context):
    """group=-1 par register hota hai — rate-limit + onboarding + force-subscribe gate."""
    ok = await run_global_middleware(update, context)
    if not ok:
        raise ApplicationHandlerStop


async def _scheduled_restart_job():
    """
    Har AUTO_RESTART_MINUTES par process ko gracefully terminate karta hai.
    Render (ya entrypoint.sh supervisor) exit dekh kar process ko turant
    wapas launch kar deta hai — stale connections/memory ko fresh kar deta hai.
    """
    logger.warning("Scheduled auto-restart triggered", interval_minutes=settings.AUTO_RESTART_MINUTES)
    import os
    os._exit(1)


async def _scheduled_keepalive_job():
    await self_ping()


async def _scheduled_deploy_check_job():
    """Optional: agar owner ne is job ko use karna chaha (RENDER_API_KEY set hone par)."""
    await trigger_manual_deploy()


def sanitize_secret_token(raw_token: str) -> str:
    """
    Telegram sirf A-Z, a-z, 0-9, '_', '-' allow karta hai secret_token mein.
    Mobile copy-paste se aksar invisible characters (spaces, newlines, smart-quotes,
    zero-width chars) chale aate hain jo screen par dikhte nahi. Yeh function
    unhe automatically strip/filter kar deta hai taaki bot crash na ho.
    """
    cleaned = raw_token.strip()
    allowed_pattern = re.compile(r"[^A-Za-z0-9_\-]")
    invalid_chars = allowed_pattern.findall(cleaned)

    if invalid_chars:
        logger.warning(
            "WEBHOOK_SECRET_TOKEN mein invalid/hidden characters mile — auto-removing.",
            invalid_chars_found=repr(invalid_chars),
            original_length=len(raw_token),
        )
        cleaned = allowed_pattern.sub("", cleaned)

    logger.info(
        "Secret token sanitized.",
        final_length=len(cleaned),
        final_token_repr=repr(cleaned),
    )
    return cleaned


telegram_app = (
    Application.builder()
    .token(settings.TELEGRAM_BOT_TOKEN.get_secret_value())
    .build()
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Ab actual uvloop chal raha hai — yahan async exception handler register karein
        try:
            register_async_exception_handler()
        except NameError:
            pass  # agar auto_healer import hi fail hua tha, silently skip

        # group=-1: sabse pehle chalta hai — rate-limit, DB onboarding, force-subscribe gate
        telegram_app.add_handler(TypeHandler(Update, _global_middleware_entry), group=-1)
        telegram_app.add_handler(CallbackQueryHandler(subscription_recheck_callback, pattern="^recheck_subscription$"))

        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("clear", clear_command))
        telegram_app.add_handler(CommandHandler("newproject", newproject_command))
        # NOTE: Telegram commands lowercase hone chahiye (BotFather bhi yehi enforce karta hai)
        telegram_app.add_handler(CommandHandler("do", do_command_handler))
        telegram_app.add_handler(CommandHandler("upgrade", upgrade_command_handler))
        telegram_app.add_handler(CommandHandler("restart", restart_command_handler))
        telegram_app.add_handler(CommandHandler("addlink", addlink_command_handler))
        telegram_app.add_handler(CommandHandler("links", list_links_command_handler))
        telegram_app.add_handler(CommandHandler("getlinks", getlinks_command_handler))
        telegram_app.add_handler(CommandHandler("syncwebsite", sync_website_command_handler))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_message_handler))

        await telegram_app.initialize()

        # Missing tables (jaise LinkAsset) ko auto-create karta hai — no Alembic in this project
        await init_db_schema()

        # Startup par agar WEBSITE_URL set hai to background me automatically
        # scan karke uske saare download links database me store kar dega.
        # Non-blocking hai — bot startup me isse koi delay nahi hoga.
        if settings.WEBSITE_URL:
            async def _startup_website_sync():
                try:
                    added = await sync_website_links(settings.WEBSITE_URL, added_by=settings.ADMIN_TELEGRAM_ID)
                    logger.info("Startup website sync complete", new_links_added=added)
                except Exception as e:
                    logger.warning("Startup website sync failed (bot chalta rahega)", error=str(e))

            telegram_app.create_task(_startup_website_sync())

        # WEBHOOK_URL bhi clean kar lein (trailing space/slash jaisi mobile-copy-paste dikkatein)
        webhook_base = settings.WEBHOOK_URL.strip().rstrip("/")
        webhook_target = f"{webhook_base}/webhook"
        logger.info("Connecting Telegram Webhook...", url=webhook_target)

        clean_secret = sanitize_secret_token(settings.WEBHOOK_SECRET_TOKEN.get_secret_value())
        app.state.webhook_secret = clean_secret  # webhook_handler mein reuse karne ke liye

        await telegram_app.bot.set_webhook(
            url=webhook_target,
            secret_token=clean_secret
        )
        logger.info("Telegram Webhook connection online successfully!")

        # --- Background schedulers ---
        if settings.AUTO_RESTART_ENABLED:
            scheduler.add_job(
                _scheduled_restart_job, "interval",
                minutes=settings.AUTO_RESTART_MINUTES, id="auto_restart", replace_existing=True,
            )
            logger.info("Auto-restart scheduled", every_minutes=settings.AUTO_RESTART_MINUTES)

        if settings.KEEPALIVE_ENABLED:
            scheduler.add_job(
                _scheduled_keepalive_job, "interval",
                minutes=settings.KEEPALIVE_INTERVAL_MINUTES, id="keepalive", replace_existing=True,
            )
            logger.info("Keepalive self-ping scheduled", every_minutes=settings.KEEPALIVE_INTERVAL_MINUTES)

        scheduler.start()

        yield
    except Exception as init_error:
        logger.critical("Lifespan startup engine failed to initialize!", error=str(init_error))
        sys.exit(1)
    finally:
        logger.info("Cleaning up webhook active routes...")
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        await telegram_app.bot.delete_webhook()
        await telegram_app.shutdown()
        logger.info("Downstream network shutdown complete.")

app = FastAPI(
    title="Telegram AI Coding Gateway",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/", status_code=status.HTTP_200_OK)
@app.head("/", status_code=status.HTTP_200_OK)
async def root():
    """
    Render (aur baaki external monitors/uptime-checkers) default root path '/'
    par HEAD/GET ping karte hain. Pehle koi route define nahi tha yahan, isliye
    logs me har baar '404 Not Found' aata tha — harmless tha, lekin logs saaf
    rakhne aur monitoring tools ko sahi 200 OK milta rahe, isliye ye route add
    kiya gaya hai.
    """
    return {"status": "ok", "service": "coderbot", "environment": settings.ENVIRONMENT}


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    return {"status": "healthy", "environment": settings.ENVIRONMENT}

@app.post("/webhook")
async def webhook_handler(request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    # Sanitized secret use karein (wahi jo set_webhook mein Telegram ko diya gaya tha)
    secret = getattr(app.state, "webhook_secret", None) or sanitize_secret_token(
        settings.WEBHOOK_SECRET_TOKEN.get_secret_value()
    )

    if x_telegram_bot_api_secret_token != secret:
        logger.warning("Unverified request blocked! Token mismatch.")
        # FIX: 'HTTP_403_FORBIDGEN' typo tha (sahi: HTTP_403_FORBIDDEN)
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as webhook_error:
        logger.error("Error receiving incoming update payload", error=str(webhook_error))
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

if __name__ == "__main__":
    # Render 'PORT' env variable provide karta hai; local run ke liye fallback 10000
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,  # production mein reload=True mat rakhein (extra process overhead)
        # --- IMPORTANT: uvloop DISABLE kiya gaya hai ---
        # requirements.txt me uvloop kahi seedha mention nahi hai, lekin FastAPI ke
        # transitive dependencies (fastapi-cli/uvicorn[standard]) ke through wo
        # install ho jaata hai. uvicorn ka default `loop="auto"` uvloop ko hi pick
        # kar leta hai agar wo installed ho. Problem ye hai ki uvloop ki apni
        # `Loop` class Python ke standard `asyncio.base_events.BaseEventLoop` se
        # inherit NAHI karti — isliye humara `getaddrinfo` DNS-fix monkeypatch
        # (jo connection.py me hai) uvloop par bilkul asar nahi karta tha, aur
        # Supabase DB connect karte waqt consistently "[Errno -2] Name or service
        # not known" (gaierror) aata rehta tha, chahe hostname ho ya IP.
        # Standard asyncio loop force karne se ye poori tarah avoid ho jaata hai,
        # kyunki humara monkeypatch (aur main-thread DNS resolution jo hamesha
        # reliably kaam karta hai) sirf asyncio ke standard event loop par hi
        # effective hai.
        loop="asyncio",
    )