"""SQLAlchemy 异步引擎与会话。连接池刻意开小：小内存机器上连接数是稀缺资源。"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.dsn,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,   # 连接被中间网络掐断后自动重连，避免半夜报 invalid connection
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
