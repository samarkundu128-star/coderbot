import re
import asyncio
import httpx
import structlog
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Set, Tuple, Callable, Awaitable

from src.database.connection import AsyncSessionLocal
from src.database.repository import LinkRepository

logger = structlog.get_logger(__name__)

# Generic/junk anchor texts jo movie/file ka naam nahi hote — inhe mile toh
# fallback (heading / page title / URL) use karenge
GENERIC_ANCHOR_TEXTS = {
    "download", "download now", "download link", "download here",
    "click here", "click", "here", "link", "watch now", "watch online",
    "server 1", "server 2", "server 3", "fast server", "mirror",
    "1080p", "720p", "480p", "hd", "full hd", "get link", "continue",
    "download link 1", "download link 2", "free download",
}

# Quality/size jaisa text — inhe movie naam ke saath jodna hai, pura discard nahi karna
QUALITY_HINTS = re.compile(r"^\s*(480p|720p|1080p|2160p|4k|hd|full hd|cam|hdrip|webrip|bluray)\s*$", re.IGNORECASE)

# --- Crawl batching settings ---
# Ek baar mein itne pages fetch karke DB me save karte hain, phir thoda rukte
# hain — isse memory/CPU spike nahi hota aur bot crash nahi hota, chahe
# website mein kitne bhi pages hon.
PAGES_PER_BATCH = 30
BATCH_PAUSE_SECONDS = 60  # 1 minute rukhna har batch ke baad
# Poori website chahe kitni badi ho, itne pages ke baad crawl rukk jayega —
# ye ek safety-valve hai taaki koi bahut badi/infinite site bot ko hang na kare.
MAX_TOTAL_PAGES = 500
MAX_CRAWL_DEPTH = 3

# In extensions wale links ko "page" maan kar crawl nahi karenge (ye khud files/assets hain)
_NON_PAGE_EXTENSIONS = (
    ".zip", ".rar", ".7z", ".exe", ".msi", ".dmg", ".iso", ".apk",
    ".mp4", ".mkv", ".avi", ".mov", ".torrent",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
)

ProgressCallback = Callable[[int, int, int, int], Awaitable[None]]
# progress_callback(batch_number, pages_scanned_so_far, links_added_this_batch, total_links_added)


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return response.text


def _normalize_for_dedup(url: str) -> str:
    """
    URL ke trailing slash, query aur fragment ki wajah se same page do baar
    visit/store na ho — isliye dedup-comparison ke liye ek normalized form
    banate hain (asal URL storage/fetch ke liye waisa hi rehta hai).
    """
    u = httpx.URL(url)
    path = u.path.rstrip("/") or "/"
    return f"{u.scheme.lower()}://{(u.host or '').lower()}{path}"


def _is_same_domain(url: str, root_host: str) -> bool:
    try:
        return (httpx.URL(url).host or "").lower() == root_host.lower()
    except Exception:
        return False


def _looks_like_non_page(url: str) -> bool:
    try:
        path = httpx.URL(url).path.lower()
    except Exception:
        return True
    return path.endswith(_NON_PAGE_EXTENSIONS)


def _clean_name_from_url(url: str) -> str:
    """Last resort: URL ke filename hisse se ek readable naam banata hai."""
    path = httpx.URL(url).path
    filename = path.rsplit("/", 1)[-1] or url
    filename = filename.rsplit(".", 1)[0]  # extension hata do
    filename = re.sub(r"[._\-]+", " ", filename).strip()
    return filename or url


def _find_nearest_heading(a_tag) -> Optional[str]:
    """
    Link ke upar (pehle) sabse nazdeeki heading (h1/h2/h3) ya strong/b tag
    dhundta hai — movie-download sites me aksar movie ka naam heading me
    hota hai, aur uske neeche generic 'Download' buttons hote hain.
    """
    # Pehle apne parent container ke andar heading dhundo
    parent = a_tag.find_parent(["div", "article", "li", "section", "td", "tr"])
    if parent:
        heading = parent.find(["h1", "h2", "h3", "h4", "strong", "b"])
        if heading:
            text = heading.get_text(strip=True)
            if text and len(text) > 3:
                return text

    # Phir document me peeche ki taraf (pichla sibling ya ancestor se pehle) dhundo
    for prev in a_tag.find_all_previous(["h1", "h2", "h3"], limit=5):
        text = prev.get_text(strip=True)
        if text and len(text) > 3:
            return text

    return None


def _extract_named_links(html: str, base_url: str) -> List[Dict[str, str]]:
    """
    Har <a> tag se URL nikalta hai aur uska sabse sahi "naam" decide karta hai:
    1. Agar anchor text hi descriptive hai (generic word jaisa nahi), wahi use karo.
    2. Nahi to, us link ke sabse nazdeeki heading (jaise movie title) ko use karo.
    3. Nahi to, page ka <title> use karo.
    4. Aakhri fallback: URL ke filename se readable naam banao.
    """
    soup = BeautifulSoup(html, "html.parser")
    page_title = soup.title.get_text(strip=True) if soup.title else None

    items: List[Dict[str, str]] = []
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue

        full_url = httpx.URL(base_url).join(href).human_repr()
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        anchor_text = a_tag.get_text(strip=True)
        title_attr = (a_tag.get("title") or "").strip()

        candidate = anchor_text or title_attr
        is_generic = not candidate or candidate.strip().lower() in GENERIC_ANCHOR_TEXTS or len(candidate) < 4
        is_quality_only = bool(candidate) and QUALITY_HINTS.match(candidate)

        if is_quality_only:
            # Anchor text sirf quality bata raha hai (jaise "720p") — movie naam ke
            # saath jod do taaki alag-alag quality wale links distinguish ho sakein
            base_name = _find_nearest_heading(a_tag) or page_title or _clean_name_from_url(full_url)
            name = f"{base_name} ({candidate.strip()})"
        elif not is_generic:
            name = candidate
        else:
            name = _find_nearest_heading(a_tag) or page_title or _clean_name_from_url(full_url)

        items.append({"name": name, "url": full_url})

    return items


