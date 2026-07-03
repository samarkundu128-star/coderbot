from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
from src.config.settings import settings
from src.database.connection import AsyncSessionLocal
from src.database.repository import ChatRepository, ProjectRepository


def _footer_markup():
    buttons = []
    if settings.CHANNEL_USERNAME:
        buttons.append(InlineKeyboardButton("📢 Channel", url=f"https://t.me/{settings.CHANNEL_USERNAME.lstrip('@')}"))
    if settings.WEBSITE_URL:
        buttons.append(InlineKeyboardButton("🌐 Website", url=settings.WEBSITE_URL))
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons])


async def start_command(update: Update, context: CallbackContext) -> None:
    """User jab /start command bhejta hai toh professional welcome message + quick-action buttons dikhte hain."""
    first_name = update.effective_user.first_name if update.effective_user else "Coder"
    welcome_text = (
        f"👋 *Welcome, {first_name}!*\n\n"
        "🚀 *AI Coding Gateway* — apni jeb me ek AI developer.\n\n"
        "Bas normal bhasha me apna coding task likhein, AI turant runnable code "
        "generate kar deta hai — bina IDE khole, bina setup jhanjhat ke.\n\n"
        "*Quick Commands:*\n"
        "⚡ `/do <task>` — turant code generate karein\n"
        "📂 `/newproject <name>` — naya project shuru karein\n"
        "🧹 `/clear` — chat history reset karein\n"
        "💡 `/help` — poori guide dekhein\n\n"
        "_Kisi bhi cheez ke baare me seedha type karke pooch sakte hain — main sun raha hoon!_"
    )
    await update.effective_chat.send_message(
        text=welcome_text,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=_footer_markup(),
    )


async def help_command(update: Update, context: CallbackContext) -> None:
    """Detailed, categorized user guide."""
    is_admin = update.effective_user is not None and update.effective_user.id == settings.ADMIN_TELEGRAM_ID

    help_text = (
        "📖 *Coderbot — Complete Guide*\n\n"
        "*🧑‍💻 Har koi kya kar sakta hai:*\n"
        "• `/start` — welcome menu\n"
        "• `/do <task>` — AI se ek-shot code generate karayein\n"
        "  _example:_ `/do python mein calculator banao`\n"
        "• `/newproject <name>` — naya isolated project banayein\n"
        "• `/clear` — apni chat history erase karein\n"
        "• Seedha type bhi kar sakte hain — koi bhi sawaal, guidance, ya feature "
        "kaise use karein, sab yahan pooch sakte hain\n"
    )

    if is_admin:
        help_text += (
            "\n*🔐 Owner-only Commands:*\n"
            "• `/upgrade <file_path> | <instructions>` — kisi specific file ko directly modify karein\n"
            "• `/restart` — bot process ko turant restart karein\n"
            "• `/addlink <name> | <url>` — ek download link store karein (ya bas URL wala message bhej dein)\n"
            "• `/links` — recently saved links dekhein\n"
            "• Seedha natural language me bol kar bhi poora bot modify/upgrade kara sakte hain — "
            "AI khud samajh lega ki yeh sirf chat hai ya code-change ka request, aur push+deploy "
            "hone ke baad aapko khud inform kar dega.\n"
        )

    await update.effective_chat.send_message(
        text=help_text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=_footer_markup()
    )


async def clear_command(update: Update, context: CallbackContext) -> None:
    """Current session ki chat history ko wipe out karne ke liye."""
    chat_id = update.effective_chat.id
    async with AsyncSessionLocal() as session:
        chat_repo = ChatRepository(session)
        await chat_repo.clear_history(chat_id)
        await session.commit()
    await update.effective_chat.send_message(text="🧹 **Success:** Aapki purani conversation history session delete ho gayi hai.")


async def newproject_command(update: Update, context: CallbackContext) -> None:
    """Naya folder/project tracking context shuru karne ke liye."""
    if not context.args:
        await update.effective_chat.send_message(text="❌ **Usage:** `/newproject <project_name>`")
        return

    project_name = " ".join(context.args)
    tg_id = update.effective_user.id

    async with AsyncSessionLocal() as session:
        proj_repo = ProjectRepository(session)
        project = await proj_repo.create_project(telegram_id=tg_id, name=project_name)
        await session.commit()
        p_id = project.id

    await update.effective_chat.send_message(
        text=f"📂 **Project Active:** Naya project `{project_name}` (ID: `{p_id}`) database me allocate ho chuka hai!"
    )
