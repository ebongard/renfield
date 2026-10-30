"""Who is holding the test database? Names no credentials."""
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

URL = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")


async def main():
    engine = create_async_engine(URL)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT pid, state, age(now(), state_change) AS idle_for,"
            " left(coalesce(query,''), 50) AS q"
            " FROM pg_stat_activity WHERE datname = 'renfield_test'"
        ))).fetchall()
    print(f"{len(rows)} connection(s) on renfield_test:")
    for r in rows:
        print(f"   pid {r[0]} | {r[1]} | idle {str(r[2]).split('.')[0]} | {r[3]}")
    await engine.dispose()

asyncio.run(main())
