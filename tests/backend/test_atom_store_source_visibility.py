"""Der Atom-Abruf sagt, auf wie vielen seiner acht Quellen er beruht.

🛑 Der Anlass, am 2026-10-29: die Schmalantwort des ersten erfolgreichen
foederierten Zuges. `PolymorphicAtomStore.query` verteilt ACHT Abrufquellen
ueber EINE `AsyncSession` per `asyncio.gather(return_exceptions=True)`.

Gemessen auf der Baubox gegen echtes Postgres, mit der echten Session-Fabrik
des Projekts, 8 nebenlaeufige Abrufe, drei Durchlaeufe stabil:

    eine Session, kalt          1 von 8 geliefert     52 ms
    eine Session, vorgewaermt   8 von 8               408 ms   (REIHUM)
    je eigene Session           8 von 8               56 ms

🛑 Und es liess sich nicht ZAEHLEN. Alle sechs `_wrap_*`-Helfer beginnen mit
`if isinstance(x, Exception) or not x: return []` — sie werfen „abgestuerzt"
mit „nichts gefunden" zusammen, und keiner protokolliert. Von den acht Quellen
melden nur drei einen Fehlschlag selbst; `rag` und `kg` gar nicht, und das sind
der Dokumentenabruf und der Wissensgraph.

Diese Datei prueft die SICHTBARKEIT (Schritt 1), nicht die Reparatur. Ohne sie
waere die Wirkung von Schritt 2 in der Produktion nicht nachweisbar.

🛑 Erste Fassung im Review zerlegt, und zwei der Befunde standen HIER:

- Die Leerheit wurde mit `not wert` bestimmt. `kg` liefert bei Nichttreffer
  `{"entities": [], "relations": []}` — ein Dict mit zwei Schluesseln
  ist WAHR, also zaehlte `kg` immer als geliefert. Und mein Test nagelte das
  als Erwartung fest (`8/8 geliefert` bei leerem `kg`), mit einem Kommentar,
  der es rechtfertigte. Ein Test, der den Fehler zum Soll macht, ist
  schlimmer als kein Test: wer ihn repariert, macht ihn rot.
- `test_the_measured_cold_case` stellte sieben gleichzeitige Ausnahmen nach.
  Den Zustand kann der echte Pfad nicht erzeugen: fuenf Quellen fingen ihre
  Ausnahme selbst und gaben `[]` zurueck, kamen also als „leer" an.
"""
from __future__ import annotations

import pytest

from services.polymorphic_atom_store import (
    _wrap_kg_atoms,
    _wrap_memory_results,
    _wrap_note_results,
    _wrap_rag_results,
    _wrappen_und_zaehlen,
)
from utils.log_safety import fehlertext_ohne_inhalt

pytestmark = pytest.mark.unit


#: Die acht Quellen in der Reihenfolge, in der `query` sie uebergibt, je mit
#: ihrem Wrapper — der die Leerheit entscheidet, weil nur er die Form kennt.
_EINPACKER = {
    "rag": _wrap_rag_results,
    "kg": _wrap_kg_atoms,
    "memory": _wrap_memory_results,
    "lexical_chunks": _wrap_rag_results,
    "lexical_memories": _wrap_memory_results,
    "skill": lambda w: list(w or []),
    "document_fact": lambda w: list(w or []),
    "note": _wrap_note_results,
}

#: Eine Form MIT Treffer je Quelle. 🛑 Nicht erfunden, sondern an den
#: Wrappern abgelesen — `_wrap_rag_results` liest `chunk`/`document`,
#: `_wrap_kg_atoms` liest `entities`/`relations`.
_MIT_TREFFER: dict[str, object] = {
    "rag": [{"chunk": {"id": 1, "content": "x", "circle_tier": 0},
             "document": {"id": 1, "atom_id": "doc:1", "circle_tier": 0},
             "similarity": 1.0}],
    "kg": {"entities": [{"id": 1, "name": "x", "atom_id": "kg:1",
                         "circle_tier": 0}], "relations": []},
    "memory": [{"id": 1, "atom_id": "mem:1", "content": "x",
                "similarity": 1.0, "circle_tier": 0}],
    "skill": [{"id": 1}],
    "document_fact": [{"id": 1}],
    "note": [{"id": 1, "atom_id": "note:1", "title": "x", "circle_tier": 0}],
}
_MIT_TREFFER["lexical_chunks"] = _MIT_TREFFER["rag"]
_MIT_TREFFER["lexical_memories"] = _MIT_TREFFER["memory"]

