import re
import json
import asyncio
import structlog
import httpx
from bs4 import BeautifulSoup
from groq import Groq
from telegram import Update
from telegram.ext import ContextTypes

from src.config.settings import settings
from src.database.connection import AsyncSessionLocal
from src.database.repository import LinkRepository
from src.services.ai_engine import AICodingEngine
from src.services.github_service import push_files
from src.services.render_service import watch_deploy_and_notify
from src.services.intent_router import classify_intent

logger = structlog.get_logger(__name__)

# Groq client ek baar hi initialize hoga (module load par) — /do command ke liye
groq_client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())
GROQ_MODEL = "openai/gpt-oss-120b"

_ai_engine = AICodingEngine()

URL_REGEX = re.compile(r"https?://[^\s]+")

SYSTEM_PROMPT = """You are an elite coding assistant. When given a task, respond ONLY with a valid JSON object — no markdown fences, no extra commentary, nothing outside the JSON.

The JSON must have exactly these keys:
{
  "language": "programming language name, e.g. python",
  "filename": "suggested filename, e.g. calculator.py",
  "code": "the complete, runnable code as a single string with \\n for newlines",
  "explanation": "a short 1-3 sentence explanation of how the code works"
}

Rules:
- Code must be complete and runnable, not a snippet.
- Do not wrap the JSON in ```json fences.
- Do not add any text before or after the JSON object.
"""

# Public users ke liye — sirf teach/guide karta hai, kabhi code-push ya GitHub touch nahi karta
ASSISTANT_SYSTEM_PROMPT = """Tum "Coderbot Assistant" ho — is Telegram bot ka friendly, professional
AI guide. Tumhara kaam hai users ko bot ke features samjhana aur unki general coding queries me
madad karna. Hamesha Hinglish-friendly, simple aur encouraging tone use karo.

Rules:
- Bot ke commands (/do, /newproject, /clear, /help) explain kar sakte ho.
- General coding concepts, debugging tips, best-practices explain kar sakte ho.
- Kabhi bhi khud se code-file generate karke GitHub par push karne ka dawa mat karo — wo
  sirf bot owner ka privilege hai.
- Agar user real code chahta hai, unhe `/do <task>` use karne ko bolo.
- Chhoti, friendly replies do — lambi lecture mat do.
"""


def _is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == settings.ADMIN_TELEGRAM_ID


# ---------------------------------------------------------------------------
# /do command — legacy single-file quick code generation
# ---------------------------------------------------------------------------
async def do_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /do command. Usage: /do <coding task in plain language>"""
    user_task = " ".join(context.args) if context.args else ""

    if not user_task.strip():
        await update.message.reply_text(
            "⚠️ Usage: `/do <apna coding task likhein>`\n\n"
            "Example: `/do python mein calculator banao`",
            parse_mode="Markdown",
        )
        return

    thinking_msg = await update.message.reply_text("⏳ Code generate ho raha hai, thoda ruko...")

    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_task},
            ],
            temperature=0.3,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )

        raw_output = response.choices[0].message.content
        data = json.loads(raw_output)

        language = data.get("language", "")
        filename = data.get("filename", "code.txt")
        code = data.get("code", "")
        explanation = data.get("explanation", "")

        reply_text = f"📄 *{filename}*\n\n```{language}\n{code}\n```\n\n_{explanation}_"

        await thinking_msg.edit_text(reply_text, parse_mode="Markdown")
        logger.info("Code generated successfully via /do", task=user_task, filename=filename)

    except json.JSONDecodeError:
        logger.error("Groq response was not valid JSON")
        await thinking_msg.edit_text("⚠️ AI ne galat format mein response diya. Dubara try karein.")
    except Exception as e:
        logger.error("do_command_handler failed", error=str(e))
        await thinking_msg.edit_text(f"⚠️ Kuch galat ho gaya: {str(e)}")


# ---------------------------------------------------------------------------
# Link store: owner ke shared links ko save karna, aur kisi ke bhi liye search karna
# ---------------------------------------------------------------------------
async def _fetch_page_title(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            if soup.title and soup.title.string:
                return soup.title.string.strip()[:200]
    except Exception:
        pass
    return None


async def _store_link_from_message(update: Update, url: str, remainder_text: str):
    name = remainder_text.strip()
    if not name:
        name = await _fetch_page_title(url) or url

    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        await repo.add_link(name=name, url=url, added_by=update.effective_user.id)
        await session.commit()

    await update.message.reply_text(
        f"🔗 *Link save ho gaya!*\n\n📌 Name: `{name}`\n\nAb koi bhi user `{name}` type karega toh yeh link mil jayega.",
        parse_mode="Markdown",
    )


async def _search_and_reply_link(update: Update, query: str) -> bool:
    """Returns True agar koi matching link mila aur reply bhej diya."""
    async with AsyncSessionLocal() as session:
        repo = LinkRepository(session)
        matches = await repo.search(query)

    if not matches:
        return False

    if len(matches) == 1:
        m = matches[0]
        await update.message.reply_text(f"🔗 *{m.name}*\n{m.url}", parse_mode="Markdown")
    else:
        lines = [f"🔎 *{len(matches)} results mile:*\n"]
        for m in matches:
            lines.append(f"• *{m.name}*\n  {m.url}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    return True


# ---------------------------------------------------------------------------
# Owner natural-language code-task flow: /do ki tarah, but bina command ke
# aur multi-file support + auto GitHub push + deploy-notify ke saath
# ---------------------------------------------------------------------------
async def _handle_owner_code_task(update: Update, context: ContextTypes.DEFAULT_TYPE, instruction: str):
    status_msg = await update.message.reply_text("🧠 Samajh gaya, code prepare kar raha hoon...")

    try:
        result = await _ai_engine.generate_solution(instruction, history=[])
        files = result.get("files", [])
        commentary = (result.get("commentary") or "").strip()

        if not files:
            await status_msg.edit_text(
                "⚠️ AI koi file generate nahi kar paya. Thoda aur specific instruction dekar dubara try karein.\n\n"
                "Tip: `/upgrade <file_path> | <instructions>` bhi use kar sakte hain kisi ek specific file ke liye."
            )
            return

        await status_msg.edit_text(f"📝 {len(files)} file(s) ready ho gayi. GitHub par push kar raha hoon...")

        commit_sha = await asyncio.to_thread(push_files, files, f"🤖 AI update: {instruction[:60]}")

        file_list = "\n".join(f"• `{f.get('file_path')}`" for f in files if f.get("file_path"))
        summary = commentary[:400] + ("…" if len(commentary) > 400 else "")

        await status_msg.edit_text(
            f"🚀 *Push ho gaya!*\n{file_list}\n\n{summary}\n\n"
            "Render deploy trigger ho chuka hai, main monitor kar raha hoon...",
            parse_mode="Markdown",
        )

        context.application.create_task(
            watch_deploy_and_notify(context.bot, update.effective_chat.id, commit_sha)
        )
        logger.info("Owner natural-language code task pushed", instruction=instruction, files=len(files))

    except Exception as e:
        logger.error("owner_code_task_failed", error=str(e))
        await status_msg.edit_text(f"❌ Kuch galat ho gaya: {str(e)}")


async def _handle_ai_chat(update: Update, user_text: str):
    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
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

    # Non-owner users: pehle stored-links me search karo (movie/anime/document name),
    # phir fallback AI teaching-assistant
    found = await _search_and_reply_link(update, user_text)
    if found:
        return

    await _handle_ai_chat(update, user_text)
