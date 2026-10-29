"""Measure: eight concurrent queries on ONE AsyncSession -- cold, warm, separate.

Measures the project's REAL session factory (`services.database`), only against
the test database. The connection string is read from the environment inside
the container and never printed.
"""
import asyncio
import os
import time

# Test database, never a live one. Set before the import so the factory picks
# it up.
raw = os.environ["DATABASE_URL"]
base, _, _ = raw.rpartition("/")
os.environ["DATABASE_URL"] = f"{base}/renfield_test"

from sqlalchemy import text                              # noqa: E402
from services.database import AsyncSessionLocal          # noqa: E402

N = 8   # as many as `PolymorphicAtomStore.query` fans out


async def one_query(session, i):
    # Cheap, but a real round trip -- like each of the eight retrieval sources.
    # No binding, no cast: `i` is a number we produce ourselves. The previous
    # version failed in the driver and made ALL THREE regimes look broken,
    # including the one that has to work.
    r = await session.execute(text(f"SELECT {int(i)} AS n, pg_sleep(0.05)"))
    return r.first()[0]


async def regime(name, *, warm, own_session):
    t0 = time.perf_counter()
    if own_session:
        async def leg(i):
            async with AsyncSessionLocal() as s:
                return await one_query(s, i)
        results = await asyncio.gather(*(leg(i) for i in range(N)),
                                       return_exceptions=True)
    else:
        async with AsyncSessionLocal() as s:
            if warm:
                await s.execute(text("SELECT 1"))   # establish the connection
            results = await asyncio.gather(*(one_query(s, i) for i in range(N)),
                                           return_exceptions=True)
    duration = time.perf_counter() - t0

    # `BaseException`, not `Exception` -- same as the production code.
    # `asyncio.CancelledError` has inherited from `BaseException` since 3.8;
    # with `Exception` a CANCELLED leg showed up as delivered, and this is the
    # tool that produced the numbers 1/8, 8/8, 8/8.
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    kinds: dict[str, int] = {}
    for f in failed:
        kinds[type(f).__name__] = kinds.get(type(f).__name__, 0) + 1
    print(f"{name:34}  {len(ok)}/{N} delivered   {duration*1000:7.0f} ms   "
          f"{kinds or ''}")
    if failed:
        # Only the KIND, not the message. The docstring promises never to print
        # the connection string -- and on a connection error the message
        # carries the user or the host (asyncpg: `password authentication
        # failed for user "..."`). Exactly the case this script exists for.
        print(f"{'':36}  first kind: {type(failed[0]).__name__}")
    return len(ok), duration


async def main():
    print(f"N = {N} concurrent queries, ~50 ms server time each\n")
    for i in range(3):
        print(f"--- run {i+1} ---")
        await regime("A) ONE session, cold", warm=False, own_session=False)
        await regime("B) ONE session, pre-warmed", warm=True, own_session=False)
        await regime("C) one session per leg", warm=False, own_session=True)
        print()

asyncio.run(main())
