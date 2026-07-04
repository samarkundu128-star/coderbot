import os
import structlog
from groq import Groq
from github import Github, GithubException
from telegram import Update
from telegram.ext import ContextTypes

from src.config.settings import settings
from src.database.connection import AsyncSessionLocal
from src.database.repository import LinkRepository
from src.services.website_sync_service import sync_website_links

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


async def restart_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only: bot process ko turant restart karta hai. Render (ya entrypoint.sh
    supervisor, agar wired hai) exit hote hi process ko automatically wapas launch
    kar deta hai — isliye downtime sirf kuch second ka hota hai.
    """
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    await update.message.reply_text("🔄 Bot restart ho raha hai... kuch second me wapas online hoga.")
    logger.warning("Manual restart triggered via /restart command", user_id=update.effective_user.id)
    os._exit(1)  # Process ko turant terminate karta hai; Render/supervisor ise auto-restart kar dega


async def addlink_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only: /addlink <name> | <url>
    Owner ke pass agar URL me hi naam nahi hai (ya explicit control chahiye), toh
    is command se manually bhi add kar sakte hain. Plain message me URL bhejna
    (core_message_handler) bhi automatically save kar deta hai.
    """
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    raw_text = " ".join(context.args) if context.args else ""
    if "|" not in raw_text:
        await update.message.reply_text(
            "⚠️ Format: `/addlink <name> | <url>`\n\nExample:\n`/addlink Naruto Episode 1 | https://example.com/ep1`",
            parse_mode="Markdown",
        )
        return

    name, url = raw_text.split("|", 1)
    name, url = name.strip(), url.strip()

    if not name or not url:
        await update.message.reply_text("⚠️ Name aur URL dono zaroori hain.")
        return

    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        await repo.add_link(name=name, url=url, added_by=update.effective_user.id)
        await session.commit()

    await update.message.reply_text(f"🔗 Link save ho gaya: *{name}*", parse_mode="Markdown")


async def sync_website_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only: /syncwebsite <optional website_url>

    - /syncwebsite                      → settings.WEBSITE_URL (env var) scan karta hai
    - /syncwebsite https://example.com  → koi bhi diya gaya website scan karta hai

    Jo bhi website do, uske saare <a> tag download links dhoond ke database me
    store kar deta hai (naam anchor text se milta hai). Jitni baar chaho, jitni
    alag websites chaho, sab isi ek command se chal jayenga.
    """
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    target_url = context.args[0].strip() if context.args else settings.WEBSITE_URL

    if not target_url:
        await update.message.reply_text(
            "⚠️ Koi website URL nahi mila.\n\n"
            "Usage: `/syncwebsite <website_url>`\n"
            "Example: `/syncwebsite https://example.com`\n\n"
            "Ya `WEBSITE_URL` environment variable set kar do — tab bina URL diye "
            "bhi `/syncwebsite` chal jayega.",
            parse_mode="Markdown",
        )
        return

    status_msg = await update.message.reply_text(f"🌐 `{target_url}` scan kar raha hoon...", parse_mode="Markdown")

    try:
        added_count = await sync_website_links(target_url, added_by=update.effective_user.id)
        if added_count == 0:
            await status_msg.edit_text("✅ Scan complete — koi naya link nahi mila (sab pehle se save hain, ya is page pe links nahi the).")
        else:
            await status_msg.edit_text(
                f"✅ Scan complete — *{added_count} naye link(s)* database me save ho gaye!\n\n"
                "Ab koi bhi user unka naam type karega toh link mil jayega.",
                parse_mode="Markdown",
            )
    except Exception as e:
        await status_msg.edit_text(f"❌ Website scan fail ho gaya: {str(e)}")


async def list_links_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: recently saved links dikhata hai."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        links = await repo.list_recent(limit=20)

    if not links:
        await update.message.reply_text("📭 Abhi tak koi link save nahi hua.")
        return

    lines = [f"📋 *Recent Links ({len(links)}):*\n"]
    for l in links:
        lines.append(f"• `{l.id}` — *{l.name}*\n  {l.url}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
