import re
import json
import asyncio
import difflib
import urllib.parse
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

# ⚠️ APNI DEFAULT WEBSITE KA BASE URL YAHA SET KAREIN
# Agar koi user bina website ke sirf naam likhega, toh bot is site par dhoondhega.
DEFAULT_TARGET_WEBSITE = "https://gplinks.com" 

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
# Brute-Force Raw Text Tokenizer (Hidden Variable & JavaScript Array Extractor)
# ---------------------------------------------------------------------------
async def _recursive_link_extractor(client: httpx.AsyncClient, current_url: str, depth: int = 0, visited_urls: set = None) -> list:
    if visited_urls is None:
        visited_urls = set()
        
    if depth > 15 or current_url in visited_urls:
        return []
        
    visited_urls.add(current_url)
    links_found = []
    
    try:
        await asyncio.sleep(0.3)
        resp = await client.get(current_url)
        if resp.status_code != 200:
            return []
            
        page_content = resp.text
        
        # Extractor Step: Pull any URL from raw code matching stream networks
        all_raw_strings = BRUTE_FORCE_URL_REGEX.findall(page_content)
        for raw_url in all_raw_strings:
            rurl_lower = raw_url.lower()
            if any(x in rurl_lower for x in ["googleads", "doubleclick", "facebook.com", "twitter.com", "instagram.com", "youtube.com", "youtu.be"]):
                continue
                
            if raw_url not in visited_urls:
                if any(x in rurl_lower for x in ["drive", "mega", "mediafire", "pixeldrain", "gdrive", "terabox", "zippyshare", "gplinks", "droplink", "link", "download", "movie", "anime", "series"]):
                    links_found.append((raw_url, "Stream Route"))

        soup = BeautifulSoup(page_content, "html.parser")
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


# ---------------------------------------------------------------------------
# DYNAMIC MULTI-WEBSITE LIVE SEARCH ENGINE
# ---------------------------------------------------------------------------
async def _live_website_search_scraper(query_text: str, target_base_url: str) -> list:
    """
    Website base URL aur query text lekar live network hit marta hai 
    aur on-the-fly bulk data elements extract karta hai.
    """
    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }
    
    clean_base = target_base_url.rstrip("/")
    encoded_query = urllib.parse.quote_plus(query_text)
    
    # Standard query search URL payload builder (?s=query)
    search_url = f"{clean_base}/?s={encoded_query}"
    
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers=headers) as client:
            resp = await client.get(search_url)
            if resp.status_code != 200:
                return []
                
            soup = BeautifulSoup(resp.text, "html.parser")
            anchors = soup.find_all("a", href=True)
            candidate_urls = set()
            
            for a in anchors:
                href = str(a["href"]).strip()
                text = str(a.get_text()).strip()
                
                if query_text.lower() in text.lower() or query_text.lower() in href.lower():
                    if href.startswith("http") and clean_base in href:
                        candidate_urls.add((href, text if len(text) > 4 else query_text))
            
            # Deep scan top 3 search results pages for immediate recovery
            for page_url, title in list(candidate_urls)[:3]:
                raw_extracted = await _recursive_link_extractor(client, page_url)
                for href, _ in raw_extracted:
                    url_slug = href.split("/")[-1] or "Media Target Node"
                    clean_title = f"{title} - {url_slug[:25]}".replace("-", " ").replace("_", " ")
                    
                    class MockLinkItem:
                        def __init__(self, name, url, id_val):
                            self.name = name
                            self.url = url
                            self.id = id_val
                            
                    mock_id = abs(hash(href)) % 1000000
                    results.append(MockLinkItem(clean_title, href, mock_id))
                    
    except Exception as e:
        logger.error("live_dynamic_search_failed", error=str(e))
        
    return results


async def _deep_scrape_and_store_website(update: Update, target_url: str):
    status_msg = await update.message.reply_text("🚀 **Bulk Scanner Active!** Webpage se saari links dhoondhi ja rahi hain...")
    try:
        clean_url = str(target_url).strip()
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=90, headers=headers) as client:
            raw_extracted_links = await _recursive_link_extractor(client, clean_url)
            unique_links = {href: text for href, text in raw_extracted_links}
            
            saved_count = 0
            async with AsyncSessionLocal() as session:
                repo = LinkRepository(session)
                for href, text in unique_links.items():
                    url_slug = href.split("/")[-1] or "Media File"
                    final_name = text if len(text) > 5 else url_slug[:40]
                    await repo.add_link(name=final_name, url=href, added_by=update.effective_user.id)
                    saved_count += 1
                if saved_count > 0:
                    await session.commit()
                    await status_msg.edit_text(f"🔥 **Super Bulk Success!** Total **{saved_count}** links database me save ho gayi hain!")
                    return
            await status_msg.edit_text("⚠️ No links extracted.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")


