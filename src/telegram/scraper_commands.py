import io
import re
from pathlib import Path
from typing import List

import httpx
import structlog
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)

# Regex pattern to match downloadable file extensions
DOWNLOAD_EXTENSIONS = re.compile(r"\.(zip|apk|pdf|exe)$", re.IGNORECASE)
DOWNLOAD_KEYWORD = re.compile(r"download", re.IGNORECASE)


async def _fetch_html(url: str) -> str:
    """Fetch HTML content of the given URL using an async httpx client."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


def _extract_links(html: str, base_url: str) -> List[str]:
    """Parse HTML and return filtered download links."""
    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        # Resolve relative URLs
        full_url = httpx.URL(href, base_url=base_url).join(base_url).human_repr()
        if DOWNLOAD_EXTENSIONS.search(full_url) or DOWNLOAD_KEYWORD.search(full_url):
            links.append(full_url)

    return links


def _create_text_file(links: List[str]) -> InputFile:
    """Create an in‑memory text file containing the list of links."""
    buffer = io.BytesIO()
    content = "\n".join(links)
    buffer.write(content.encode("utf-8"))
    buffer.seek(0)
    return InputFile(buffer, filename="download_links.txt")


async def getlinks_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /getlinks command.

    Expected usage:
        /getlinks <website_url>

    The handler fetches the page, extracts download‑related links, writes them to a
    temporary text file and sends that file back to the user.
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /getlinks <website_url>\nExample: /getlinks https://example.com"
        )
        return

    url = context.args[0]
    logger.info("Processing /getlinks", url=url, user_id=update.effective_user.id)

    try:
        html = await _fetch_html(url)
        links = _extract_links(html, base_url=url)

        if not links:
            await update.message.reply_text("No downloadable links were found on the page.")
            return

        txt_file = _create_text_file(links)
        await update.message.reply_document(
            document=txt_file,
            caption=f"Found {len(links)} downloadable link(s) from {url}",
        )
        logger.info(
            "Sent download links file",
            url=url,
            link_count=len(links),
            user_id=update.effective_user.id,
        )
    except httpx.HTTPError as exc:
        logger.error("Failed to fetch URL", url=url, error=str(exc), user_id=update.effective_user.id)
        await update.message.reply_text(f"Failed to fetch the URL: {exc}")
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error in getlinks_command_handler", error=str(exc))
        await update.message.reply_text("An unexpected error occurred while processing your request.")
