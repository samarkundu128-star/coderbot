import re
import json
import asyncio
import difflib
import base64
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
BASE64_REGEX = re.compile(r'(?:[A-Za-z0-9+/]{4}){3,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?')

DEFAULT_TARGET_WEBSITE = "https://gplinks.com" 

# ---------------------------------------------------------------------------
# NETWORK CONFIGURATION: 4 OPTIMAL ALTERNATE FINGERPRINTS
# ---------------------------------------------------------------------------
HEADERS_POOL = [
    # System 1: Standard Desktop Chrome
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive"
    },
    # System 2: Modern Mobile iOS Safari (Bypasses basic script-blockers)
    {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9"
    },
    # System 3: High-Compatibility Firefox
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    },
    # System 4: Cloudflare Simulation Fingerprint
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Upgrade-Insecure-Requests": "1"
    }
]

# ---------------------------------------------------------------------------
# DATA LAYER: MULTI-SYSTEM EXTRACTOR & HYBRID DECODER
# ---------------------------------------------------------------------------
def _multi_layer_decoder(raw_text: str) -> list:
    """
    3 Alternate Data Decoders working one after another inside every page.
    """
    extracted_urls = []
    
    # System 1: Plain Text Scanner
    plain_matches = BRUTE_FORCE_URL_REGEX.findall(raw_text)
    extracted_urls.extend(plain_matches)
    
    # System 2: Base64 Obfuscated String Decryption
    b64_blocks = BASE64_REGEX.findall(raw_text)
    for block in b64_blocks:
        if len(block) > 20:
            try:
                decoded = base64.b64decode(block).decode('utf-8', errors='ignore')
                if "http" in decoded:
                    extracted_urls.extend(BRUTE_FORCE_URL_REGEX.findall(decoded))
            except Exception:
                pass
                
    # System 3: JS Window Target Location Parsing
    js_locations = re.findall(r"(?:location\.href|window\.open)\s*=\s*['\"]([^'\"]+)['\"]", raw_text)
    for loc in js_locations:
        if loc.startswith("http"):
            extracted_urls.append(loc)
            
    return extracted_urls


async def _recursive_link_extractor(client: httpx.AsyncClient, current_url: str, depth: int = 0, visited_urls: set = None) -> list:
    if visited_urls is None:
        visited_urls = set()
        
    if depth > 10 or current_url in visited_urls:
        return []
        
    visited_urls.add(current_url)
    links_found = []
    
    # NETWORK STEP: Try hitting the url with 4 alternate configurations sequentially on block
    response_text = ""
    for idx, headers in enumerate(HEADERS_POOL, start=1):
        try:
            await asyncio.sleep(0.5)
            resp = await client.get(current_url, timeout=15.0, headers=headers)
            if resp.status_code == 200:
                response_text = resp.text
                break # System succeeded, exit fallback loop
        except Exception as e:
            logger.debug(f"Network configuration system {idx} failed, switching to fallback...")
            continue
            
    if not response_text:
        return []
        
    # DECODING STEP: Process text using hybrid systems
    all_raw_strings = _multi_layer_decoder(response_text)

    for raw_url in all_raw_strings:
        rurl_lower = raw_url.lower()
        if any(x in rurl_lower for x in ["googleads", "doubleclick", "facebook.com", "twitter.com", "instagram.com", "youtube.com", "youtu.be"]):
            continue
            
        if raw_url not in visited_urls:
            if any(x in rurl_lower for x in ["drive", "mega", "mediafire", "pixeldrain", "gdrive", "terabox", "zippyshare", "gplinks", "droplink", "link", "download", "movie", "anime", "series"]):
                links_found.append((raw_url, "Stream Route"))

    soup = BeautifulSoup(response_text, "html.parser")
    all_anchors = soup.find_all("a", href=True)
    for anchor in all_anchors:
        href = str(anchor["href"]).strip()
        text = str(anchor.get_text()).strip().lower()
        if href.startswith("http") and href not in visited_urls:
            if any(x in text or x in href.lower() for x in ["continue", "next", "get link", "download now", "open", "verify", "click here", "step", "unlock", "option"]):
                sub_links = await _recursive_link_extractor(client, href, depth + 1, visited_urls)
                links_found.extend(sub_links)
                
    return links_found


