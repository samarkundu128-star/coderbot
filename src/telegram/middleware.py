import time
from collections import defaultdict
from telegram import Update
from telegram.ext import CallbackContext
import structlog
from src.database.connection import AsyncSessionLocal
from src.database.repository import UserRepository, ChatRepository

logger = structlog.get_logger(__name__)

# Anti-spam limits setup (Max 20 messages per minute)
RATE_LIMIT_WINDOW = 60
MAX_REQUESTS_PER_WINDOW = 20
rate_limit_store = defaultdict(list)

class TelegramMiddlewareEngine:
    @staticmethod
    async def process_user_and_rate_limit(update: Update, context: CallbackContext) -> bool:
        if not update.effective_user or not update.effective_chat:
            return False

        tg_id = update.effective_user.id
        chat_id = update.effective_chat.id
        current_time = time.time()

        # Sliding window algorithm for rate limiting
        user_timestamps = rate_limit_store[tg_id]
        rate_limit_store[tg_id] = [t for t in user_timestamps if current_time - t < RATE_LIMIT_WINDOW]

        if len(rate_limit_store[tg_id]) >= MAX_REQUESTS_PER_WINDOW:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ **Rate Limit Active:** Aap boht jaldi commands bhej rahe hain. Kripya thodi der ruk kar try karein!"
            )
            return False

        rate_limit_store[tg_id].append(current_time)

        # Database automatic onboarding (Naye users ko automatic database me add karna)
        async with AsyncSessionLocal() as session:
            user_repo = UserRepository(session)
            chat_repo = ChatRepository(session)

            user = await user_repo.get_by_id(tg_id)
            if not user:
                user = await user_repo.create_user(
                    telegram_id=tg_id,
                    first_name=update.effective_user.first_name,
                    last_name=update.effective_user.last_name,
                    username=update.effective_user.username
                )
                logger.info("Naya user database me automatically register ho gaya!", telegram_id=tg_id)

            if user.is_banned:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text="🚫 Aapka account system par ban kar diya gaya hai."
                )
                return False

            chat = await chat_repo.get_by_id(chat_id)
            if not chat:
                await chat_repo.create_chat(chat_id=chat_id, telegram_id=tg_id)

            await session.commit()
        return True
