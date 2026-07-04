import re
import json
import asyncio
import structlog
import httpx
from bs4 import BeautifulSoup
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.config.settings import settings
from src.database.connection import AsyncSessionLocal
from src.database.repository import LinkRepository
from src.services.ai_engine import AICodingEngine
from src.services.github_service import push_files
from src.services.render_service import watch_deploy_and_notify
from src.services.intent_router import classify_intent

logger = structlog.get_logger(__name__)

groq_client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())
GROQ_MODEL = "openai/gpt-oss-120b"

_ai_engine = AICodingEngine()

URL_REGEX = re.compile(r"https?://[^\s]+")

SYSTEM_PROMPT = """You are an elite coding assistant. When given a task, respond ONLY with a valid JSON object — no markdown fences, no extra commentary, nothing outside the JSON.

The JSON must have exactly these keys:
{
  "language": "programming language name, e.g. python",
  "filename": "suggested filename, e.g. calculator.py",
  "code": "the complete, runnable code as a single string with \n for newlines",
  "explanation": "a short 1-3 sentence explanation of how the code works"
}

Rules:
- Code must be complete and runnable, not a snippet.
"""


def _is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == settings.ADMIN_TELEGRAM_ID


async def _store_link_from_message(update: Update, url: str, remainder: str):
    name = remainder if remainder else f"Link ({url[:30]}...)"
    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        await repo.add_link(name=name, url=url, added_by=update.effective_user.id)
        await session.commit()
    await update.message.reply_text(f"✅ **Saved Link:**\n📝 *Name:* {name}\n🔗 *URL:* {url}", parse_mode="Markdown")


async def _handle_owner_code_task(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    status_msg = await update.message.reply_text("🧠 Coding task analyze kar raha hoon...")
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        raw_json = response.choices[0].message.content
        parsed = json.loads(raw_json)
        
        filename = parsed.get("filename", "output.py")
        code = parsed.get("code", "")
        explanation = parsed.get("explanation", "")

        await status_msg.edit_text(f"📝 **File Generation:** `{filename}`\n\n`{explanation}`\n\nPushing to GitHub...")
        
        commit_sha = await asyncio.to_thread(
            push_files,
            [{"file_path": filename, "content": code}],
            f"Auto-coded: {filename}"
        )
        
        await status_msg.edit_text(f"🚀 **Pushed to GitHub!**\nCommit: `{commit_sha[:7]}`\n\nStarting Render Deployment tracker...")
        asyncio.create_task(watch_deploy_and_notify(context.bot, update.effective_chat.id, commit_sha))
    except Exception as e:
        logger.error("owner_code_task_failed", error=str(e))
        await status_msg.edit_text(f"❌ **Code Task Error:** {str(e)}")


async def _handle_ai_chat(update: Update, user_text: str):
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful teaching assistant chat bot."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.6,
            max_tokens=600,
        )
        reply = response.choices[0].message.content
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error("ai_chat_failed", error=str(e))
        await update.message.reply_text("⚠️ Abhi response nahi de paya, dubara try karein.")


# ---------------------------------------------------------------------------
# Website Ads Bypass Logic & Inline Button Clicks
# ---------------------------------------------------------------------------
async def _bypass_website_ads_engine(original_url: str, quality: str) -> str:
    """Background scraper/bypasser to clean shortener pages and fetch source url."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(original_url)
            soup = BeautifulSoup(resp.text, "html.parser")
            all_links = soup.find_all("a", href=True)
            
            for link in all_links:
                href = link["href"]
                text = link.get_text().lower()
                if quality in text or (quality in href and "download" in text):
                    return href
            for link in all_links:
                href_lower = link["href"].lower()
                if any(x in href_lower for x in ["drive", "mega", "download", "gdrive", "gplinks"]):
                    return link["href"]
    except Exception as e:
        logger.error("Bypass module error", error=str(e))
    return original_url


async def quality_button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when any normal user clicks on 480p/720p/1080p buttons."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    _, link_id, quality = data.split("_")
    
    await query.edit_message_text(text=f"🔄 {quality} ke liye website se ads bypass kiye ja rahe hain, please wait...")

    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        link_obj = await repo.get_by_id(int(link_id))

    if not link_obj:
        await query.edit_message_text(text="❌ Error: Yeh link database me nahi mila.")
        return

    direct_download_url = await _bypass_website_ads_engine(link_obj.url, quality)

    delivery_message = (
        f"✅ *Direct Download Link Ready!* ({quality})\n\n"
        f"📌 *Name:* {link_obj.name}\n"
        f"🚀 *⚡ Clean Download Link:* {direct_download_url}\n\n"
        "📖 *Download Kaise Karein? (Tutorial):*\n"
        "Link par click karein. Agar browser me koi dusra pop-up ad page khule, toh use turant **Back** karke band kar dein aur main page par clear 'Download Now' button par click karein!"
    )
    await query.edit_message_text(text=delivery_message, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Core entrypoint for all plain-text (non-command) messages
# ---------------------------------------------------------------------------
async def core_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""

    if _is_admin(update):
        url_match = URL_REGEX.search(user_text)
        if url_match:
            url = url_match.group(0)
            remainder = (user_text[:url_match.start()] + user_text[url_match.end():]).strip(" -:|\n")
            await _store_link_from_message(update, url, remainder)
            return

        intent = await classify_intent(user_text)
        if intent == "CODE_TASK":
            await _handle_owner_code_task(update, context, user_text)
        else:
            await _handle_ai_chat(update, user_text)
        return

    # Non-owner users: search first, then fallback to AI chat
    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        matches = await repo.search(user_text)

    if matches:
        await update.message.reply_text(f"🔎 *{len(matches)} results* mile! Niche se quality chuney:", parse_mode="Markdown")
        for m in matches:
            keyboard = [[
                InlineKeyboardButton("🎥 480p", callback_data=f"bypass_{m.id}_480p"),
                InlineKeyboardButton("🎥 720p", callback_data=f"bypass_{m.id}_720p"),
                InlineKeyboardButton("🎥 1080p", callback_data=f"bypass_{m.id}_1080p")
            ]]
            await update.message.reply_text(f"🎬 *{m.name}*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await _handle_ai_chat(update, user_text)
