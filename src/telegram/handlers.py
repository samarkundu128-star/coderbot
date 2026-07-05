import re
import json
import asyncio
import difflib
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
BRUTE_FORCE_URL_REGEX = re.compile(r"https?://[^\s'\"\\><\}\{\[\]\)\(,\n\r\t]+")

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


# ---------------------------------------------------------------------------
# Brute-Force Raw Text Tokenizer & Aggressive Multi-Layer Deep Scraper
# ---------------------------------------------------------------------------
async def _recursive_link_extractor(client: httpx.AsyncClient, current_url: str, depth: int = 0, visited_urls: set = None) -> list:
    if visited_urls is None:
        visited_urls = set()
        
    if depth > 30 or current_url in visited_urls:
        return []
        
    visited_urls.add(current_url)
    links_found = []
    
    try:
        await asyncio.sleep(0.4)
        resp = await client.get(current_url)
        if resp.status_code != 200:
            return []
            
        page_content = resp.text
        
        all_raw_strings = BRUTE_FORCE_URL_REGEX.findall(page_content)
        for raw_url in all_raw_strings:
            rurl_lower = raw_url.lower()
            if any(x in rurl_lower for x in ["googleads", "doubleclick", "facebook.com", "twitter.com", "instagram.com", "youtube.com", "youtu.be"]):
                continue
                
            if raw_url not in visited_urls:
                if any(x in rurl_lower for x in ["drive", "mega", "mediafire", "pixeldrain", "gdrive", "terabox", "zippyshare", "gplinks", "droplink", "link", "download", "wp-content", "movie", "anime", "series"]):
                    links_found.append((raw_url, "Brute-Force Captured Stream Route"))

        soup = BeautifulSoup(page_content, "html.parser")
        forms = soup.find_all("form")
        for form in forms:
            action = form.get("action", "").strip()
            if action.startswith("http") and action not in visited_urls:
                sub_links = await _recursive_link_extractor(client, action, depth + 1, visited_urls)
                links_found.extend(sub_links)

        all_anchors = soup.find_all("a", href=True)
        for anchor in all_anchors:
            href = str(anchor["href"]).strip()
            text = str(anchor.get_text()).strip().lower()
            
            if href.startswith("http") and href not in visited_urls:
                if any(x in text or x in href.lower() for x in ["continue", "next", "get link", "download now", "open", "verify", "click here", "step", "unlock", "option"]):
                    sub_links = await _recursive_link_extractor(client, href, depth + 1, visited_urls)
                    links_found.extend(sub_links)
                    
    except Exception:
        pass
    return links_found


async def _deep_scrape_and_store_website(update: Update, target_url: str):
    status_msg = await update.message.reply_text("🚀 **Ultimate Brute-Force Bulk Tokenizer Active!** Webpage se saari links dhoondhi ja rahi hain...")
    
    try:
        clean_url = str(target_url).strip()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
            "Connection": "keep-alive"
        }
        
        async with httpx.AsyncClient(follow_redirects=True, timeout=120, headers=headers) as client:
            raw_extracted_links = await _recursive_link_extractor(client, clean_url)
            
            unique_links = {}
            for href, text in raw_extracted_links:
                if href not in unique_links:
                    unique_links[href] = text
            
            saved_count = 0
            async with AsyncSessionLocal() as session:
                repo = LinkRepository(session)
                
                for href, text in unique_links.items():
                    href_lower = href.lower()
                    text_lower = text.lower()
                    
                    quality = "Unknown"
                    if "480p" in href_lower or "480p" in text_lower:
                        quality = "480p"
                    elif "720p" in href_lower or "720p" in text_lower:
                        quality = "720p"
                    elif "1080p" in href_lower or "1080p" in text_lower:
                        quality = "1080p"
                    elif "2160p" in href_lower or "4k" in text_lower:
                        quality = "4K"
                        
                    language = "Unknown"
                    if "hindi" in href_lower or "hindi" in text_lower:
                        language = "Hindi"
                    elif "english" in href_lower or "english" in text_lower:
                        language = "English"
                    elif "dual" in href_lower or "dual" in text_lower:
                        language = "Dual Audio"

                    if len(text) <= 5 or "brute-force" in text.lower():
                        url_slug = href.split("/")[-1] or href.split("/")[-2] or "Media File"
                        extracted_name = url_slug[:40].replace("-", " ").replace("_", " ")
                    else:
                        extracted_name = text

                    final_name = f"{extracted_name} [{quality}] [{language}]".strip()
                    
                    await repo.add_link(name=final_name, url=href, added_by=update.effective_user.id)
                    saved_count += 1
                
                if saved_count > 0:
                    await session.commit()
                    await status_msg.edit_text(f"🔥 **Super Bulk Success!** Bot ne total **{saved_count}** links Supabase database me save kar di hain!")
                    return

            # Fallback
            fallback_text = (await client.get(clean_url)).text
            fallback_urls = set(BRUTE_FORCE_URL_REGEX.findall(fallback_text))
            
            async with AsyncSessionLocal() as session:
                repo = LinkRepository(session)
                for f_url in fallback_urls:
                    if not any(x in f_url.lower() for x in ["youtube", "facebook", "googleads"]):
                        url_slug = f_url.split("/")[-1] or "Bulk Node Resource"
                        await repo.add_link(name=url_slug[:40], url=f_url, added_by=update.effective_user.id)
                        saved_count += 1
                
                if saved_count > 0:
                    await session.commit()
                    await status_msg.edit_text(f"✅ **Super Brute-Force Done!** Total **{saved_count}** core links database me dump kar di gayi hain!")
                else:
                    await repo.add_link(name="Root Target Node", url=clean_url, added_by=update.effective_user.id)
                    await session.commit()
                    await status_msg.edit_text("⚠️ Main target securely save ho gaya hai.")
                    
    except Exception as e:
        logger.error("bulk_bruteforce_failed", error=str(e))
        await status_msg.edit_text(f"❌ Automation Failure: {str(e)}")