#: Eine Form OHNE Treffer je Quelle. 🛑 Fuer `kg` ist das NICHT `{}` sondern
#: `{"entities": [], "relations": []}` — genau der Wert, der `not wert` austrickst.
_OHNE_TREFFER: dict[str, object] = {
    "kg": {"entities": [], "relations": []},
}


def _quellen(**ueber):
    """Die acht Quellen, standardmaessig alle mit Treffer."""
    liste = []
    for name, einpacker in _EINPACKER.items():
        if name in ueber:
            wert = ueber[name]
            if wert == "leer":
                wert = _OHNE_TREFFER.get(name, [])
        else:
            wert = _MIT_TREFFER[name]
        liste.append((name, wert, einpacker))
    return liste


def _zaehle(**ueber):
    """Nur die Zaehlung, ohne die gewrappten Treffer."""
    aus = ueber.pop("aus", set())
    dauer = ueber.pop("dauer_s", 0.05)
    foed = ueber.pop("foederiert", False)
    _, zahlen = _wrappen_und_zaehlen(
        _quellen(**ueber), ausgeschaltet=aus, dauer_s=dauer, foederiert=foed)
    return zahlen


class TestItTellsCrashApartFromEmpty:
    """🛑 Die Unterscheidung IST der Zweck. Eine leere Quelle ist ein
    Messergebnis, eine abgestuerzte ein Ausfall — zusammengeworfen sieht ein
    Abruf auf einem Achtel der Quellen aus wie ein Haushalt ohne Daten.
    """

    def test_all_eight_delivering(self):
        geliefert, leer, abgestuerzt = _zaehle()
        assert len(geliefert) == 8, f"leer={leer} abgestuerzt={abgestuerzt}"
        assert leer == [] and abgestuerzt == []

    def test_an_empty_source_is_NOT_a_crash(self):
        _, leer, abgestuerzt = _zaehle(note="leer")
        assert leer == ["note"]
        assert abgestuerzt == []

    def test_a_crashed_source_is_NOT_empty(self):
        _, leer, abgestuerzt = _zaehle(note=RuntimeError("kaputt"))
        assert abgestuerzt == ["note"]
        assert leer == []

    def test_an_empty_KG_DICT_counts_as_empty(self):
        """🛑 Der Befund aus dem Review, als Test. `kg` liefert bei
        Nichttreffer `{"entities": [], "relations": []}`, und das ist WAHR —
        mit `not wert` zaehlte es als geliefert, dauerhaft eins zu hoch. Und
        meine erste Testfassung nagelte genau das als Soll fest."""
        geliefert, leer, abgestuerzt = _zaehle(kg="leer")
        assert leer == ["kg"], f"geliefert={geliefert}"
        assert len(geliefert) == 7
        assert abgestuerzt == []

    def test_a_KG_WITH_hits_still_counts_as_delivered(self):
        """Negativkontrolle dazu: der Riegel darf `kg` nicht grundsaetzlich
        als leer abtun, sonst steht dauerhaft eins zu WENIG."""
        geliefert, leer, _ = _zaehle()
        assert "kg" in geliefert and "kg" not in leer

    def test_the_wrapper_decides_emptiness_for_EVERY_source(self):
        """🛑 Die Klasse, nicht die benannte Instanz: `kg` war der gefundene
        Fall, aber die Frage gilt fuer alle acht. Jede Quelle muss mit ihrer
        eigenen Leer-Form als leer herauskommen."""
        for name in _EINPACKER:
            _, leer, abgestuerzt = _zaehle(**{name: "leer"})
            assert leer == [name], f"{name}: leer={leer}"
            assert abgestuerzt == []