async def list_all_stored_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        all_records = await repo.search("")
    if not all_records:
        await update.message.reply_text("📭 Database khali hai.")
        return
    response_text = f"📋 **Total Stored Links ({len(all_records)}):**\n\n"
    for idx, item in enumerate(all_records, start=1):
        response_text += f"{idx}. *{item.name}*\n🔗 `{item.url}`\n\n"
    await update.message.reply_text(response_text[:4000], parse_mode="Markdown", disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Core Message Router (Dono Normal aur Command Search ko sambhalti hai)
# ---------------------------------------------------------------------------
async def core_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""
    user_text_stripped = user_text.strip()

    if user_text_stripped in ["/all", "/links"]:
        await list_all_stored_links_command(update, context)
        return

    # Multi-Website Dynamic Command Check (`/search https://site.com naruto`)
    is_explicit_search_command = user_text_stripped.startswith("/search")
    chosen_website_base = DEFAULT_TARGET_WEBSITE
    search_query = user_text_stripped

    if is_explicit_search_command:
        parts = user_text_stripped.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("⚠️ **Format Galat Hai!**\nSahi tarika: `/search [website_url] [movie_name]`\n\n*Example:* `/search https://gplinks.com Naruto`")
            return
        chosen_website_base = parts[1]
        search_query = parts[2]

    if _is_admin(update) and not is_explicit_search_command:
        url_match = URL_REGEX.search(user_text)
        if url_match:
            url = url_match.group(0)
            remainder = (user_text[:url_match.start()] + user_text[url_match.end():]).strip(" -:|\n")
            if remainder and "scan" in remainder.lower() or not remainder:
                await _deep_scrape_and_store_website(update, url)
            else:
                await _store_link_from_message(update, url, remainder)
            return

    # 1. Pehle local database me check karo (Agar normal keyword query hai toh fuzzy search chalao)
    matches = []
    if not is_explicit_search_command:
        async with AsyncSessionLocal() as session:
            repo = LinkRepository(session)
            matches = await repo.search(search_query)
            
            if not matches:
                all_links = await repo.search("") 
                link_names = [m.name for m in all_links]
                closest_matches = difflib.get_close_matches(search_query, link_names, n=5, cutoff=0.45)
                if closest_matches:
                    matches = []
                    for name in closest_matches:
                        for l in all_links:
                            if l.name == name and l not in matches:
                                matches.append(l)

    # 2. Agar database me nahi mila ya explicit command hai, toh WEBSITE PAR LIVE JAAO!
    if not matches and len(search_query) > 2:
        status_searching = await update.message.reply_text(f"🔍 Live Search Engine Active! Bot ab `{chosen_website_base}` par jaakar live '{search_query}' dhoondh raha hai...")
        matches = await _live_website_search_scraper(search_query, chosen_website_base)
        await status_searching.delete()

    # Delivery response block
    if matches:
        await update.message.reply_text(f"🔎 **{len(matches)} results** mile! Download karne ke liye quality chuney:", parse_mode="Markdown")
        for m in matches:
            keyboard = [[
                InlineKeyboardButton("🎥 480p", callback_data=f"bypass_{m.id}_480p"),
                InlineKeyboardButton("🎥 720p", callback_data=f"bypass_{m.id}_720p"),
                InlineKeyboardButton("🎥 1080p", callback_data=f"bypass_{m.id}_1080p")
            ]]
            context.user_data[f"transient_{m.id}"] = m.url
            await update.message.reply_text(f"🎬 *{m.name}*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if is_explicit_search_command:
        await update.message.reply_text(f"❌ Afsos! Website `{chosen_website_base}` par '{search_query}' naam ki koi post ya link nahi mili.")
        return

    # Chat fallback
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": user_text}]
        )
        await update.message.reply_text(response.choices[0].message.content)
    except Exception:
        await update.message.reply_text("⚠️ Movie mili nahi aur server busy hai.")


# ---------------------------------------------------------------------------
# Callback Handler (Transient / Direct Node Delivery Module)
# ---------------------------------------------------------------------------
async def quality_button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, link_id, quality = query.data.split("_")
    
    await query.edit_message_text(text=f"🔄 {quality} ke liye website ads bypass ho rahe hain...")

    transient_url = context.user_data.get(f"transient_{link_id}")
    target_url = None
    target_name = "Live Scraped Media"
    
    if transient_url:
        target_url = transient_url
    else:
        async with AsyncSessionLocal() as session:
            repo = LinkRepository(session)
            link_obj = await repo.get_by_id(int(link_id))
            if link_obj:
                target_url = link_obj.url
                target_name = link_obj.name

    if not target_url:
        await query.edit_message_text(text="❌ Error: Link session expired. Dubara search karein.")
        return

    delivery_message = (
        f"✅ *Direct Download Link Ready!* ({quality})\n\n"
        f"📌 *Name:* {target_name}\n"
        f"🚀 *⚡ Clean Download Link:* {target_url}"
    )
    await query.edit_message_text(text=delivery_message, parse_mode="Markdown")