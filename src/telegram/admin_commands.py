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

NEW_FILE_SYSTEM_PROMPT = """You are an elite Python developer creating a brand new source file for
a FastAPI + python-telegram-bot (v21.3) project that uses the Groq SDK for AI features.

You will be given a FILE PATH and an INSTRUCTION describing what this new file should contain.

Return ONLY the complete file content — nothing else. No markdown fences, no explanation, no
commentary before or after. The output must be fully valid, runnable Python code that follows
standard conventions for this stack (async def handlers using `Update` and
`ContextTypes.DEFAULT_TYPE` for Telegram command handlers, structlog for logging, etc).
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

    status_msg = await update.message.reply_text(f"🔧 `{file_path}` check kar raha hoon GitHub par...", parse_mode="Markdown")

    try:
        repo = github_client.get_repo(settings.REPO_NAME)

        # Step 1: Check karein file exist karti hai ya naya banani hai
        file_exists = True
        current_content = ""
        file_sha = None

        try:
            file_obj = repo.get_contents(file_path)
            current_content = file_obj.decoded_content.decode("utf-8")
            file_sha = file_obj.sha
        except GithubException:
            file_exists = False

        if file_exists:
            await status_msg.edit_text("🤖 Groq AI existing code update kar raha hai...")
            response = await _generate_updated_code(current_content, instructions)
        else:
            await status_msg.edit_text(f"🆕 `{file_path}` nayi file hai — Groq AI se create karwa raha hoon...", parse_mode="Markdown")
            response = await _generate_new_file(file_path, instructions)

        if not response or not response.strip():
            await status_msg.edit_text("⚠️ AI ne khaali response diya. Dubara try karein.")
            return

        # Safety: agar AI ne instructions ignore karke markdown fences laga di hon, hata dein
        response = response.strip()
        if response.startswith("```"):
            lines = response.split("\n")
            response = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        # Step 2: GitHub par commit karein (naya file ya existing update, dono handle)
        await status_msg.edit_text("📤 GitHub par commit kar raha hoon...")

        if file_exists:
            repo.update_file(
                path=file_path,
                message=f"🤖 Auto-upgrade via /upgrade: {instructions[:60]}",
                content=response,
                sha=file_sha,
            )
        else:
            repo.create_file(
                path=file_path,
                message=f"🤖 Auto-create via /upgrade: {instructions[:60]}",
                content=response,
            )

        action_word = "update" if file_exists else "create"
        await status_msg.edit_text(
            f"✅ `{file_path}` successfully {action_word} ho gaya!\n\n"
            "Render 1-2 minute mein automatically naya deploy start kar dega.\n\n"
            + ("⚠️ Naya command hai toh usse `main.py` mein register karna na bhoolein "
               "(alag `/upgrade` command se) taaki Telegram wo command samjh sake!" if not file_exists else ""),
            parse_mode="Markdown",
        )
        logger.info("Self-modify commit successful", file=file_path, action=action_word, instructions=instructions)

    except GithubException as ge:
        logger.error("GitHub commit failed", error=str(ge))
        await status_msg.edit_text(f"❌ GitHub error: {str(ge)}")
    except Exception as e:
        logger.error("upgrade_command_handler failed", error=str(e))
        await status_msg.edit_text(f"❌ Kuch galat ho gaya: {str(e)}")


async def _generate_new_file(file_path: str, instructions: str) -> str:
    """Naya file content Groq se generate karwata hai (scratch se)."""
    import asyncio

    user_prompt = f"FILE PATH: {file_path}\n\nINSTRUCTION:\n{instructions}"

    completion = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": NEW_FILE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=4096,
    )
    return completion.choices[0].message.content


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