class TestDisabledSourcesAreNotEmptySources:
    """🛑 Eine abgeschaltete Quelle ist nicht befragt worden. Zaehlte sie als
    leer, stuende bei `NOTES_ENABLED=false` dauerhaft „6/8 geliefert, 2 leer"
    — das sieht wie ein Teilausfall aus, und wer dem nachgeht, sucht einen
    Fehler, den es nicht gibt.
    """

    def test_a_disabled_source_is_in_NEITHER_bucket(self):
        geliefert, leer, abgestuerzt = _zaehle(note="leer", aus={"note"})
        assert "note" not in geliefert and "note" not in leer
        assert abgestuerzt == []

    def test_the_denominator_shrinks(self):
        from loguru import logger as _l

        texte: list[str] = []
        kennung = _l.add(lambda m: texte.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _zaehle(note="leer", skill="leer", aus={"note", "skill"})
            assert any("6/6 Quellen geliefert" in t for t in texte), texte
            assert any("2 aus" in t for t in texte), texte
        finally:
            _l.remove(kennung)

    def test_without_disabled_sources_the_line_says_nothing_about_them(self):
        """Negativkontrolle: der Zusatz darf nicht immer dastehen."""
        from loguru import logger as _l

        texte: list[str] = []
        kennung = _l.add(lambda m: texte.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _zaehle()
            assert any("8/8 Quellen geliefert" in t for t in texte), texte
            assert not any(" aus" in t for t in texte), texte
        finally:
            _l.remove(kennung)


class TestTheRealColdCase:
    """🛑 Die erste Fassung stellte SIEBEN gleichzeitige Ausnahmen nach. Den
    Zustand kann der echte Pfad nicht erzeugen — fuenf Quellen fingen ihre
    Ausnahme selbst und gaben `[]` zurueck, kamen also als „leer" an.

    Drei davon (`lexical_chunks`, `lexical_memories`, `note`) haben keinen
    Aufrufer ausserhalb der Fan-out (nachgemessen) und greifen jetzt weiter.
    `memory`, `document_fact` und `kg` haben Fremdaufrufer und schlucken
    weiter — das ist eine offene Entscheidung, kein Versehen, und dieser Test
    haelt den Stand fest, damit die Zahl nicht spaeter still kippt.
    """

    def test_the_three_callerless_sources_report_a_crash(self):
        from sqlalchemy.exc import InvalidRequestError

        kalt = InvalidRequestError(
            "This session is provisioning a new connection; "
            "concurrent operations are not permitted")
        _, _, abgestuerzt = _zaehle(
            lexical_chunks=kalt, lexical_memories=kalt, note=kalt)
        assert set(abgestuerzt) == {"lexical_chunks", "lexical_memories", "note"}

    def test_a_still_swallowing_source_shows_up_as_empty(self):
        """Der Stand, nicht das Ziel: schluckt eine Quelle weiter, kommt sie
        als leer an. Wird das repariert, faellt dieser Test — und das ist
        dann der Anlass, die Zahl im Docstring nachzuziehen."""
        _, leer, abgestuerzt = _zaehle(memory="leer", document_fact="leer")
        assert set(leer) == {"memory", "document_fact"}
        assert abgestuerzt == []


class TestTheGuardAgainstContentInTheLog:
    """🛑 Der `[SQL: …] [parameters: …]`-Anhang von SQLAlchemy traegt den
    gebundenen Nutzertext. Er bringt diagnostisch nichts und wird abgeschnitten.

    🛑 Was das NICHT ist: ein Verbot von Inhalt im Protokoll. Das Repo
    protokolliert den Anfragetext an anderer Stelle absichtlich
    (`rag_retrieval` schreibt `query='…'` auf INFO). Diese Entscheidung wird
    hier nicht umgedreht — hier wird nur eine Last entfernt, die keinen Nutzen
    hat. Erste Fassung dieses PRs hat das als „kein Inhalt" ueberverkauft.
    """

    def test_the_SQL_tail_is_cut_off(self):
        text = fehlertext_ohne_inhalt(RuntimeError(
            "could not connect\n"
            "[SQL: SELECT * FROM documents WHERE title = 'Scheidungsurteil']\n"
            "[parameters: ('Scheidungsurteil',)]"))
        assert "could not connect" in text
        assert "Scheidungsurteil" not in text
        assert "SELECT" not in text

    def test_a_plain_message_survives(self):
        """Negativkontrolle: der Riegel darf nicht einfach alles wegschneiden,
        sonst steht im Protokoll nichts Brauchbares."""
        assert fehlertext_ohne_inhalt(
            RuntimeError("Verbindung abgelehnt")) == "Verbindung abgelehnt"

    def test_it_is_capped(self):
        from utils.log_safety import MAX_LEN

        assert len(fehlertext_ohne_inhalt(RuntimeError("x" * 5000))) == MAX_LEN

    def test_a_real_sqlalchemy_error_is_stripped(self):
        """Gegen eine ECHTE SQLAlchemy-Ausnahme, nicht gegen eine
        nachgestellte String — die Anhaengsel baut die Bibliothek."""
        from sqlalchemy.exc import StatementError

        e = StatementError(
            message="boom",
            statement="SELECT * FROM memories WHERE content = %(c)s",
            params={"c": "sehr privat"},
            orig=RuntimeError("orig"),
        )
        text = fehlertext_ohne_inhalt(e)
        assert "sehr privat" not in text
        assert "SELECT" not in text

    def test_the_crash_line_uses_the_guard(self):
        """🛑 Die Verdrahtung: nicht nur die Funktion, sondern ihre Verwendung
        in der Absturzzeile."""
        from loguru import logger as _l
        from sqlalchemy.exc import StatementError

        e = StatementError(message="boom", statement="SELECT 1",
                           params={"c": "sehr privat"}, orig=RuntimeError("o"))
        texte: list[str] = []
        kennung = _l.add(lambda m: texte.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _zaehle(note=e)
            assert any("abgestuerzt" in t for t in texte), texte
            assert not any("sehr privat" in t for t in texte), texte
        finally:
            _l.remove(kennung)

    def test_every_DB_error_log_in_the_retrieval_modules_uses_it(self):
        """🛑 Der Riegel wurde in der ersten Fassung von den Quellen selbst
        umgangen: vier protokollierten `{e}` roh, mitsamt `[SQL: …]`-Anhang und
        der gebundenen Nutzeranfrage.

        🛑 Die Abgrenzung ist nicht willkuerlich und auch keine Liste, die
        verrottet: geprueft werden genau die `except`-Zweige, deren `try`-Rumpf
        eine `.execute(...)` enthaelt — dort und nur dort kann ein
        SQL-Anhang entstehen, und nur dort nuetzt der Schnitt. Ein
        Einbettungs- oder LLM-Fehler traegt keinen `[SQL:`-Anhang; der Riegel
        waere dort Theater. (Wer DIE Luecke schliessen will, braucht einen
        anderen Riegel, nicht denselben an mehr Stellen.)

        Am RUMPF geprueft (`ast`), nicht per Textsuche ueber die Datei — eine
        Textsuche faende die eigenen Kommentare mit.
        """
        import ast
        import pathlib as _pl

        wurzel = _pl.Path(__file__).resolve().parents[2] / "src/backend/services"

        def hat_execute(knoten) -> bool:
            for n in ast.walk(knoten):
                if (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "execute"):
                    return True
            return False

        verdaechtig: list[str] = []
        for name in ("rag_retrieval", "kg_retrieval", "memory_retrieval",
                     "lexical_retrieval", "skill_service",
                     "document_fact_retrieval", "note_retrieval"):
            datei = wurzel / f"{name}.py"
            baum = ast.parse(datei.read_text())
            for versuch in (n for n in ast.walk(baum) if isinstance(n, ast.Try)):
                if not any(hat_execute(a) for a in versuch.body):
                    continue
                for zweig in versuch.handlers:
                    for n in ast.walk(zweig):
                        if not isinstance(n, ast.JoinedStr):
                            continue
                        roh = ast.unparse(n)
                        if "{e}" in roh and "fehlertext_ohne_inhalt" not in roh:
                            verdaechtig.append(f"{name}:{n.lineno}: {roh[:70]}")

        assert not verdaechtig, (
            "rohe Ausnahme in einer Protokollzeile, deren `try` eine Abfrage "
            f"enthaelt — der SQL-Anhang geht ungeschnitten hinaus: {verdaechtig}")

    def test_the_structural_sweep_actually_BITES(self):
        """🛑 Negativkontrolle an der Pruefung selbst: findet der Durchgang
        ueberhaupt etwas, wenn es etwas zu finden gibt? Ohne das koennte die
        Suche an einem Detail scheitern und der Test waere stumm gruen."""
        import ast

        quelle = (
            "async def f(self, sql):\n"
            "    try:\n"
            "        return await self.db.execute(sql)\n"
            "    except Exception as e:\n"
            "        logger.warning(f'kaputt: {e}')\n"
            "        return []\n"
        )
        baum = ast.parse(quelle)
        treffer = []
        for versuch in (n for n in ast.walk(baum) if isinstance(n, ast.Try)):
            hat = any(
                isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute)
                and k.func.attr == "execute"
                for a in versuch.body for k in ast.walk(a))
            if not hat:
                continue
            for zweig in versuch.handlers:
                for n in ast.walk(zweig):
                    if isinstance(n, ast.JoinedStr) and "{e}" in ast.unparse(n):
                        treffer.append(n.lineno)
        assert treffer, "der Durchgang findet nicht einmal den offensichtlichen Fall"


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
            _zaehle(note=RuntimeError("kaputt"))
            assert "WARNING" in {s for s, _ in aufgefangen}
            assert any("note" in m for _, m in aufgefangen)
        finally:
            _l.remove(kennung)

    def test_a_clean_run_logs_at_INFO(self):
        """Negativkontrolle: ohne Absturz keine Warnung, sonst wird die
        Warnstufe wertlos."""
        from loguru import logger as _l

        aufgefangen: list[str] = []
        kennung = _l.add(lambda m: aufgefangen.append(m.record["level"].name),
                         level="INFO", format="{message}")
        try:
            _zaehle()
            assert aufgefangen and "WARNING" not in set(aufgefangen)
        finally:
            _l.remove(kennung)

    def test_the_duration_is_in_the_line(self):
        """Die Dauer der FAECHERUNG, ausdruecklich so benannt. Sie umschliesst
        auch die Einbettungsaufrufe und ist deshalb NICHT mit den reinen
        SQL-Zahlen der Probe (56 gegen 408 ms) vergleichbar — ein Befund aus
        dem Review."""
        from loguru import logger as _l

        texte: list[str] = []
        kennung = _l.add(lambda m: texte.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _zaehle(dauer_s=0.408)
            assert any("Fan-out 408 ms" in t for t in texte), texte
        finally:
            _l.remove(kennung)


class TestQueryACTUALLYCallsIt:
    """🛑 Die Klassen darueber pruefen die ZAEHLUNG. Diese prueft, dass `query`
    sie ruft — und dass die Pfadmarkierung stimmt.

    🛑 Die erste Fassung behauptete hier, sie pruefe die REIHENFOLGE
    („Klassifizierung vor den `_wrap_*`-Helfern"). Das war nachweisbar falsch:
    die Helfer veraendern ihr Argument nicht, sie geben eine neue Liste zurueck
    — die Reihenfolge war also gleichgueltig, und der Test blieb gruen, wenn
    man sie umdrehte. Eine Scheinpruefung an genau der Stelle, die sich „den
    Kern" nannte. Die Behauptung ist gestrichen; Wrappen und Zaehlen sind
    jetzt ohnehin ein Durchgang.
    """

    @pytest.fixture
    def quellen(self, monkeypatch):
        """Alle sieben Abrufklassen ersetzt, jede mit der Form, die ihr
        Wrapper WIRKLICH liest — an den Wrappern abgelesen, nicht geraten.
        Die erste Fassung gab `rag` eine Form ohne `chunk`/`document`, womit
        bei jedem Lauf eine WARNING mitlief und die rag-Form ungeprueft blieb.

        `query` importiert die Klassen traege im Rumpf, darum greift das Setzen
        am Modul.
        """
        from unittest.mock import MagicMock

        plan: dict[str, object] = {}

        def stelle(modul_name, klasse_name, zuordnung):
            import importlib
            modul = importlib.import_module(f"services.{modul_name}")
            inst = MagicMock()
            for methode, quelle in zuordnung.items():
                def mach(quelle=quelle):
                    async def ruf(*a, **k):
                        wert = plan.get(quelle, _MIT_TREFFER[quelle])
                        if isinstance(wert, BaseException):
                            raise wert
                        if wert == "leer":
                            return _OHNE_TREFFER.get(quelle, [])
                        return wert
                    return ruf
                setattr(inst, methode, mach())
            monkeypatch.setattr(modul, klasse_name, MagicMock(return_value=inst))

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

    async def _lauf(self, monkeypatch, **query_kwargs):
        from unittest.mock import MagicMock

        from loguru import logger as _l

        from services.polymorphic_atom_store import PolymorphicAtomStore
        from utils.config import settings

        # Die Graph-Erweiterung wuerde nach der Fusion die Bank anfassen.
        monkeypatch.setattr(settings, "graph_expansion_enabled", False)
        # 🛑 `skill` und `note` sind flaggengesteuert. Im Behaelter sind sie
        # aus, auf BEIDEN Instanzen live aber `true` — ohne dieses Setzen misst
        # der Test eine Konfiguration, die es in der Produktion nicht gibt.
        monkeypatch.setattr(settings, "skills_enabled", True)
        monkeypatch.setattr(settings, "notes_enabled", True)

        texte: list[tuple[str, str]] = []
        kennung = _l.add(
            lambda m: texte.append(
                (m.record["level"].name, m.record["message"])),
            level="INFO", format="{message}")
        try:
            await PolymorphicAtomStore(MagicMock()).query(
                "egal", asker_id=1, max_visible_tier=0, top_k=5,
                **query_kwargs)
        finally:
            _l.remove(kennung)
        return texte

    @pytest.mark.asyncio
    async def test_a_clean_run_logs_the_summary_line(self, quellen, monkeypatch):
        texte = await self._lauf(monkeypatch)
        zeilen = [m for _, m in texte if "Atom-Abruf" in m]
        assert zeilen, f"keine Abruf-Zeile im Protokoll: {texte}"
        assert "8/8 Quellen geliefert" in zeilen[0], zeilen[0]
        assert "Fan-out" in zeilen[0]

    @pytest.mark.asyncio
    async def test_the_mock_shapes_produce_NO_stray_warning(self, quellen, monkeypatch):
        """🛑 Negativkontrolle an der Attrappe selbst. Passt eine Form nicht,
        protokolliert der Wrapper eine WARNING — dann ist nicht zu sehen, ob
        ein Test wegen der Sache gruen ist oder trotz einer kaputten Attrappe."""
        texte = await self._lauf(monkeypatch)
        fremd = [m for s, m in texte if s == "WARNING" and "Atom-Abruf" not in m]
        assert not fremd, f"Attrappe loest eine Warnung aus: {fremd}"

    @pytest.mark.asyncio
    async def test_a_crashing_source_is_COUNTED_through_query(
        self, quellen, monkeypatch
    ):
        from sqlalchemy.exc import InvalidRequestError

        quellen["rag"] = InvalidRequestError(
            "This session is provisioning a new connection; "
            "concurrent operations are not permitted")
        texte = await self._lauf(monkeypatch)
        zusammen = [m for _, m in texte if "Quellen geliefert" in m]
        assert zusammen, f"keine Zusammenfassung: {texte}"
        assert "7/8 Quellen geliefert" in zusammen[0], zusammen[0]
        assert "1 abgestuerzt" in zusammen[0]
        assert any(s == "WARNING" for s, m in texte if "Quellen geliefert" in m)

    @pytest.mark.asyncio
    async def test_an_empty_KG_is_counted_as_empty_through_query(
        self, quellen, monkeypatch
    ):
        """Der Review-Befund, durch `query` hindurch — nicht nur an der
        Zaehlfunktion."""
        quellen["kg"] = "leer"
        texte = await self._lauf(monkeypatch)
        zusammen = [m for _, m in texte if "Quellen geliefert" in m]
        assert "7/8 Quellen geliefert" in zusammen[0], zusammen[0]
        assert "1 leer" in zusammen[0]


class TestTheFederatedPathIsMarkedBYTHECALLER:
    """🛑 Der schwerste Befund des Reviews. Die Zeile leitete den Pfad aus
    `peer is not None` ab — und `_peer_scope_for` gibt im GEMAPPTEN Fall
    `None` zurueck (`federation_query_responder.py:285`). Gemappt ist der
    Pfad, der live laeuft. Damit blieb genau der kalte Lauf unmarkiert, fuer
    den die Markierung gebaut wurde.
    """

    def test_query_takes_the_path_as_its_OWN_keyword(self):
        import inspect

        from services.polymorphic_atom_store import PolymorphicAtomStore

        sig = inspect.signature(PolymorphicAtomStore.query)
        assert "federated" in sig.parameters
        assert sig.parameters["federated"].default is False, (
            "die Vorgabe muss `False` sein — der lokale Pfad ist der haeufige")

    def test_the_responder_SETS_it(self):
        """Am Rumpf geprueft (`ast`, ohne Docstring), nicht per Textsuche —
        eine Textsuche faende meine eigenen Kommentare mit."""
        import ast
        import inspect

        from services.federation_query_responder import FederationQueryResponder

        quelle = inspect.getsource(FederationQueryResponder._retrieve)
        baum = ast.parse(quelle.lstrip() if quelle.startswith(" ") else quelle)
        gesetzt = False
        for n in ast.walk(baum):
            if isinstance(n, ast.Call):
                for kw in n.keywords:
                    if kw.arg == "federated" and getattr(
                            kw.value, "value", None) is True:
                        gesetzt = True
        assert gesetzt, (
            "`_retrieve` muss `federated=True` uebergeben — sonst traegt der "
            "kalte Lauf keine Markierung, und kalt gegen warm ist der ganze "
            "Unterschied")

    def test_peer_is_NOT_used_as_the_path_signal(self):
        """🛑 Negativkontrolle gegen den Rueckfall in den alten Fehler: die
        Markierung darf nicht wieder aus `peer` abgeleitet werden. Geprueft
        wird die WIRKUNG — `peer` gesetzt, `federated` nicht, also unmarkiert."""
        from loguru import logger as _l

        from services.polymorphic_atom_store import _wrappen_und_zaehlen

        texte: list[str] = []
        kennung = _l.add(lambda m: texte.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _wrappen_und_zaehlen(
                _quellen(), ausgeschaltet=set(), dauer_s=0.05,
                foederiert=False)
            assert not any("foederiert" in t for t in texte), texte
            texte.clear()
            _wrappen_und_zaehlen(
                _quellen(), ausgeschaltet=set(), dauer_s=0.05,
                foederiert=True)
            assert any("foederiert" in t for t in texte), texte
        finally:
            _l.remove(kennung)
