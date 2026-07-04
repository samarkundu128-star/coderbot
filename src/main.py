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
from src.telegram.commands import start_command, help_command, clear_command, newproject_command, links_command
# FIXED: do_command_handler ko yahan se hata diya gaya hai kyunki core_message_handler hi sab handle karti hai
from src.telegram.handlers import core_message_handler, quality_button_callback_handler
from src.telegram.admin_commands import (
    upgrade_command_handler,
    restart_command_handler,
    add_link_command_handler,
    list_recent_links_command_handler,
    sync_website_command_handler,
)
from src.telegram.scraper_commands import getlinks_command_handler
from src.services.website_sync_service import sync_website_links
from src.telegram.middleware import run_global_middleware, subscription_recheck_callback

logger = structlog.get_logger(__name__)


async def run_scheduler_jobs():
    """Background cron targets to keep application synchronized."""
    try:
        logger.info("Scheduler task triggered: Automatic website link scanning...")
        added = await sync_website_links(settings.WEBSITE_URL, added_by=settings.ADMIN_TELEGRAM_ID)
        logger.info("Scheduler job execution finished", newly_inserted_count=added)
    except Exception as exc:
        logger.error("Cron synchronized sync failed", error=str(exc))


def build_telegram_application() -> Application:
    """Builds and wires up internal routes for upstream production dispatch."""
    application = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN.get_secret_value())
        .build()
    )

    # --------------------------------------------------------------------------
    # MIDDLEWARE GATEWAY
    # --------------------------------------------------------------------------
    async def middleware_interceptor_wrapper(update: Update, context):
        allowed = await run_global_middleware(update, context)
        if not allowed:
            raise ApplicationHandlerStop()

    application.add_handler(TypeHandler(Update, middleware_interceptor_wrapper), group=-1)

    # --------------------------------------------------------------------------
    # SUBSCRIPTION/CHANNEL FORCE RE-CHECK CALLBACK
    # --------------------------------------------------------------------------
    application.add_handler(
        CallbackQueryHandler(subscription_recheck_callback, pattern="^recheck_subscription$")
    )

    # --------------------------------------------------------------------------
    # QUALITY BUTTONS CLICK EVENT
    # --------------------------------------------------------------------------
    application.add_handler(
        CallbackQueryHandler(quality_button_callback_handler, pattern=r"^bypass_")
    )

    # --------------------------------------------------------------------------
    # TELEGRAM CORE COMMAND ROUTING
    # --------------------------------------------------------------------------
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("newproject", newproject_command))
    application.add_handler(CommandHandler("links", links_command))

    # --------------------------------------------------------------------------
    # OWNER-ONLY ADMINISTRATIVE INTERFACES
    # --------------------------------------------------------------------------
    application.add_handler(CommandHandler("upgrade", upgrade_command_handler))
    application.add_handler(CommandHandler("restart", restart_command_handler))
    application.add_handler(CommandHandler("addlink", add_link_command_handler))
    application.add_handler(CommandHandler("syncwebsite", sync_website_command_handler))
    application.add_handler(CommandHandler("getlinks", getlinks_command_handler))
    application.add_handler(CommandHandler("recentlinks", list_recent_links_command_handler))

    # --------------------------------------------------------------------------
    # PLAIN TEXT FALLBACK (Handles text inputs, /do requests, and admin auto-saves)
    # --------------------------------------------------------------------------
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_message_handler))

    return application


tg_application: Application = None
scheduler: AsyncIOScheduler = None


@asynccontextmanager
async def lifespan_context_manager(app: FastAPI):
    """Lifecycle lifecycle engine hook initialization wrapper."""
    global tg_application, scheduler
    logger.info("Initializing application infrastructure...")

    await init_db_schema()

    tg_application = build_telegram_application()
    await tg_application.initialize()

    try:
        register_async_exception_handler()
    except NameError:
        pass

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_scheduler_jobs, "interval", hours=4, id="auto_website_sync")
    scheduler.start()
    logger.info("Background Cron scheduler scheduler initialized successfully.")

    if settings.WEBHOOK_ENABLED and settings.WEBHOOK_URL:
        webhook_target = f"{settings.WEBHOOK_URL.rstrip('/')}/telegram-webhook-endpoint"
        logger.info("Registering outbound Upstream Webhook URL target...", target=webhook_target)
        await tg_application.bot.set_webhook(
            url=webhook_target,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            secret_token=settings.TELEGRAM_BOT_TOKEN.get_secret_value()[:16],
        )
    else:
        logger.warn("WEBHOOK_ENABLED is false. Starting native local development Long Polling...")
        await tg_application.bot.delete_webhook()
        await tg_application.start()
        await tg_application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    yield

    logger.info("Triggering standard microservice graceful shutdown procedures...")
    if scheduler.running:
        scheduler.shutdown()

    if tg_application:
        if settings.WEBHOOK_ENABLED and settings.WEBHOOK_URL:
            await tg_application.bot.delete_webhook()
        else:
            await tg_application.updater.stop()
            await tg_application.stop()
        await tg_application.shutdown()

    logger.info("Infrastructure lifecycle destroyed successfully. Off.")


app = FastAPI(
    title="AI Telegram Link Service Engine",
    version="2.4.0",
    lifespan=lifespan_context_manager,
)


@app.get("/healthz", status_code=status.HTTP_200_OK)
async def service_health_check_endpoint():
    return {"status": "healthy", "scheduler_active": scheduler.running if scheduler else False}


@app.post("/telegram-webhook-endpoint")
async def process_incoming_telegram_updates(
    request: Request, x_telegram_bot_api_secret_token: str = Header(None, alias="X-Telegram-Bot-Api-Secret-Token")
):
    if not settings.WEBHOOK_ENABLED:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    expected_token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()[:16]
    if x_telegram_bot_api_secret_token != expected_token:
        logger.warn("Unauthorized webhook secret token validation signature mismatch detected. Dropping packet.")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload_dict = await request.json()
        update_object = Update.de_json(payload_dict, tg_application.bot)
        await tg_application.process_update(update_object)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as webhook_error:
        logger.error("Webhook packet crash processing failure encountered", error=str(webhook_error))
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(
        "main.py:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
