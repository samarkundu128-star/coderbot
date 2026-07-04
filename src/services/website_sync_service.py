import re
import httpx
import structlog
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

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

async def _fetch_html(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


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
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        full_url = httpx.URL(href, base_url=base_url).join(base_url).human_repr()
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


async def sync_website_links(website_url: str, added_by: int) -> int:
    """
    WEBSITE_URL ko scan karke uske saare links database me store karta hai
    (jo already exist nahi karte). Startup par aur /syncwebsite command dono
    se call hota hai.

    Returns: kitne naye links add hue.
    """
    if not website_url:
        return 0

    try:
        html = await _fetch_html(website_url)
        items = _extract_named_links(html, base_url=website_url)

        if not items:
            logger.info("Website sync: koi link nahi mila", url=website_url)
            return 0

        async with AsyncSessionLocal() as session:
            repo = LinkRepository(session)
            added_count = await repo.bulk_add_links(items, added_by=added_by)
            await session.commit()

        logger.info(
            "Website sync complete",
            url=website_url,
            total_found=len(items),
            new_added=added_count,
        )
        return added_count

    except Exception as e:
        logger.error("Website sync failed", url=website_url, error=str(e))
        raise

