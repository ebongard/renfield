"""Der Atom-Abruf sagt, auf wie vielen seiner acht Quellen er beruht.

🛑 Der Anlass, am 2026-10-29: die Schmalantwort des ersten erfolgreichen
foederierten Zuges. `PolymorphicAtomStore.query` verteilt ACHT Abrufquellen
ueber EINE `AsyncSession` per `asyncio.gather(return_exceptions=True)`.

Gemessen auf der Baubox gegen echtes Postgres, mit der echten Sitzungsfabrik
des Projekts, 8 nebenlaeufige Abrufe, drei Durchlaeufe stabil:

    eine Sitzung, kalt          1 von 8 geliefert     52 ms
    eine Sitzung, vorgewaermt   8 von 8               408 ms   (REIHUM)
    je eigene Sitzung           8 von 8               56 ms

🛑 Und es liess sich nicht ZAEHLEN. Alle sechs `_wrap_*`-Helfer beginnen mit
`if isinstance(x, Exception) or not x: return []` — sie werfen „abgestuerzt"
mit „nichts gefunden" zusammen, und keiner protokolliert. Von den acht Quellen
melden nur drei einen Fehlschlag selbst; `rag` und `kg` gar nicht, und das sind
der Dokumentenabruf und der Wissensgraph.

Diese Datei prueft die SICHTBARKEIT (Schritt 1), nicht die Reparatur. Ohne sie
waere die Wirkung von Schritt 2 in der Produktion nicht nachweisbar.
"""
from __future__ import annotations

import pytest

from services.polymorphic_atom_store import (
    _fehlertext,
    _protokolliere_quellen,
)

pytestmark = pytest.mark.unit


def _acht(**über):
    """Acht Quellen, alle mit einem Treffer — dann einzelne uebersteuern."""
    roh = {n: [{"id": 1}] for n in (
        "rag", "kg", "memory", "lexical_chunks", "lexical_memories",
        "skill", "document_fact", "note")}
    roh.update(über)
    return roh


class TestItTellsCrashApartFromEmpty:
    """🛑 Die Unterscheidung IST der Zweck. Eine leere Quelle ist ein
    Messergebnis, eine abgestuerzte ein Ausfall — zusammengeworfen sieht ein
    Abruf auf einem Achtel der Quellen aus wie ein Haushalt ohne Daten.
    """

    def test_all_eight_delivering(self):
        geliefert, leer, abgestürzt = _protokolliere_quellen(
            _acht(), dauer_s=0.056, foederiert=False)
        assert len(geliefert) == 8
        assert leer == [] and abgestürzt == []

    def test_an_empty_source_is_NOT_a_crash(self):
        _, leer, abgestürzt = _protokolliere_quellen(
            _acht(note=[]), dauer_s=0.05, foederiert=False)
        assert leer == ["note"]
        assert abgestürzt == []

    def test_a_crashed_source_is_NOT_empty(self):
        _, leer, abgestürzt = _protokolliere_quellen(
            _acht(note=RuntimeError("kaputt")), dauer_s=0.05, foederiert=False)
        assert abgestürzt == ["note"]
        assert leer == []

    def test_the_measured_cold_case(self):
        """Der gemessene Zustand des foederierten Pfads: 1 von 8. Genau diese
        Zahl muss im Protokoll erscheinen, sonst ist Schritt 2 nicht
        nachweisbar."""
        from sqlalchemy.exc import InvalidRequestError

        kaputt = {
            n: InvalidRequestError(
                "This session is provisioning a new connection; "
                "concurrent operations are not permitted")
            for n in ("kg", "memory", "lexical_chunks", "lexical_memories",
                      "skill", "document_fact", "note")
        }
        geliefert, _, abgestürzt = _protokolliere_quellen(
            _acht(**kaputt), dauer_s=0.052, foederiert=True)
        assert geliefert == ["rag"]
        assert len(abgestürzt) == 7