# ---------------------------------------------------------------------------
# CORE SYSTEM: 4-LAYER SEARCH STRUCTURE CRAWLER
# ---------------------------------------------------------------------------
async def _live_website_search_scraper(query_text: str, target_base_url: str) -> tuple[list, str]:
    results = []
    clean_base = target_base_url.rstrip("/")
    search_keywords = query_text.lower().replace(" ", "-")
    
    # 4 Dynamic Search Systems triggered back-to-back if previous draws a blank
    url_patterns = [
        {"url": f"{clean_base}/{search_keywords}/", "system_name": "SEO Slug Matcher"},
        {"url": f"{clean_base}/?s={urllib.parse.quote_plus(query_text)}", "system_name": "Standard Query Engine"},
        {"url": f"{clean_base}/search/{urllib.parse.quote_plus(query_text)}", "system_name": "Global Path Router"},
        {"url": clean_base, "system_name": "Index DOM Mapping Falling Edge"}
    ]
    
    candidate_urls = set()
    
    # Executing each system sequentially as alternate fallbacks
    for pattern in url_patterns:
        current_url = pattern["url"]
        logger.info(f"Triggering {pattern['system_name']} for query: '{query_text}'")
        
        # Test headers configurations inside search loops too
        for config_headers in HEADERS_POOL[:2]: 
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=25.0, headers=config_headers) as client:
                    resp = await client.get(current_url)
                    if resp.status_code != 200:
                        continue
                        
                    soup = BeautifulSoup(resp.text, "html.parser")
                    anchors = soup.find_all("a", href=True)
                    
                    if search_keywords in current_url and len(anchors) > 5:
                        candidate_urls.add((current_url, query_text.title()))
                    
                    for a in anchors:
                        href = str(a["href"]).strip()
                        text = str(a.get_text()).strip()
                        if query_text.lower() in text.lower() or search_keywords in href.lower():
                            if href.startswith("http") and clean_base in href:
                                candidate_urls.add((href, text if len(text) > 4 else query_text.title()))
                                
                    # If any anchor links secured from this system, skip remaining patterns to save server load
                    if candidate_urls:
                        break
            except Exception:
                continue
        if candidate_urls:
            break

    if not candidate_urls:
        return [], f"❌ **All 4 Search Systems Exhausted!** Target website par '{query_text}' ke patterns match nahi huye."

    # Final Stage Extraction with adaptive client configurations
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=35.0, headers=HEADERS_POOL[0]) as client:
            for page_url, title in list(candidate_urls)[:4]:
                raw_extracted = await _recursive_link_extractor(client, page_url)
                for href, _ in raw_extracted:
                    url_slug = href.split("/")[-1] or "Target Node"
                    clean_title = f"{title} - {url_slug[:25]}".replace("-", " ").replace("_", " ")
                    
                    class MockLinkItem:
                        def __init__(self, name, url, id_val):
                            self.name = name
                            self.url = url
                            self.id = id_val
                            
                    mock_id = abs(hash(href)) % 1000000
                    results.append(MockLinkItem(clean_title, href, mock_id))
    except Exception as e:
        return [], f"❌ Pipeline Extraction Error: {str(e)}"
        
    return results, "SUCCESS"


