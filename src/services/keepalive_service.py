import structlog
import httpx
from src.config.settings import settings

logger = structlog.get_logger(__name__)


async def self_ping():
    """
    Apne hi /health endpoint ko periodically hit karta hai. Render ke free-tier
    web services ~15 min ki inactivity ke baad spin-down ho jaate hain, jisse
    agla real request slow/cold-start hota hai ya webhook silently miss ho
    jaata hai. Yeh function usi ko rokta hai.
    """
    if not settings.WEBHOOK_URL:
        return

    base = settings.WEBHOOK_URL.strip().rstrip("/")
    url = f"{base}/health"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            logger.info("Keepalive self-ping sent", status_code=resp.status_code)
    except Exception as e:
        logger.warning("Keepalive self-ping failed", error=str(e))
