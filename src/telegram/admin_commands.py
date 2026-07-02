import structlog
from groq import Groq
from github import Github, GithubException
from telegram import Update
from telegram.ext import ContextTypes

from src.config.settings import settings

logger = structlog.get_logger(__name__)

github_client = Github(settings.GITHUB_TOKEN.get_secret_value())

# Apna khud ka Groq client (handlers.py par depend nahi karta, kisi bhi naam ka
# variable ho ya na ho wahan — is file ko standalone rakha hai)
groq_client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())

# NOTE: llama3-70b-8192, llama-3.3-70b-versatile, aur llama-3.1-8b-instant sab
# Groq dwara deprecate ho chuke hain (last update: 17 June 2026). Current
# recommended high-quality model: openai/gpt-oss-120b
GROQ_MODEL = "openai/gpt-oss-120b"

SELF_MODIFY_SYSTEM_PROMPT = """You are an elite Python developer editing an existing source file.
You will be given the CURRENT FULL CONTENT of a file and an INSTRUCTION describing a change or
feature to add.

Return ONLY the complete, updated file content — nothing else. No markdown fences, no explanation,
no commentary before or after. The output must be a fully valid, runnable replacement for the
entire file, preserving all existing functionality unless the instruction explicitly asks to
remove or change it.
"""


def _is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == settings.ADMIN_TELEGRAM_ID


async def upgrade_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only command to self-modify the bot's codebase.

    Usage:
        /upgrade <file_path> | <instructions>

    Example:
        /upgrade src/telegram/handlers.py | ek naya /joke command add karo
    """
    if not _is_admin(update):
        logger.warning(
            "Unauthorized /upgrade attempt blocked",
            user_id=update.effective_user.id if update.effective_user else None,
        )
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    raw_text = " ".join(context.args) if context.args else ""

    if "|" not in raw_text:
        await update.message.reply_text(
            "⚠️ Format galat hai. Sahi format:\n\n"
            "`/upgrade <file_path> | <instructions>`\n\n"
            "Example:\n"
            "`/upgrade src/telegram/handlers.py | ek naya /joke command add karo`",
            parse_mode="Markdown",
        )
        return

    file_path, instructions = raw_text.split("|", 1)
    file_path = file_path.strip()
    instructions = instructions.strip()

    if not file_path or not instructions:
        await update.message.reply_text("⚠️ File path aur instructions dono zaroori hain.")
        return

    status_msg = await update.message.reply_text(f"🔧 `{file_path}` ko padh raha hoon GitHub se...", parse_mode="Markdown")

    try:
        repo = github_client.get_repo(settings.REPO_NAME)

        # Step 1: Current file content GitHub se fetch karein
        try:
            file_obj = repo.get_contents(file_path)
            current_content = file_obj.decoded_content.decode("utf-8")
        except GithubException:
            await status_msg.edit_text(f"❌ File `{file_path}` repo mein nahi mili. Path check karein.", parse_mode="Markdown")
            return

        await status_msg.edit_text("🤖 Groq AI code update kar raha hai...")

        # Step 2: Groq AI se updated content generate karwayein
        response = await _generate_updated_code(current_content, instructions)

        if not response or not response.strip():
            await status_msg.edit_text("⚠️ AI ne khaali response diya. Dubara try karein.")
            return

        # Step 3: GitHub par commit karein
        await status_msg.edit_text("📤 GitHub par commit kar raha hoon...")

        repo.update_file(
            path=file_obj.path,
            message=f"🤖 Auto-upgrade via /upgrade: {instructions[:60]}",
            content=response,
            sha=file_obj.sha,
        )

        await status_msg.edit_text(
            f"✅ `{file_path}` successfully update ho gaya!\n\n"
            "Render 1-2 minute mein automatically naya deploy start kar dega.",
            parse_mode="Markdown",
        )
        logger.info("Self-modify commit successful", file=file_path, instructions=instructions)

    except GithubException as ge:
        logger.error("GitHub commit failed", error=str(ge))
        await status_msg.edit_text(f"❌ GitHub error: {str(ge)}")
    except Exception as e:
        logger.error("upgrade_command_handler failed", error=str(e))
        await status_msg.edit_text(f"❌ Kuch galat ho gaya: {str(e)}")


async def _generate_updated_code(current_content: str, instructions: str) -> str:
    """Groq API ko synchronous SDK ke sath thread mein call karta hai."""
    import asyncio

    user_prompt = (
        f"CURRENT FILE CONTENT:\n{current_content}\n\n"
        f"INSTRUCTION:\n{instructions}"
    )

    completion = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SELF_MODIFY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=4096,
    )
    return completion.choices[0].message.content
