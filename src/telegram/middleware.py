import time
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CallbackContext
import structlog
from src.config.settings import settings
from src.database.connection import AsyncSessionLocal
from src.database.repository import UserRepository, ChatRepository

logger = structlog.get_logger(__name__)


def _subscribe_gate_markup() -> InlineKeyboardMarkup:
    channel = settings.CHANNEL_USERNAME.lstrip("@")
    buttons = [[InlineKeyboardButton("📢 Channel Join Karein", url=f"https://t.me/{channel}")]]
    if settings.WEBSITE_URL:
        buttons.append([InlineKeyboardButton("🌐 Website Visit Karein", url=settings.WEBSITE_URL)])
    buttons.append([InlineKeyboardButton("✅ Maine Join Kar Liya", callback_data="recheck_subscription")])
    return InlineKeyboardMarkup(buttons)


async def is_user_subscribed(update: Update, context: CallbackContext) -> bool:
    """Owner ko aur agar force-subscribe off/unconfigured hai to sabko bypass milta hai."""
    if not settings.FORCE_SUBSCRIBE_ENABLED or not settings.CHANNEL_USERNAME:
        return True
    if update.effective_user and update.effective_user.id == settings.ADMIN_TELEGRAM_ID:
        return True
    try:
        member = await context.bot.get_chat_member(
            chat_id=settings.CHANNEL_USERNAME, user_id=update.effective_user.id
        )
        return member.status in ("member", "administrator", "creator")
    except TelegramError as e:
        # Agar channel-check hi fail ho jaye (bot admin nahi hai channel me, etc.)
        # to users ko block mat karo — fail-open taaki bot use-able rahe.
        logger.warning("Subscription check failed, allowing by default", error=str(e))
        return True


async def send_subscribe_gate(update: Update, context: CallbackContext):
    text = (
        "🔒 *Ek aakhri step!*\n\n"
        "Is bot ko use karne ke liye pehle hamara official channel join kar lein — "
        "wahan naye features, updates aur tips milte hain.\n\n"
        "Join karne ke baad neeche wala button dabayein."
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=_subscribe_gate_markup())
    else:
        await update.effective_chat.send_message(text, parse_mode="Markdown", reply_markup=_subscribe_gate_markup())


async def subscription_recheck_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    subscribed = await is_user_subscribed(update, context)
    if subscribed:
        await query.message.edit_text("✅ Shukriya! Ab aap bot poori tarah use kar sakte hain. Kuch bhi type karein 🚀")
    else:
        await query.answer("⚠️ Abhi bhi channel join nahi dikh raha. Join karke dubara try karein.", show_alert=True)

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


async def run_global_middleware(update: Update, context: CallbackContext) -> bool:
    """
    main.py me group=-1 par register hota hai — har update ke liye sabse pehle chalta hai.
    Return False ka matlab: further handlers (commands/messages) skip ho jaayenge.
    """
    if update.callback_query:
        # Callback queries (jaise "Maine Join Kar Liya" button) apna dedicated
        # handler khud handle karta hai — yahan sirf rate-limit/onboarding chalega,
        # subscription-gate dobara mat dikhao.
        ok = await TelegramMiddlewareEngine.process_user_and_rate_limit(update, context)
        return ok

    ok = await TelegramMiddlewareEngine.process_user_and_rate_limit(update, context)
    if not ok:
        return False

    subscribed = await is_user_subscribed(update, context)
    if not subscribed:
        await send_subscribe_gate(update, context)
        return False

    return True