# ---------------------------------------------------------------------------
# Command / Handler To View ALL Stored Links (Aapka Naya Command)
# ---------------------------------------------------------------------------
async def list_all_stored_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command handler for admin to look at every link present inside the repo"""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Yeh command sirf bot owner ke liye hai.")
        return

    status_msg = await update.message.reply_text("📊 Database se saari links fetch ho rahi hain...")
    
    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        all_records = await repo.search("") # Khali query matlab sab pull karega
        
    if not all_records:
        await status_msg.edit_text("📭 Database bilkul khali hai. Koi bhi link stored nahi mili.")
        return
        
    response_text = f"📋 **Total Stored Links ({len(all_records)}):**\n\n"
    for idx, item in enumerate(all_records, start=1):
        line = f"{idx}. *{item.name}*\n🔗 `{item.url}`\n\n"
        if len(response_text) + len(line) > 3900: # Telegram message character limit safety check
            await update.message.reply_text(response_text, parse_mode="Markdown", disable_web_page_preview=True)
            response_text = ""
        response_text += line
        
    if response_text:
        await update.message.reply_text(response_text, parse_mode="Markdown", disable_web_page_preview=True)
        
    await status_msg.delete()


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
        await status_msg.edit_text(f"🚀 **Pushed to GitHub!**\nCommit: `{commit_sha[:7]}`")
        asyncio.create_task(watch_deploy_and_notify(context.bot, update.effective_chat.id, commit_sha))
    except Exception as e:
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
        await update.message.reply_text(response.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text("⚠️ Dubara try karein.")


# ---------------------------------------------------------------------------
# Ads Bypass Engine & Callback Handlers
# ---------------------------------------------------------------------------
async def _bypass_website_ads_engine(original_url: str, quality: str) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(str(original_url).strip())
            soup = BeautifulSoup(resp.text, "html.parser")
            all_links = soup.find_all("a", href=True)
            for link in all_links:
                href = str(link["href"]).strip()
                text = str(link.get_text()).lower()
                if quality in text or (quality in href.lower() and "download" in text):
                    return href
            for link in all_links:
                if any(x in str(link["href"]).lower() for x in ["drive", "mega", "download", "gdrive"]):
                    return str(link["href"]).strip()
    except Exception:
        pass
    return original_url


async def quality_button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    _, link_id, quality = data.split("_")
    
    await query.edit_message_text(text=f"🔄 {quality} ke liye website se ads bypass kiye ja rahe hain...")

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
        f"🚀 *⚡ Clean Download Link:* {direct_download_url}"
    )
    await query.edit_message_text(text=delivery_message, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Core Entrypoint (With Fuzzy/Spelling Auto-Correction System)
# ---------------------------------------------------------------------------
async def core_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""

    # Agar normal string direct command /all jaisa text hai to redirection set karo
    if user_text.strip() == "/all" or user_text.strip() == "/links":
        await list_all_stored_links_command(update, context)
        return

    if _is_admin(update):
        url_match = URL_REGEX.search(user_text)
        if url_match:
            url = url_match.group(0)
            remainder = (user_text[:url_match.start()] + user_text[url_match.end():]).strip(" -:|\n")
            if remainder and "scan" in remainder.lower() or not remainder:
                await _deep_scrape_and_store_website(update, url)
            else:
                await _store_link_from_message(update, url, remainder)
            return

        intent = await classify_intent(user_text)
        if intent == "CODE_TASK":
            await _handle_owner_code_task(update, context, user_text)
        else:
            await _handle_ai_chat(update, user_text)
        return

    # Non-owner fuzzy search database matching mechanism (Fuzzy Corrector)
    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        matches = await repo.search(user_text)
        
        if not matches:
            all_links = await repo.search("") 
            link_names = [m.name for m in all_links]
            
            # 45% matching threshold: handles extreme typos like 'Nroto' -> 'Naruto'
            closest_matches = difflib.get_close_matches(user_text, link_names, n=5, cutoff=0.45)
            if closest_matches:
                matches = []
                for name in closest_matches:
                    for l in all_links:
                        if l.name == name and l not in matches:
                            matches.append(l)

    if matches:
        await update.message.reply_text(f"🔎 *{len(matches)} results* mile (Auto-Corrected)! Quality chuney:", parse_mode="Markdown")
        for m in matches:
            keyboard = [[
                InlineKeyboardButton("🎥 480p", callback_data=f"bypass_{m.id}_480p"),
                InlineKeyboardButton("🎥 720p", callback_data=f"bypass_{m.id}_720p"),
                InlineKeyboardButton("🎥 1080p", callback_data=f"bypass_{m.id}_1080p")
            ]]
            await update.message.reply_text(f"🎬 *{m.name}*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await _handle_ai_chat(update, user_text)