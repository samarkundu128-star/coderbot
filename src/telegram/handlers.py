import json
import asyncio
import structlog
from groq import Groq
from telegram import Update
from telegram.ext import ContextTypes

from src.config.settings import settings

logger = structlog.get_logger(__name__)

# Groq client ek baar hi initialize hoga (module load par)
groq_client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())

# NOTE: llama3-70b-8192, llama-3.3-70b-versatile, aur llama-3.1-8b-instant sab
# Groq dwara deprecate ho chuke hain (last update: 17 June 2026).
# Current recommended high-quality model: openai/gpt-oss-120b
GROQ_MODEL = "openai/gpt-oss-120b"

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


async def do_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the /do command. Usage: /do <coding task in plain language>
    Sends the task to Groq AI and returns generated, runnable code.
    """
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
        # Groq SDK synchronous hai — event loop block na ho isliye thread mein chalayein
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

        reply_text = (
            f"📄 *{filename}*\n\n"
            f"```{language}\n{code}\n```\n\n"
            f"_{explanation}_"
        )

        await thinking_msg.edit_text(reply_text, parse_mode="Markdown")
        logger.info("Code generated successfully via /do", task=user_task, filename=filename)

    except json.JSONDecodeError:
        logger.error("Groq response was not valid JSON", raw=raw_output if "raw_output" in dir() else None)
        await thinking_msg.edit_text("⚠️ AI ne galat format mein response diya. Dubara try karein.")

    except Exception as e:
        logger.error("do_command_handler failed", error=str(e))
        await thinking_msg.edit_text(f"⚠️ Kuch galat ho gaya: {str(e)}")


async def core_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles plain text messages (non-command). Currently guides the user
    to use /do for code generation tasks.
    """
    await update.message.reply_text(
        "💡 Agar aapko code chahiye, `/do` command use karein.\n\n"
        "Example: `/do python mein calculator banao`",
        parse_mode="Markdown",
    )
