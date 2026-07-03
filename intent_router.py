import asyncio
import structlog
from groq import Groq
from src.config.settings import settings

logger = structlog.get_logger(__name__)

_client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())
# Chhota/fast model — sirf classification ke liye, latency kam rakhne ke liye
_MODEL = "openai/gpt-oss-20b"

_ROUTER_PROMPT = """Tum ek intent classifier ho. Bot ke OWNER ka message padhkar sirf EK WORD return karo:

- CODE_TASK: agar owner code likhne, koi feature add karne, bug fix karne, file modify/create
  karne, ya bot ke codebase me koi bhi technical change karne ko keh raha hai.
- CHAT: agar owner sirf baat kar raha hai, sawal pooch raha hai, status check kar raha hai,
  ya general guidance maang raha hai.

Sirf ek word likho: CODE_TASK ya CHAT. Kuch aur mat likhna."""


async def classify_intent(message: str) -> str:
    try:
        completion = await asyncio.to_thread(
            _client.chat.completions.create,
            model=_MODEL,
            messages=[
                {"role": "system", "content": _ROUTER_PROMPT},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=6,
        )
        result = (completion.choices[0].message.content or "").strip().upper()
        return "CODE_TASK" if "CODE_TASK" in result else "CHAT"
    except Exception as e:
        logger.error("Intent classification failed, defaulting to CHAT", error=str(e))
        return "CHAT"
