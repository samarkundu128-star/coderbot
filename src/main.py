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
from fastapi import FastAPI, Request, Response, status, Header
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.config.settings import settings
from src.telegram.commands import start_command, help_command, clear_command, newproject_command
from src.telegram.handlers import core_message_handler, do_command_handler
from src.telegram.admin_commands import upgrade_command_handler

logger = structlog.get_logger(__name__)


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

        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("clear", clear_command))
        telegram_app.add_handler(CommandHandler("newproject", newproject_command))
        # NOTE: Telegram commands lowercase hone chahiye (BotFather bhi yehi enforce karta hai)
        telegram_app.add_handler(CommandHandler("do", do_command_handler))
        telegram_app.add_handler(CommandHandler("upgrade", upgrade_command_handler))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_message_handler))

        await telegram_app.initialize()

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

        yield
    except Exception as init_error:
        logger.critical("Lifespan startup engine failed to initialize!", error=str(init_error))
        sys.exit(1)
    finally:
        logger.info("Cleaning up webhook active routes...")
        await telegram_app.bot.delete_webhook()
        await telegram_app.shutdown()
        logger.info("Downstream network shutdown complete.")

app = FastAPI(
    title="Telegram AI Coding Gateway",
    version="1.0.0",
    lifespan=lifespan
)

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
    )
