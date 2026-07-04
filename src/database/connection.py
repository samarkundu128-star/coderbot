import sys
import asyncio
import socket
from urllib.parse import urlsplit, urlunsplit
from typing import AsyncGenerator
import structlog
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.config.settings import settings
from src.errors.handlers import DatabaseTransactionError

# Logging setup kar rahe hain errors track karne ke liye
logger = structlog.get_logger(__name__)

# --- AUTOMATIC DATABASE URL PROTOCOL FIXER ---
# SecretStr se string nikal rahe hain safely
raw_url = settings.DATABASE_URL.get_secret_value() if hasattr(settings.DATABASE_URL, "get_secret_value") else settings.DATABASE_URL

# Protocol ko ensure karein ki sirf asyncpg driver hi use ho
if raw_url.startswith("postgres://"):
    db_url = raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif raw_url.startswith("postgresql://"):
    db_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif "postgresql+psycopg2://" in raw_url:
    db_url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
else:
    db_url = raw_url
# ---------------------------------------------

# --- GETADDRINFO THREAD-POOL BYPASS ---
# Deployment logs se ye confirm hua ki hostname/IP DNS resolution jab MAIN
# THREAD se synchronously ki jaati hai, wo HAMESHA successfully hoti hai — lekin
# jab wahi resolution asyncio ke default background thread-pool executor se
# (jise asyncpg/asyncio internally `loop.getaddrinfo()` ke through use karte
# hain) hoti hai, wo CONSISTENTLY fail hoti hai — chahe hostname ho ya seedha
# numeric IP. Isse pata chalta hai ki asli problem DNS content ka nahi, balki
# is Render container me background-thread-pool se getaddrinfo() call karne
# ka mechanism hi broken hai (jaisa gVisor-jaise sandboxed environments me
# kabhi-kabhi dekha jata hai).
#
# Fix: event loop ka `getaddrinfo` seedha main thread par (synchronously, executor
# thread-pool ko bypass karke) call karne ke liye monkeypatch kar dete hain. Isse
# har getaddrinfo() call reliable, already-proven-working code path use karega.
import asyncio as _asyncio_patch

async def _main_thread_getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
    return socket.getaddrinfo(host, port, family, type, proto, flags)

_asyncio_patch.base_events.BaseEventLoop.getaddrinfo = _main_thread_getaddrinfo
logger.warning("getaddrinfo() ko background thread-pool se bypass karke main-thread par force kar diya gaya (Render DNS workaround).")
# ---------------------------------------------

