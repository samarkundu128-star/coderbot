```python
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

# Logging setup takki runtime me errors console par dikhein
logger = structlog.get_logger(__name__)

# Main Telegram Application Instance setup kiya ja raha hai
telegram_app = (
    Application.builder()
    .token(settings.TELEGRAM_BOT_TOKEN.get_secret_value())
    .build()
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI Lifespan Manager: Startup aur Shutdown ke waqt 
    webhook registration aur connections cleanup ko handle karta hai.
    """
    try:
        # Saare commands aur text handlers register kiye ja rahe hain
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("clear", clear_command))
        telegram_app.add_handler(CommandHandler("newproject", newproject_command))
        telegram_app.add_handler(CommandHandler("Do", do_command_handler))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, core_message_handler))

        # Application initialize ho raha hai backend par
        await telegram_app.initialize()
        
        # Webhook routing configure ho raha hai
        webhook_target = f"{settings.WEBHOOK_URL}/webhook"
        logger.info("Connecting Telegram Webhook...", url=webhook_target)
        
        # Security ke sath webhook URL set kiya ja raha hai
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
        # Shutdown ke waqt clean up logic taaki connection conflicts na ho
        logger.info("Cleaning up webhook active routes...")
        await telegram_app.bot.delete_webhook()
        await telegram_app.shutdown()
        logger.info("Downstream network shutdown complete.")

# FastAPI Web Server initialize kiya ja raha hai
app = FastAPI(
    title="Telegram AI Coding Gateway",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """
    Render health checks ke liye endpoint. 
    Agar status 'healthy' aayega, tabhi deployment active rahegi.
    """
    return {"status": "healthy", "environment": settings.ENVIRONMENT}

@app.post("/webhook")
async def webhook_handler(request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    """
    Main Webhook Endpoint. Telegram yahan raw updates bhejta hai, 
    aur yeh token verification ke baad use safety se process karta hai.
    """
    secret = settings.WEBHOOK_SECRET_TOKEN.get_secret_value()
    
    # Token verify karke anonymous access ko reject karte hain
    if x_telegram_bot_api_secret_token != secret:
        logger.warning("Unverified request blocked! Token mismatch.")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        
        # Update ko process karne ke liye telegram pipeline me transfer kar rahe hain
        await telegram_app.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as webhook_error:
        logger.error("Error receiving incoming update payload", error=str(webhook_error))
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

if __name__ == "__main__":
    # Local debugging and testing entrypoint
    uvicorn.run("src.main:app", host="0.0.0.0", port=10000, reload=True)

```

