from telegram import Update, constants
from telegram.ext import CallbackContext
from src.database.connection import AsyncSessionLocal
from src.database.repository import ChatRepository, ProjectRepository

async def start_command(update: Update, context: CallbackContext) -> None:
    """
    User jab /start command bhejta hai toh welcome message aur menu show hota hai.
    """
    welcome_text = (
        "🚀 **Elite AI Coding Assistant Bot Active!**\n\n"
        "Welcome! Yeh ek high-performance, asynchronous coding bot hai jo aapke liye production-ready code likh sakta hai.\n\n"
        "**Core Commands list:**\n"
        "📂 `/newproject <project_name>` - Naya project scope banayein\n"
        "⚡ `/Do <prompt>` - AI ko directly code likhne ka force command dein\n"
        "🧹 `/clear` - Purani chat memory ko erase karein\n"
        "💡 `/help` - System manual aur specifications dekhein"
    )
    await update.effective_chat.send_message(text=welcome_text, parse_mode=constants.ParseMode.MARKDOWN)

async def help_command(update: Update, context: CallbackContext) -> None:
    """
    User ko help instructions provide karne ke liye.
    """
    help_text = (
        "💡 **User Guide & Features:**\n\n"
        "Aap kisi bhi coding problem ko simple bhasha me chat me likh sakte hain. AI automatic files and folders analyze karke code likh dega.\n\n"
        "**Aap kya kar sakte hain?**\n"
        "• Code structure generate karna\n"
        "• Purane code ke bugs fix karna\n"
        "• Algorithms aur logic samajhna\n"
        "• Docker/Deployment files generate karna\n\n"
        "Guaranteed processing ke liye, `/Do` ke sath apna message likhein: `/Do build a clean fastAPI auth system`"
    )
    await update.effective_chat.send_message(text=help_text, parse_mode=constants.ParseMode.MARKDOWN)

async def clear_command(update: Update, context: CallbackContext) -> None:
    """
    Current session ki chat history ko wipe out karne ke liye.
    """
    chat_id = update.effective_chat.id
    async with AsyncSessionLocal() as session:
        chat_repo = ChatRepository(session)
        await chat_repo.clear_history(chat_id)
        await session.commit()
    await update.effective_chat.send_message(text="🧹 **Success:** Aapki purani conversation history session delete ho gayi hai.")

async def newproject_command(update: Update, context: CallbackContext) -> None:
    """
    Naya folder/project tracking context shuru karne ke liye.
    """
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
