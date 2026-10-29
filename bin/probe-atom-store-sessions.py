"""Messung: acht nebenlaeufige Abrufe auf EINER AsyncSession — kalt, warm, je eigene.

Gemessen wird die ECHTE Sitzungsfabrik des Projekts (`services.database`),
nur gegen die Testbank. Die Verbindungszeichenkette wird im Container aus der
Umgebung gelesen und NIE ausgegeben.
"""
import asyncio
import os
import time

# Testbank, niemals eine lebende. Vor dem Import setzen, damit die Fabrik sie nimmt.
roh = os.environ["DATABASE_URL"]
basis, _, _ = roh.rpartition("/")
os.environ["DATABASE_URL"] = f"{basis}/renfield_test"

from sqlalchemy import text                              # noqa: E402
from services.database import AsyncSessionLocal          # noqa: E402

N = 8   # so viele Quellen verteilt `PolymorphicAtomStore.query`


async def ein_abruf(session, i):
    # Billig, aber ein echter Netzgang — wie jeder der acht Abrufzweige.
    # Keine Bindung, kein Cast — `i` ist eine von mir erzeugte Zahl. Die
    # vorige Fassung scheiterte am Treiber und liess ALLE drei Regime als
    # kaputt erscheinen, auch das, das funktionieren muss.
    r = await session.execute(text(f"SELECT {int(i)} AS n, pg_sleep(0.05)"))
    return r.first()[0]


async def regime(name, *, warm, je_eigene_sitzung):
    t0 = time.perf_counter()
    if je_eigene_sitzung:
        async def zweig(i):
            async with AsyncSessionLocal() as s:
                return await ein_abruf(s, i)
        ergebnisse = await asyncio.gather(*(zweig(i) for i in range(N)),
                                          return_exceptions=True)
    else:
        async with AsyncSessionLocal() as s:
            if warm:
                await s.execute(text("SELECT 1"))   # Verbindung herstellen
            ergebnisse = await asyncio.gather(*(ein_abruf(s, i) for i in range(N)),
                                             return_exceptions=True)
    dauer = time.perf_counter() - t0

    # 🛑 `BaseException`, nicht `Exception` — wie in der Produktionsfassung.
    # `asyncio.CancelledError` erbt seit 3.8 von `BaseException`; mit
    # `Exception` erschien ein ABGEBROCHENER Zweig als geliefert, und genau
    # dieses Werkzeug hat die Zahlen 1/8, 8/8, 8/8 erzeugt.
    ok = [r for r in ergebnisse if not isinstance(r, BaseException)]
    fehler = [r for r in ergebnisse if isinstance(r, BaseException)]
    arten = {}
    for f in fehler:
        arten[type(f).__name__] = arten.get(type(f).__name__, 0) + 1
    print(f"{name:34}  {len(ok)}/{N} geliefert   {dauer*1000:7.0f} ms   {arten or ''}")
    if fehler:
        # 🛑 Nur die ART, nicht die Meldung. Der Docstring verspricht, die
        # Verbindungszeichenkette nie auszugeben — und bei einem
        # Verbindungsfehler steht Benutzer oder Wirt in der Meldung
        # (asyncpg: `password authentication failed for user "…"`). Genau der
        # Fall, fuer den dieses Skript gebaut ist.
        print(f"{'':36}  erste Art: {type(fehler[0]).__name__}")
    return len(ok), dauer


async def main():
    print(f"N = {N} nebenlaeufige Abrufe, je ~50 ms Serverzeit\n")
    for i in range(3):
        print(f"--- Durchlauf {i+1} ---")
        await regime("A) EINE Sitzung, kalt", warm=False, je_eigene_sitzung=False)
        await regime("B) EINE Sitzung, vorgewaermt", warm=True, je_eigene_sitzung=False)
        await regime("C) je eigene Sitzung", warm=False, je_eigene_sitzung=True)
        print()

asyncio.run(main())
