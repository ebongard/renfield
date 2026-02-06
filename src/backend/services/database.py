"""
Datenbank Service
"""
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.database import Base
from utils.config import settings

# Async Engine erstellen
engine = create_async_engine(
    settings.database_url.replace("postgresql://", "postgresql+asyncpg://"),
    echo=False,
    future=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=True,
)

# Session Factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def init_db():
    """Datenbank initialisieren und Tabellen erstellen"""
    try:
        async with engine.begin() as conn:
            # Ensure pgvector extension exists before creating tables with vector columns
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Datenbank-Tabellen erstellt")
    except Exception as e:
        logger.error(f"❌ Fehler beim Initialisieren der Datenbank: {e}")
        raise

async def get_db():
    """Dependency für FastAPI Endpoints"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
