import asyncio
import structlog
import httpx
from src.config.settings import settings

logger = structlog.get_logger(__name__)
RENDER_API_BASE = "https://api.render.com/v1"


def _render_configured() -> bool:
    return bool(settings.RENDER_API_KEY and settings.RENDER_SERVICE_ID)


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.RENDER_API_KEY.get_secret_value()}"}


async def trigger_manual_deploy(clear_cache: bool = False) -> dict | None:
    """Render par 'Manual Deploy' trigger karta hai (RENDER_API_KEY + RENDER_SERVICE_ID zaroori)."""
    if not _render_configured():
        logger.warning("Render deploy trigger skipped — RENDER_API_KEY/RENDER_SERVICE_ID set nahi hain.")
        return None

    url = f"{RENDER_API_BASE}/services/{settings.RENDER_SERVICE_ID}/deploys"
    payload = {"clearCache": "clear" if clear_cache else "do_not_clear"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=_headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Render manual deploy triggered", deploy_id=data.get("id"))
            return data
    except Exception as e:
        logger.error("Render manual deploy trigger failed", error=str(e))
        return None


async def watch_deploy_and_notify(bot, chat_id: int, commit_sha: str | None,
                                   timeout_sec: int = 420, poll_interval: int = 10):
    """
    Background task: naya commit push hone ke baad Render ke deploy status ko
    poll karta hai aur owner ko final result (live / failed) ka message bhejta hai.
    """
    if not _render_configured():
        await bot.send_message(
            chat_id,
            "✅ GitHub par push ho gaya. (Deploy status auto-track karne ke liye "
            "RENDER_API_KEY aur RENDER_SERVICE_ID env vars add karein — Render usually "
            "1-2 minute me khud hi naya build shuru kar dega.)",
        )
        return

    url = f"{RENDER_API_BASE}/services/{settings.RENDER_SERVICE_ID}/deploys"
    elapsed = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while elapsed < timeout_sec:
            try:
                resp = await client.get(url, headers=_headers(), params={"limit": 5})
                resp.raise_for_status()
                deploys = resp.json()

                for entry in deploys:
                    deploy = entry.get("deploy", entry)
                    deploy_commit = (deploy.get("commit") or {}).get("id", "")

                    if commit_sha and not deploy_commit.startswith(commit_sha[:7]):
                        continue

                    status = deploy.get("status")

                    if status == "live":
                        await bot.send_message(
                            chat_id,
                            "✅ *Deploy ho gaya!*\nAapka naya feature ab live hai — test kar sakte hain.",
                            parse_mode="Markdown",
                        )
                        return

                    if status in ("build_failed", "update_failed", "canceled", "deactivated"):
                        await bot.send_message(
                            chat_id,
                            f"⚠️ Deploy fail ho gaya (status: `{status}`).\n"
                            "Logs Render dashboard par check karein — agar runtime crash hua "
                            "toh Auto-Healer khud fix karne ki koshish karega.",
                            parse_mode="Markdown",
                        )
                        return
            except Exception as e:
                logger.error("Deploy watch polling error", error=str(e))

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    await bot.send_message(chat_id, "⏱️ Deploy status timeout ho gaya — Render dashboard manually check kar lein.")