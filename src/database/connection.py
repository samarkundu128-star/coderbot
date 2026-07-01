import sys
from typing import AsyncGenerator
import structlog
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.config.settings import settings
from src.errors.handlers import DatabaseTransactionError

# Logging setup kar rahe hain errors track karne ke liye
logger = structlog.get_logger(__name__)

# --- AUTOMATIC DATABASE URL PROTOCOL FIXER ---
raw_url = settings.DATABASE_URL

# Agar settings se normal string na mile (Pydantic SecretStr ho)
if hasattr(raw_url, "get_secret_value"):
    raw_url = raw_url.get_secret_value()

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

try:
    # Supabase PostgreSQL se asynchronous engine connect kar rahe hain
    engine = create_async_engine(
        db_url,                  # Fixed dynamic async URL use kar rahe hain
        pool_pre_ping=True,      # Connection check karne ke liye ping bhejna
        pool_size=20,            # Maximum active connections
        max_overflow=10,         # Extra connections limits
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