class TestTheLogCarriesNoContENT:
    """🛑 Ein Protokoll, das den Abruf sichtbar macht, darf nicht den INHALT
    sichtbar machen. SQLAlchemy haengt `[SQL: ...] [parameters: ...]` an seine
    Meldungen — also Anfragetext und Bindungen.
    """

    def test_the_SQL_tail_is_cut_off(self):
        text = _fehlertext(RuntimeError(
            "could not connect\n"
            "[SQL: SELECT * FROM documents WHERE title = 'Scheidungsurteil']\n"
            "[parameters: ('Scheidungsurteil',)]"))
        assert "could not connect" in text
        assert "Scheidungsurteil" not in text
        assert "SELECT" not in text

    def test_a_plain_message_survives(self):
        """Negativkontrolle: der Riegel darf nicht einfach alles wegschneiden,
        sonst steht im Protokoll nichts Brauchbares."""
        assert _fehlertext(RuntimeError("Verbindung abgelehnt")) == (
            "Verbindung abgelehnt")

    def test_it_is_capped(self):
        assert len(_fehlertext(RuntimeError("x" * 5000))) == 200

    def test_a_real_sqlalchemy_error_is_stripped(self):
        """Gegen eine ECHTE SQLAlchemy-Ausnahme, nicht gegen eine
        nachgestellte Zeichenkette — die Anhaengsel baut die Bibliothek."""
        from sqlalchemy.exc import StatementError

        e = StatementError(
            message="boom",
            statement="SELECT * FROM memories WHERE content = %(c)s",
            params={"c": "sehr privat"},
            orig=RuntimeError("orig"),
        )
        text = _fehlertext(e)
        assert "sehr privat" not in text
        assert "SELECT" not in text


