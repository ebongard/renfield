"""Rows left behind in the test database by a killed run. Reports, does not delete."""
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

URL = (os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/renfield_test").replace(
    "postgresql://", "postgresql+asyncpg://", 1)


async def main():
    engine = create_async_engine(URL)
    async with engine.connect() as conn:
        tables = [r[0] for r in (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
        ))).fetchall()]
        dirty = []
        for t in tables:
            n = (await conn.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar()
            if n:
                dirty.append((t, n))
    print(f"{len(dirty)} of {len(tables)} tables hold rows:")
    for t, n in dirty:
        print(f"   {t}: {n}")
    await engine.dispose()

asyncio.run(main())
