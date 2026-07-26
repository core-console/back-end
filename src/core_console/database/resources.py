"""Async SQLAlchemy resource ownership."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True, slots=True)
class DatabaseResources:
    """Engine and session factory owned by the application lifespan."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    @classmethod
    def create(cls, database_url: str) -> DatabaseResources:
        engine = create_async_engine(database_url, pool_pre_ping=True)
        return cls(
            engine=engine,
            sessions=async_sessionmaker(engine, expire_on_commit=False),
        )

    async def ping(self, timeout_seconds: float) -> None:
        """Run the smallest truthful PostgreSQL readiness query."""

        async with asyncio.timeout(timeout_seconds):
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Release the engine pool during application shutdown."""

        await self.engine.dispose()
