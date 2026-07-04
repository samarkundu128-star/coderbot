import sys
import asyncio
import socket
from urllib.parse import urlsplit
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

# --- DEEP DEBUG: exact hostname jo Python ko mil raha hai wo dikhate hain (repr()
# ke saath, taaki hidden/invisible characters bhi escape-sequence ki tarah dikh
# jaayein), aur DNS resolution manually try karke exact result/error print karte hain.
# Password ko hamesha *** se mask karke rakha jaata hai logs me. ---
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
            resolved = socket.getaddrinfo(hostname, port)
            logger.warning(
                "DATABASE_URL DEBUG — DNS resolution SUCCESS",
                hostname=hostname,
                resolved_ips=[r[4][0] for r in resolved][:3],
            )
        except socket.gaierror as dns_err:
            logger.error(
                "DATABASE_URL DEBUG — DNS resolution FAILED",
                hostname_repr=repr(hostname),
                error=str(dns_err),
            )
except Exception as debug_err:
    logger.warning("DATABASE_URL DEBUG block itself failed", error=str(debug_err))
# ---------------------------------------------

try:
    # Supabase PostgreSQL se asynchronous engine connect kar rahe hain
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
            )
            await asyncio.sleep(delay)

    logger.error(
        "Database schema auto-create failed after all retries!",
        attempts=max_retries,
        error=str(last_error),
    )