def _find_internal_page_links(html: str, current_url: str, root_host: str) -> List[str]:
    """
    Us page ke andar se aage crawl karne layak "internal" page links dhoondta
    hai — sirf same-domain, aur wo links jo khud koi file/asset na ho.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        try:
            next_url = httpx.URL(current_url).join(href).human_repr()
        except Exception:
            continue
        if _is_same_domain(next_url, root_host) and not _looks_like_non_page(next_url):
            found.append(next_url)
    return found


async def _crawl_in_batches(start_url: str):
    """
    Async generator: website ko BFS tarike se crawl karta hai, lekin
    PAGES_PER_BATCH pages fetch karne ke baad ek batch ("items ki list")
    yield karta hai aur BATCH_PAUSE_SECONDS ke liye rukta hai (agar aur
    pages baaki hon). Isse:
      - Memory/CPU spike nahi hota (bot crash nahi hota)
      - Progress incrementally save ho sakta hai (kuch fail ho bhi jaye
        toh pehle wale batches ka data DB me safe rehta hai)

    Overall MAX_TOTAL_PAGES tak hi jayega — bahut badi/infinite site se
    bot ko bachane ke liye.
    """
    root_host = httpx.URL(start_url).host or ""
    visited: Set[str] = set()
    queue: List[Tuple[str, int]] = [(start_url, 0)]
    total_pages_fetched = 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        while queue and total_pages_fetched < MAX_TOTAL_PAGES:
            batch_items: List[Dict[str, str]] = []
            pages_in_this_batch = 0

            while queue and pages_in_this_batch < PAGES_PER_BATCH and total_pages_fetched < MAX_TOTAL_PAGES:
                url, depth = queue.pop(0)
                norm = _normalize_for_dedup(url)
                if norm in visited:
                    continue
                visited.add(norm)

                try:
                    html = await _fetch_html(client, url)
                except Exception as e:
                    if total_pages_fetched == 0 and pages_in_this_batch == 0:
                        # Pehla/starting page fail hua — ye asli error hai, upar report karo
                        raise
                    logger.warning("Website sync: ek page fetch nahi ho payi, skip kar rahe hain", url=url, error=str(e))
                    continue

                total_pages_fetched += 1
                pages_in_this_batch += 1
                batch_items.extend(_extract_named_links(html, base_url=url))

                if depth < MAX_CRAWL_DEPTH:
                    for next_url in _find_internal_page_links(html, current_url=url, root_host=root_host):
                        if _normalize_for_dedup(next_url) not in visited:
                            queue.append((next_url, depth + 1))

            if pages_in_this_batch > 0:
                yield batch_items, total_pages_fetched

            if queue and total_pages_fetched < MAX_TOTAL_PAGES:
                logger.info(
                    "Website sync: batch complete, pause karke aage badhenge",
                    pages_scanned_so_far=total_pages_fetched,
                    pause_seconds=BATCH_PAUSE_SECONDS,
                )
                await asyncio.sleep(BATCH_PAUSE_SECONDS)


async def sync_website_links(
    website_url: str,
    added_by: int,
    progress_callback: Optional[ProgressCallback] = None,
) -> int:
    """
    WEBSITE_URL (aur uske andar ke saare connected pages) ko batches me scan
    karke links database me store karta hai (jo already exist nahi karte).
    Startup scheduler aur /syncwebsite command dono se call hota hai.

    Har batch (PAGES_PER_BATCH pages) ke baad turant DB me save ho jata hai
    (crash-safe / resumable progress) aur agar progress_callback diya gaya
    ho toh usse call karta hai — /syncwebsite command isse Telegram message
    live update karne ke liye use karta hai.

    Returns: total kitne naye links add hue (saare batches milakar).
    """
    if not website_url:
        return 0

    total_added = 0
    batch_number = 0

    try:
        async for batch_items, pages_scanned_so_far in _crawl_in_batches(website_url):
            batch_number += 1

            if not batch_items:
                continue

            # Isi batch ke andar duplicate URLs hata do
            seen_urls = set()
            deduped_items: List[Dict[str, str]] = []
            for item in batch_items:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                deduped_items.append(item)

            async with AsyncSessionLocal() as session:
                repo = LinkRepository(session)
                batch_added = await repo.bulk_add_links(deduped_items, added_by=added_by)
                await session.commit()

            total_added += batch_added

            logger.info(
                "Website sync: batch saved",
                url=website_url,
                batch_number=batch_number,
                pages_scanned_so_far=pages_scanned_so_far,
                batch_links_found=len(deduped_items),
                batch_links_added=batch_added,
                total_links_added=total_added,
            )

            if progress_callback is not None:
                try:
                    await progress_callback(batch_number, pages_scanned_so_far, batch_added, total_added)
                except Exception as cb_err:
                    logger.warning("Website sync: progress_callback fail hua, ignore kar rahe hain", error=str(cb_err))

        if batch_number == 0:
            logger.info("Website sync: koi link nahi mila", url=website_url)

        return total_added

    except Exception as e:
        logger.error("Website sync failed", url=website_url, error=str(e), total_links_added_before_fail=total_added)
        raise

