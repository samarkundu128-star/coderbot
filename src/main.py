import sys
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

logger = structlog.get_logger(__name__)

telegram_app = (
    Application.builder()
    .token(settings.TELEGRAM_BOT_TOKEN.get_secret_value())
    .build()
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("clear", clear_command))
        telegram_app.add_handler(CommandHandler("newproject", newproject_command))
        telegram_app.add_handler(CommandHandler("Do", do_command_handler))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_message_handler))

        await telegram_app.initialize()

        webhook_target = f"{settings.WEBHOOK_URL}/webhook"
        logger.info("Connecting Telegram Webhook...", url=webhook_target)

        await telegram_app.bot.set_webhook(
            url=webhook_target,
            secret_token=settings.WEBHOOK_SECRET_TOKEN.get_secret_value()
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
    secret = settings.WEBHOOK_SECRET_TOKEN.get_secret_value()

    if x_telegram_bot_api_secret_token != secret:
        logger.warning("Unverified request blocked! Token mismatch.")
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
    uvicorn.run("src.main:app", host="0.0.0.0", port=10000, reload=True)
