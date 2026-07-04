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
groq_client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())
GROQ_MODEL = "openai/gpt-oss-120b"

SELF_MODIFY_SYSTEM_PROMPT = """You are an elite Python developer editing an existing source file.
You will be given the CURRENT FULL CONTENT of a file and an INSTRUCTION describing a change or
feature to add.

Return ONLY the complete, updated file content — nothing else. No markdown fences, no explanation,
no commentary before or after.
"""


def _is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == settings.ADMIN_TELEGRAM_ID


async def upgrade_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: Reads file path and prompts AI tool to refactor code on GitHub."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    full_text = " ".join(context.args) if context.args else ""
    if "|" not in full_text:
        await update.message.reply_text("❌ Usage: `/upgrade <file_path> | <instructions>`", parse_mode="Markdown")
        return

    path_part, instruction = full_text.split("|", 1)
    file_path = path_part.strip()
    instruction = instruction.strip()

    status_msg = await update.message.reply_text(f"📦 GitHub se `{file_path}` fetch kar raha hoon...", parse_mode="Markdown")

    try:
        repo = github_client.get_repo(settings.GITHUB_REPO)
        contents = repo.get_contents(file_path)
        current_content = contents.decoded_content.decode("utf-8")

        await status_msg.edit_text("🧠 Groq AI dwara file content refactor kiya ja raha hai...")

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SELF_MODIFY_SYSTEM_PROMPT},
                {"role": "user", "content": f"=== CURRENT CONTENT OF {file_path} ===\n{current_content}\n\n=== INSTRUCTION ===\n{instruction}"}
            ],
            temperature=0.1
        )

        updated_code = response.choices[0].message.content.strip()
        if updated_code.startswith("```"):
            lines = updated_code.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            updated_code = "\n".join(lines).strip()

        await status_msg.edit_text("📤 Code modification completed. Pushing change back to GitHub...")
        repo.update_file(contents.path, f"Self-upgrade: {file_path}", updated_code, contents.sha)
        await status_msg.edit_text(f"✅ **Success!** `{file_path}` safely modified and pushed to main branch. Deployment tracking will trigger shortly.", parse_mode="Markdown")

    except GithubException as ge:
        await status_msg.edit_text(f"❌ **GitHub Error:** {ge.data.get('message', str(ge))}")
    except Exception as e:
        await status_msg.edit_text(f"❌ **Upgrade System Fail:** {str(e)}")


async def restart_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: immediately kills process so process managers boot fresh image."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return
    await update.message.reply_text("🔄 Bot application ko force restart kiya ja raha hai...")
    os._exit(0)


async def add_link_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: Manually insert item inside repository."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    full_text = " ".join(context.args) if context.args else ""
    if "|" not in full_text:
        await update.message.reply_text("❌ Usage: `/addlink <name> | <url>`", parse_mode="Markdown")
        return

    name_part, url_part = full_text.split("|", 1)
    name = name_part.strip()
    url = url_part.strip()

    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        await repo.add_link(name=name, url=url, added_by=update.effective_user.id)
        await session.commit()

    await update.message.reply_text(f"✅ **Manually Added Link:**\n🎬 *Name:* {name}\n🔗 *URL:* {url}", parse_mode="Markdown")


async def sync_website_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: Scan dedicated domain and push contents automatically into internal records."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner use kar sakta hai.")
        return

    target_url = context.args[0] if context.args else settings.WEBSITE_URL
    if not target_url:
        await update.message.reply_text("❌ Koi URL specified nahi hai aur settings me WEBSITE_URL khali hai.")
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


async def list_recent_links_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: recently saved links dikhata hai. Ab ye /recentlinks se chalega."""
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
