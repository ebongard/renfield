"""Who is holding the test database? Names no credentials."""
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 🛑 The TEST database, never the live one. `pg_stat_activity` is
# cluster-wide, so there is no reason to connect to production -- and its
# sibling bin/test-db-stale-rows.py already does it this way.
URL = (os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/renfield_test").replace(
    "postgresql://", "postgresql+asyncpg://", 1
)


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
