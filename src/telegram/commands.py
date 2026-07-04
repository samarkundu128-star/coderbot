from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
from src.config.settings import settings
from src.database.connection import AsyncSessionLocal
from src.database.repository import ChatRepository, ProjectRepository, LinkRepository


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
        "🎬 *Movie & Anime Downloader Bot* 🚀\n\n"
        "Ab aapko ads ke jhanjhat me padne ki zaroorat nahi! Bas niche diye gaye tarike se search karein:\n\n"
        "🔍 *Kaise Search Karein:*\n"
        "⚡ `/links <movie_ya_anime_ka_naam>`\n"
        "Example: `/links Naruto` ya `/links Avengers`\n\n"
        "💡 Baaki commands dekhne ke liye `/help` dabayein."
    )
    await update.effective_chat.send_message(
        text=welcome_text,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=_footer_markup(),
    )


async def help_command(update: Update, context: CallbackContext) -> None:
    """Detailed, categorized user guide with global user instructions."""
    is_admin = update.effective_user is not None and update.effective_user.id == settings.ADMIN_TELEGRAM_ID

    help_text = (
        "📖 *Bot Usage Guide — Users Ke Liye*\n\n"
        "🤖 **Bot Se Download Kaise Karein?**\n"
        "1️⃣ Type karein: `/links <naam>` (Example: `/links Naruto`)\n"
        "2️⃣ Bot aapko us movie/anime ki available qualities dikhaega.\n"
        "3️⃣ Apni pasand ki quality (480p, 720p, 1080p) ke button par click karein.\n"
        "4️⃣ Bot background me saare ads ko bypass karke aapko **Direct Download Link** de dega!\n\n"
        "📌 **Bypass Ke Baad Kya Karein?**\n"
        "• Direct Link par click karte hi agar browser me koi doosra pop-up page khule, toh use turant back/close kar dein aur wapas original page par 'Download Now' par click karein."
    )

    if is_admin:
        help_text += (
            "\n\n*🔐 Owner-only Commands:*\n"
            "• `/upgrade <file_path> | <instructions>` — modify codebase\n"
            "• `/restart` — bot process restart\n"
            "• `/addlink <name> | <url>` — manual link add\n"
            "• `/syncwebsite <url>` — website se saare links automatic uthana\n"
            "• `/recentlinks` — recently saved 20 links dekhna\n"
            "• `/do <task>` — AI code generation utility\n"
        )

    await update.effective_chat.send_message(
        text=help_text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=_footer_markup()
    )


async def links_command(update: Update, context: CallbackContext) -> None:
    """
    Public Command: Saare users ke liye database se links bina kisi limit ke dhoondta hai.
    Usage: /links <movie_ya_anime_ka_naam>
    """
    query = " ".join(context.args) if context.args else ""
    
    if not query.strip():
        await update.message.reply_text(
            "⚠️ **Sahi Format:** `/links <naam>`\n"
            "Example: `/links Naruto` ya `/links Avengers`"
        )
        return

    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        matches = await repo.search(query)

    if not matches:
        await update.message.reply_text(
            "😔 Maaf kijiyega, is naam ka koi link nahi mila.\n"
            "Admin jald hi ise update karke website se sync kar dega!"
        )
        return

    await update.message.reply_text(
        f"🔎 *{len(matches)} results* mile hain! Niche se quality select karein:", 
        parse_mode=constants.ParseMode.MARKDOWN
    )

    for m in matches:
        keyboard = [
            [
                InlineKeyboardButton("🎥 480p", callback_data=f"bypass_{m.id}_480p"),
                InlineKeyboardButton("🎥 720p", callback_data=f"bypass_{m.id}_720p"),
                InlineKeyboardButton("🎥 1080p", callback_data=f"bypass_{m.id}_1080p")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🎬 *{m.name}*", 
            parse_mode=constants.ParseMode.MARKDOWN, 
            reply_markup=reply_markup
        )


async def clear_command(update: Update, context: CallbackContext) -> None:
    """Current session ki chat history ko wipe out karne ke liye."""
    chat_id = update.effective_chat.id
    async with AsyncSessionLocal() as session:
        chat_repo = ChatRepository(session)
        await chat_repo.clear_history(chat_id)
        await session.commit()
    await update.effective_chat.send_message(text=\"🧹 **Success:** Aapki purani conversation history session delete ho gayi hai.\")


async def newproject_command(update: Update, context: CallbackContext) -> None:
    """Naya folder/project tracking context shuru karne ke liye."""
    if not context.args:
        await update.effective_chat.send_message(text=\"❌ **Usage:** `/newproject <project_name>`\")
        return

    project_name = " ".join(context.args)
    tg_id = update.effective_user.id

    async with AsyncSessionLocal() as session:
        proj_repo = ProjectRepository(session)
        project = await proj_repo.create_project(telegram_id=tg_id, name=project_name)
        await session.commit()

    await update.effective_chat.send_message(
        text=f"📂 **Project Active:** Naya project `{project_name}` allocate ho chuka hai!"
    )