class TestTheSummaryLine:
    """Die eine Zeile je Durchlauf — Stufe und Inhalt."""

    def test_a_crash_logs_at_WARNING_not_info(self):
        """🛑 Abgestuerzte Quellen sind keine Nebensache: die Antwort stand
        dann auf einem Teil des Abrufs. Auf INFO ginge das zwischen den
        gewoehnlichen Zeilen unter."""
        from loguru import logger as _l

        aufgefangen: list[tuple[str, str]] = []
        kennung = _l.add(
            lambda m: aufgefangen.append(
                (m.record["level"].name, m.record["message"])),
            level="INFO", format="{message}")
        try:
            _protokolliere_quellen(_acht(note=RuntimeError("kaputt")),
                                   dauer_s=0.05, foederiert=False)
            stufen = {s for s, _ in aufgefangen}
            assert "WARNING" in stufen
            assert any("note" in m for _, m in aufgefangen)
        finally:
            _l.remove(kennung)

    def test_a_clean_run_logs_at_INFO(self):
        """Negativkontrolle: ohne Absturz keine Warnung, sonst wird die
        Warnstufe wertlos."""
        from loguru import logger as _l

        aufgefangen: list[str] = []
        kennung = _l.add(
            lambda m: aufgefangen.append(m.record["level"].name),
            level="INFO", format="{message}")
        try:
            _protokolliere_quellen(_acht(), dauer_s=0.056, foederiert=False)
            assert aufgefangen and "WARNING" not in set(aufgefangen)
        finally:
            _l.remove(kennung)

    def test_the_federated_path_is_marked(self):
        """Der Pfad muss in der Zeile stehen: kalt gegen warm ist der ganze
        Unterschied, und er haengt am Pfad."""
        from loguru import logger as _l

        texte: list[str] = []
        kennung = _l.add(lambda m: texte.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _protokolliere_quellen(_acht(), dauer_s=0.05, foederiert=True)
            assert any("foederiert" in t for t in texte)
            texte.clear()
            _protokolliere_quellen(_acht(), dauer_s=0.05, foederiert=False)
            assert not any("foederiert" in t for t in texte)
        finally:
            _l.remove(kennung)

    def test_the_duration_is_in_the_line(self):
        """Ohne Dauer ist der lokale Fehler (408 statt 56 ms) nicht messbar."""
        from loguru import logger as _l

        texte: list[str] = []
        kennung = _l.add(lambda m: texte.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _protokolliere_quellen(_acht(), dauer_s=0.408, foederiert=False)
            assert any("408 ms" in t for t in texte)
        finally:
            _l.remove(kennung)


class TestQueryACTUALLYCallsIt:
    """🛑 Die Klassen darueber pruefen die FUNKTION. Diese prueft, dass
    `query` sie RUFT.

    Zweimal in dieser Sitzung ist mir genau das nachgewiesen worden: eine
    gemessene Funktion und eine ungepruefte Verwendung. Ein fester Aufruf, ein
    vergessener Aufruf oder eine Klassifizierung NACH den `_wrap_*`-Helfern
    (die die Ausnahme schon in `[]` verwandelt haben) bliebe dort unsichtbar.
    """

    @pytest.fixture
    def quellen(self, monkeypatch):
        """Alle sieben Abrufklassen ersetzt, jede mit der FORM, die ihr
        `_wrap_*`-Helfer erwartet — sonst scheitert der Test an der Attrappe
        statt an der Sache (so geschehen: `_wrap_kg_atoms` will ein
        Woerterbuch, kein Liste).

        `query` importiert die Klassen TRAEGE im Rumpf, darum greift das Setzen
        am Modul. Der Plan wird je QUELLE geschluesselt, nicht je Methode —
        `search` tragen drei verschiedene Quellen.
        """
        from unittest.mock import MagicMock

        liste = [{"id": 1, "atom_id": "a1", "score": 1.0, "content": "x",
                  "document_id": 1, "chunk_text": "x", "similarity": 1.0}]
        formen: dict[str, object] = {
            "rag": liste,
            "kg": {"entities": [], "relations": []},
            "memory": liste,
            "lexical_chunks": liste,
            "lexical_memories": liste,
            "skill": liste,
            "document_fact": liste,
            "note": liste,
        }
        plan: dict[str, object] = {}

        def stelle(modul_name, klasse_name, zuordnung):
            import importlib
            modul = importlib.import_module(f"services.{modul_name}")
            inst = MagicMock()
            for methode, quelle in zuordnung.items():
                def mach(quelle=quelle):
                    async def ruf(*a, **k):
                        wert = plan.get(quelle, formen[quelle])
                        if isinstance(wert, BaseException):
                            raise wert
                        return wert
                    return ruf
                setattr(inst, methode, mach())
            klasse = MagicMock(return_value=inst)
            monkeypatch.setattr(modul, klasse_name, klasse)

        stelle("rag_retrieval", "RAGRetrieval", {"search": "rag"})
        stelle("kg_retrieval", "KGRetrieval", {"get_relevant_atoms": "kg"})
        stelle("memory_retrieval", "MemoryRetrieval", {"retrieve": "memory"})
        stelle("lexical_retrieval", "LexicalRetrieval", {
            "search_chunks_lexical": "lexical_chunks",
            "search_memories_lexical": "lexical_memories"})
        stelle("skill_service", "SkillService", {"find_similar": "skill"})
        stelle("document_fact_retrieval", "DocumentFactRetrieval",
               {"search": "document_fact"})
        stelle("note_retrieval", "NoteRetrieval", {"search": "note"})
        return plan

    def _store(self):
        from unittest.mock import MagicMock

        from services.polymorphic_atom_store import PolymorphicAtomStore
        return PolymorphicAtomStore(MagicMock())

    async def _lauf(self, monkeypatch, plan_eintrag=None):
        from loguru import logger as _l

        from utils.config import settings

        # Die Graph-Erweiterung wuerde nach der Fusion die Bank anfassen.
        monkeypatch.setattr(settings, "graph_expansion_enabled", False)
        # 🛑 `skill` und `note` sind flaggengesteuert und liefern `[]`, wenn
        # sie aus sind — im Behaelter sind sie das, auf BEIDEN Instanzen live
        # aber `true`. Ohne dieses Setzen misst der Test 6 von 8 und damit eine
        # Konfiguration, die es in der Produktion nicht gibt.
        monkeypatch.setattr(settings, "skills_enabled", True)
        monkeypatch.setattr(settings, "notes_enabled", True)

        texte: list[tuple[str, str]] = []
        kennung = _l.add(
            lambda m: texte.append(
                (m.record["level"].name, m.record["message"])),
            level="INFO", format="{message}")
        try:
            await self._store().query(
                "egal", asker_id=1, max_visible_tier=0, top_k=5)
        finally:
            _l.remove(kennung)
        return texte

    @pytest.mark.asyncio
    async def test_a_clean_run_logs_the_summary_line(self, quellen, monkeypatch):
        texte = await self._lauf(monkeypatch)
        zeilen = [m for _, m in texte if "Atom-Abruf" in m]
        assert zeilen, f"keine Abruf-Zeile im Protokoll: {texte}"
        assert "8/8 Quellen geliefert" in zeilen[0], zeilen[0]
        assert " ms" in zeilen[0]

    @pytest.mark.asyncio
    async def test_a_crashing_source_reaches_the_log_THROUGH_query(
        self, quellen, monkeypatch
    ):
        """🛑 Der Kern: die Ausnahme muss GEZAEHLT werden, bevor ein
        `_wrap_*`-Helfer sie in `[]` verwandelt. Stuende die Klassifizierung
        hinter den Helfern, waere diese Zeile `8/8 geliefert`."""
        from sqlalchemy.exc import InvalidRequestError

        quellen["rag"] = InvalidRequestError(
            "This session is provisioning a new connection; "
            "concurrent operations are not permitted")
        texte = await self._lauf(monkeypatch)
        zusammen = [m for s, m in texte if "Quellen geliefert" in m]
        assert zusammen, f"keine Zusammenfassung: {texte}"
        assert "7/8 Quellen geliefert" in zusammen[0], zusammen[0]
        assert "1 abgestuerzt" in zusammen[0]
        assert any(s == "WARNING" for s, m in texte if "Quellen geliefert" in m)