# --- DEEP DEBUG + IP PRE-RESOLUTION ---
# Render jaise sandboxed containers me ek strange-lekin-known behavior dekha gaya:
# hostname jab MAIN THREAD se synchronously resolve kiya jata hai (jaise yahan
# neeche), toh hamesha successfully resolve hota hai. Lekin jab wahi hostname
# baad me asyncio ke background thread-pool executor se (jo asyncpg internally
# `loop.getaddrinfo()` ke through use karta hai) resolve hone ki koshish hoti
# hai, wo consistently "[Errno -2] Name or service not known" de kar fail ho
# jaata hai — chahe kitni bhi baar retry karo.
#
# Fix: hostname ko yahin, main thread par, ek baar reliably resolve karke uski
# IP address seedha DB connection URL me daal dete hain. Numeric IP ke liye
# getaddrinfo() koi real DNS/network query nahi karta (locally hi resolve ho
# jaata hai), isliye runtime par asyncpg/asyncio ko dobara DNS lookup karne ki
# zaroorat hi nahi padti — aur wo problematic background-thread DNS path
# poori tarah bypass ho jaata hai.
try:
    parsed = urlsplit(db_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    hostname = parsed.hostname
    port = parsed.port or 5432

    logger.warning(
        "DATABASE_URL DEBUG — parsed hostname",
        hostname_repr=repr(hostname),
        hostname_length=len(hostname) if hostname else 0,
        port=port,
        username=parsed.username,
        dbname=parsed.path.lstrip("/") if parsed.path else None,
    )

    if hostname:
        try:
            resolved = socket.getaddrinfo(hostname, port, family=socket.AF_INET)
            resolved_ip = resolved[0][4][0]
            logger.warning(
                "DATABASE_URL DEBUG — DNS resolution SUCCESS, IP ko URL me inject kar rahe hain",
                hostname=hostname,
                resolved_ip=resolved_ip,
            )

            # Netloc rebuild karte hain: hostname ki jagah resolved IP, baaki
            # (username, password, port) bilkul waise hi rakhte hain.
            userinfo = ""
            if parsed.username:
                userinfo = parsed.username
                if parsed.password:
                    userinfo += f":{parsed.password}"
                userinfo += "@"
            new_netloc = f"{userinfo}{resolved_ip}:{port}"
            new_parsed = parsed._replace(netloc=new_netloc)
            resolved_plain_url = urlunsplit(new_parsed)
            db_url = resolved_plain_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        except socket.gaierror as dns_err:
            logger.error(
                "DATABASE_URL DEBUG — DNS resolution FAILED, original hostname hi use karenge",
                hostname_repr=repr(hostname),
                error=str(dns_err),
            )
except Exception as debug_err:
    logger.warning("DATABASE_URL DEBUG/IP-resolution block itself failed — original db_url hi use karenge", error=str(debug_err))
# ---------------------------------------------

try:
    # Supabase PostgreSQL se asynchronous engine connect kar rahe hain

    # --- FINAL CONFIRMATION LOG: SQLAlchemy khud is db_url ko kaise parse
    # karega, wahi yahan dikhate hain — taaki 100% confirm ho ki asyncpg ko
    # kaunsa exact host milne wala hai (hostname ya IP), guesswork nahi. ---
    try:
        from sqlalchemy.engine.url import make_url
        _sa_url = make_url(db_url)
        logger.warning(
            "FINAL CHECK — SQLAlchemy engine ko ye exact host milega",
            final_host_repr=repr(_sa_url.host),
            final_port=_sa_url.port,
            final_database=_sa_url.database,
            final_username=_sa_url.username,
            final_drivername=_sa_url.drivername,
        )
    except Exception as final_check_err:
        logger.warning("FINAL CHECK block fail ho gaya", error=str(final_check_err))

    engine = create_async_engine(
        db_url,                  # Fixed dynamic async URL use kar rahe hain
        pool_pre_ping=True,      # Connection check karne ke liye ping bhejna
        pool_size=20,            # Maximum active connections
        max_overflow=10,         # Extra connections limits
        pool_recycle=280,        # Managed Postgres (Supabase/Render) idle connections ko ~300s
                                  # par silently drop kar dete hain — isse pehle hi refresh kar dete hain,
                                  # taaki "kuch ghante baad bot response nahi deta" wali dikkat na aaye
        echo=False
    )
except Exception as engine_init_err:
    logger.critical("Sqlalchemy engine setup fail ho gaya!", error=str(engine_init_err))
    sys.exit(1)

# Session maker instantiate kar rahe hain database transactions ke liye
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# API requests ke lifecycle ke liye Async Session yield karne wala helper
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit() # Har badlav commit (save) ho jayega automatically
        except Exception as transaction_exc:
            await session.rollback() # Kuch galat hone par purana database state wapas aa jayega
            logger.error("Database error! Session rollback kar di gayi hai.", error=str(transaction_exc))
            raise DatabaseTransactionError(f"Database task failed: {str(transaction_exc)}")
        finally:
            await session.close() # Session band ho jayegi taaki leak na ho


async def _raw_asyncpg_diagnostic():
    """
    SQLAlchemy ko poori tarah bypass karke, seedha asyncpg.connect() se ek
    diagnostic connection try karta hai — sirf logging ke liye. Isse pata
    chalega ki asli fail SQLAlchemy ke URL/dialect layer me ho raha hai ya
    asyncpg driver me hi.
    """
    try:
        import asyncpg
    except Exception as import_err:
        logger.warning("Raw asyncpg diagnostic — asyncpg import hi fail ho gaya", error=str(import_err))
        return

    try:
        from sqlalchemy.engine.url import make_url
        u = make_url(db_url)
        logger.warning(
            "Raw asyncpg diagnostic — connect try kar rahe hain",
            host_repr=repr(u.host),
            port=u.port,
        )
        conn = await asyncpg.connect(
            host=u.host,
            port=u.port,
            user=u.username,
            password=u.password,
            database=u.database,
            timeout=10,
        )
        await conn.close()
        logger.warning("Raw asyncpg diagnostic — CONNECT SUCCESS (bina SQLAlchemy ke)")
    except Exception as raw_err:
        logger.warning(
            "Raw asyncpg diagnostic — CONNECT FAILED (bina SQLAlchemy ke)",
            error=str(raw_err),
            error_type=type(raw_err).__name__,
            error_repr=repr(raw_err),
        )


async def init_db_schema(max_retries: int = 5, base_delay_seconds: float = 2.0):
    """
    Startup par (FastAPI lifespan ke andar) call hota hai. Koi Alembic migration
    system nahi hai is project me, isliye missing tables (jaise naya LinkAsset)
    ko safely auto-create kar deta hai. Existing tables ko touch nahi karta.

    --- RETRY-WITH-BACKOFF ---
    Render par container cold-boot hone ke turant baad kabhi-kabhi outbound DNS
    resolver 1-2 second ke liye fully ready nahi hota, isliye pehla hi DB connect
    attempt "[Errno -2] Name or service not known" jaisi transient error de sakta
    hai — jabki hostname khud sahi/valid hota hai (thodi der baad resolve ho jaata
    hai). Isliye ek hi attempt par give up karne ke bajaye, chhoti exponential
    backoff ke saath dobara try karte hain, taaki genuine transient startup
    hiccups se schema-creation permanently fail na ho.
    """
    from src.database.models import Base

    await _raw_asyncpg_diagnostic()

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info(
                "Database schema verified/created successfully.",
                attempt=attempt,
            )
            return
        except Exception as e:
            last_error = e
            if attempt == max_retries:
                break
            delay = base_delay_seconds * (2 ** (attempt - 1))  # 2s, 4s, 8s, 16s...
            logger.warning(
                "Database schema auto-create attempt failed — retrying.",
                attempt=attempt,
                max_retries=max_retries,
                retry_in_seconds=delay,
                error=str(e),
                error_type=type(e).__name__,
                error_repr=repr(e),
                error_cause=repr(e.__cause__) if e.__cause__ else None,
            )
            await asyncio.sleep(delay)

    logger.error(
        "Database schema auto-create failed after all retries!",
        attempts=max_retries,
        error=str(last_error),
        error_type=type(last_error).__name__ if last_error else None,
        error_repr=repr(last_error),
        error_cause=repr(last_error.__cause__) if last_error and last_error.__cause__ else None,
    )