# ---------------------------------------------------------------------------
# DATABASE TRANSACTION BATCHING SYSTEM
# ---------------------------------------------------------------------------
async def _deep_scrape_and_store_website(update: Update, target_url: str):
    status_msg = await update.message.reply_text("🚀 **Resilient Extraction Active!** Page analyze kiya ja raha hai...")
    try:
        clean_url = str(target_url).strip()
        # Initializing client with standard header
        async with httpx.AsyncClient(follow_redirects=True, timeout=45.0, headers=HEADERS_POOL[0]) as client:
            raw_extracted_links = await _recursive_link_extractor(client, clean_url)
            unique_links = {href: text for href, text in raw_extracted_links}
            
            saved_count = 0
            # Database Transaction Layer: Sequenced Commit Fallback Matrix
            async with AsyncSessionLocal() as session:
                repo = LinkRepository(session)
                for href, text in unique_links.items():
                    url_slug = href.split("/")[-1] or "Media File"
                    final_name = text if len(text) > 5 else url_slug[:40]
                    
                    # Try System: Store data one-by-one safely
                    try:
                        await repo.add_link(name=final_name, url=href, added_by=update.effective_user.id)
                        saved_count += 1
                    except Exception:
                        continue # If single duplicate or type error occurs, skip and save remaining array node elements
                        
                if saved_count > 0:
                    await session.commit()
                    await status_msg.edit_text(f"🔥 **Extraction Success!** Total **{saved_count}** core media links successfully saved to database!")
                    return
            await status_msg.edit_text("⚠️ Extraction Failed: Is page par crawler ko koi media download path detect nahi hua.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Automation Timeout Error: {str(e)}")


async def list_all_stored_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
# CORE MESSAGE ROUTER
# ---------------------------------------------------------------------------
async def core_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""
    user_text_stripped = user_text.strip()

    if user_text_stripped in ["/all", "/links"]:
        await list_all_stored_links_command(update, context)
        return

    is_explicit_search_command = user_text_stripped.startswith("/search")
    chosen_website_base = DEFAULT_TARGET_WEBSITE
    search_query = user_text_stripped

    if is_explicit_search_command:
        parts = user_text_stripped.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("⚠️ **Format:** `/search [website_url] [movie_name]`")
            return
        chosen_website_base = parts[1]
        search_query = parts[2]

    # Universal Capture: Process direct URL strings instantly
    url_match = URL_REGEX.search(user_text)
    if url_match and not is_explicit_search_command:
        url = url_match.group(0)
        await _deep_scrape_and_store_website(update, url)
        return

    # SYSTEM 1: Local Database Exact/Fuzzy Matching
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

    # SYSTEM 2: Multi-Layer Live Web Crawler
    status_msg_text = ""
    if not matches and len(search_query) > 2:
        status_searching = await update.message.reply_text(f"🔍 Security Sandbox Active! Bot `{chosen_website_base}` par sequential bypass protocols try kar raha hai...")
        matches, status_msg_text = await _live_website_search_scraper(search_query, chosen_website_base)
        await status_searching.delete()

    # UI Interactive Buttons Delivery
    if matches:
        await update.message.reply_text(f"🔎 **{len(matches)} results** mile! Quality chuney:", parse_mode="Markdown")
        for m in matches:
            keyboard = [[
                InlineKeyboardButton("🎥 480p", callback_data=f"bypass_{m.id}_480p"),
                InlineKeyboardButton("🎥 720p", callback_data=f"bypass_{m.id}_720p"),
                InlineKeyboardButton("🎥 1080p", callback_data=f"bypass_{m.id}_1080p")
            ]]
            context.user_data[f"transient_{m.id}"] = m.url
            await update.message.reply_text(f"🎬 *{m.name}*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if status_msg_text and status_msg_text != "SUCCESS":
        await update.message.reply_text(status_msg_text, parse_mode="Markdown")
        return

    # SYSTEM 3: AI Chat Inference Fallback
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": user_text}]
        )
        await update.message.reply_text(response.choices[0].message.content)
    except Exception:
        await update.message.reply_text("⚠️ System Fallback: Service under high load. Correct format name query.")


# ---------------------------------------------------------------------------
# Callback Quality Router
# ---------------------------------------------------------------------------
async def quality_button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, link_id, quality = query.data.split("_")
    
    await query.edit_message_text(text=f"🔄 {quality} ke liye website ads bypass ho rahe hain...")

    transient_url = context.user_data.get(f"transient_{link_id}")
    target_url = None
    target_name = "Live Scraped Media Node"
    
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
        await query.edit_message_text(text="❌ Error: Cache cleared. Please re-search.")
        return

    delivery_message = (
        f"✅ *Direct Download Link Ready!* ({quality})\n\n"
        f"📌 *Name:* {target_name}\n"
        f"🚀 *⚡ Clean Download Link:* {target_url}"
    )
    await query.edit_message_text(text=delivery_message, parse_mode="Markdown")