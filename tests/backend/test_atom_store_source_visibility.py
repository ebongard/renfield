"""Atom retrieval reports how many of its eight sources an answer rests on.

Why this file exists, 2026-10-29: the thin answer of the first successful
federated query. `PolymorphicAtomStore.query` spreads EIGHT retrieval sources
over ONE `AsyncSession` via `asyncio.gather(return_exceptions=True)`.

Measured on the build box against real Postgres, using the project's own
session factory, 8 concurrent queries, three runs, stable:

    one session, cold           1 of 8 delivered      52 ms
    one session, pre-warmed     8 of 8               408 ms   (SERIAL)
    one session per source      8 of 8                56 ms

And it could not be COUNTED. All six `_wrap_*` helpers start with
`if isinstance(x, Exception) or not x: return []` -- they conflate "crashed"
with "found nothing", and none of them logs. Of the eight sources only three
report a failure themselves; `rag` and `kg` not at all, and those two are
document retrieval and the knowledge graph.

This file tests VISIBILITY (step 1), not the fix. Without it the effect of
step 2 would not be provable in production.

The first version was taken apart in review, and two of the findings were
HERE:

- Emptiness was decided with `not value`. `kg` returns
  `{"entities": [], "relations": []}` on a miss -- a dict with two keys is
  TRUTHY, so `kg` always counted as delivered. And my test pinned that down as
  the expected value (`8/8 delivered` with an empty `kg`), with a comment
  justifying it. A test that makes the bug the target is worse than no test:
  whoever fixes it turns the test red.
- `test_the_measured_cold_case` staged seven simultaneous exceptions. The real
  path cannot produce that state: five sources caught their own exception and
  returned `[]`, so they arrived as "empty".
"""
from __future__ import annotations

import pytest

from services.polymorphic_atom_store import (
    _wrap_and_count,
    _wrap_kg_atoms,
    _wrap_memory_results,
    _wrap_note_results,
    _wrap_rag_results,
)
from utils.log_safety import error_text_without_content

pytestmark = pytest.mark.unit


#: The eight sources in the order `query` passes them, each with its wrapper
#: -- which decides emptiness, because only it knows the shape.
_WRAPPERS = {
    "rag": _wrap_rag_results,
    "kg": _wrap_kg_atoms,
    "memory": _wrap_memory_results,
    "lexical_chunks": _wrap_rag_results,
    "lexical_memories": _wrap_memory_results,
    "skill": lambda w: list(w or []),
    "document_fact": lambda w: list(w or []),
    "note": _wrap_note_results,
}

#: A shape WITH hits per source. Not invented but read off the wrappers:
#: `_wrap_rag_results` reads `chunk`/`document`, `_wrap_kg_atoms` reads
#: `entities`/`relations`.
_WITH_HITS: dict[str, object] = {
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
_WITH_HITS["lexical_chunks"] = _WITH_HITS["rag"]
_WITH_HITS["lexical_memories"] = _WITH_HITS["memory"]

#: A shape WITHOUT hits per source. For `kg` that is NOT `{}` but
#: `{"entities": [], "relations": []}` -- the exact value that fools
#: `not value`.
_WITHOUT_HITS: dict[str, object] = {
    "kg": {"entities": [], "relations": []},
}


def _sources(**overrides):
    """The eight sources, all with hits unless overridden."""
    out = []
    for name, wrapper in _WRAPPERS.items():
        if name in overrides:
            value = overrides[name]
            if value == "empty":
                value = _WITHOUT_HITS.get(name, [])
        else:
            value = _WITH_HITS[name]
        out.append((name, value, wrapper))
    return out


def _count(**overrides):
    """Just the counts, without the wrapped hits."""
    off = overrides.pop("off", set())
    duration = overrides.pop("duration_s", 0.05)
    federated = overrides.pop("foederiert", False)
    _, counts = _wrap_and_count(
        _sources(**overrides), disabled=off, duration_s=duration, federated=federated)
    return counts


class TestItTellsCrashApartFromEmpty:
    """The distinction IS the point. An empty source is a measurement, a
    crashed one is an outage -- conflated, a retrieval that used one
    eighth of its sources looks like a household with no data.
    """

    def test_all_eight_delivering(self):
        delivered, empty, crashed = _count()
        assert len(delivered) == 8, f"empty={empty} crashed={crashed}"
        assert empty == [] and crashed == []

    def test_an_empty_source_is_NOT_a_crash(self):
        _, empty, crashed = _count(note="empty")
        assert empty == ["note"]
        assert crashed == []

    def test_a_crashed_source_is_NOT_empty(self):
        _, empty, crashed = _count(note=RuntimeError("broken"))
        assert crashed == ["note"]
        assert empty == []

    def test_an_empty_KG_DICT_counts_as_empty(self):
        """🛑 The finding from the review, as a test. `kg` returns a dict on
        a miss, and that is TRUTHY -- with `not value` it counted as
        delivered, permanently one too high. And my first version of this
        test pinned exactly that down as the target."""
        delivered, empty, crashed = _count(kg="empty")
        assert empty == ["kg"], f"delivered={delivered}"
        assert len(delivered) == 7
        assert crashed == []

    def test_a_KG_WITH_hits_still_counts_as_delivered(self):
        """Negative control for it: the guard must not write `kg` off as
        empty across the board, or the count is permanently one too LOW."""
        delivered, empty, _ = _count()
        assert "kg" in delivered and "kg" not in empty

    def test_the_wrapper_decides_emptiness_for_EVERY_source(self):
        """The class, not the named instance: `kg` was the case that was
        found, but the question applies to all eight. Every source must
        come out as empty when given its own empty shape."""
        for name in _WRAPPERS:
            _, empty, crashed = _count(**{name: "empty"})
            assert empty == [name], f"{name}: empty={empty}"
            assert crashed == []


class TestDisabledSourcesAreNotEmptySources:
    """A disabled source was never asked. Counting it as empty would make
    `NOTES_ENABLED=false` read "6/8 delivered, 2 empty" forever -- that
    looks like a partial outage, and anyone chasing it hunts a bug that is
    not there.
    """

    def test_a_disabled_source_is_in_NEITHER_bucket(self):
        delivered, empty, crashed = _count(note="empty", off={"note"})
        assert "note" not in delivered and "note" not in empty
        assert crashed == []

    def test_the_denominator_shrinks(self):
        from loguru import logger as _l

        lines: list[str] = []
        sink_id = _l.add(lambda m: lines.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _count(note="empty", skill="empty", off={"note", "skill"})
            assert any("6/6 sources delivered" in t for t in lines), lines
            assert any("2 off" in t for t in lines), lines
        finally:
            _l.remove(sink_id)

    def test_without_disabled_sources_the_line_says_nothing_about_them(self):
        """Negative control: the suffix must not always be there."""
        from loguru import logger as _l

        lines: list[str] = []
        sink_id = _l.add(lambda m: lines.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _count()
            assert any("8/8 sources delivered" in t for t in lines), lines
            assert not any(" off" in t for t in lines), lines
        finally:
            _l.remove(sink_id)


class TestTheRealColdCase:
    """🛑 The first version reproduced SEVEN simultaneous exceptions. The
    cannot produce that state -- five sources caught their own exception and
    returned `[]`, so they arrived as "empty".

    Three of them (`lexical_chunks`, `lexical_memories`, `note`) have no caller
    outside the fan-out (measured) and now re-raise. `memory`,
    `document_fact` and `kg` have external callers and still swallow --
    that is an open decision, not an oversight, and this test pins down the
    current state so the number cannot quietly shift later.
    """

    def test_the_three_callerless_sources_report_a_crash(self):
        from sqlalchemy.exc import InvalidRequestError

        kalt = InvalidRequestError(
            "This session is provisioning a new connection; "
            "concurrent operations are not permitted")
        _, _, crashed = _count(
            lexical_chunks=kalt, lexical_memories=kalt, note=kalt)
        assert set(crashed) == {"lexical_chunks", "lexical_memories", "note"}

    def test_a_still_swallowing_source_shows_up_as_empty(self):
        """The state, not the goal: a source that still swallows arrives as
        empty. Fix that and this test fails -- which is then the prompt to
        update the number in the docstring."""
        _, empty, crashed = _count(memory="empty", document_fact="empty")
        assert set(empty) == {"memory", "document_fact"}
        assert crashed == []


class TestTheGuardAgainstContentInTheLog:
    """🛑 SQLAlchemy's `[SQL: ...] [parameters: ...]` tail carries the
    bound user text. It adds nothing diagnostically and is cut off.

    What this is NOT: a ban on content in the log. The repo logs the query
    text elsewhere on purpose (`rag_retrieval` writes `query='...'` at
    INFO). That decision is not reversed here -- all that happens is
    removing a liability with no benefit. The first version of this PR
    oversold it as "no content".
    """

    def test_the_SQL_tail_is_cut_off(self):
        text = error_text_without_content(RuntimeError(
            "could not connect\n"
            "[SQL: SELECT * FROM documents WHERE title = 'Scheidungsurteil']\n"
            "[parameters: ('Scheidungsurteil',)]"))
        assert "could not connect" in text
        assert "Scheidungsurteil" not in text
        assert "SELECT" not in text

    def test_a_plain_message_survives(self):
        """Negative control: the guard must not simply cut everything, or
        nothing usable is left in the log."""
        assert error_text_without_content(
            RuntimeError("Verbindung abgelehnt")) == "Verbindung abgelehnt"

    def test_it_is_capped(self):
        from utils.log_safety import MAX_LEN

        assert len(error_text_without_content(RuntimeError("x" * 5000))) == MAX_LEN

    def test_a_real_sqlalchemy_error_is_stripped(self):
        """Against a REAL SQLAlchemy exception, not a hand-made string --
        the library builds the tail."""
        from sqlalchemy.exc import StatementError

        e = StatementError(
            message="boom",
            statement="SELECT * FROM memories WHERE content = %(c)s",
            params={"c": "sehr privat"},
            orig=RuntimeError("orig"),
        )
        text = error_text_without_content(e)
        assert "sehr privat" not in text
        assert "SELECT" not in text

    def test_the_crash_line_uses_the_guard(self):
        """The wiring: not just the function but its use in the crash line."""
        from loguru import logger as _l
        from sqlalchemy.exc import StatementError

        e = StatementError(message="boom", statement="SELECT 1",
                           params={"c": "sehr privat"}, orig=RuntimeError("o"))
        lines: list[str] = []
        sink_id = _l.add(lambda m: lines.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _count(note=e)
            assert any("crashed" in t for t in lines), lines
            assert not any("sehr privat" in t for t in lines), lines
        finally:
            _l.remove(sink_id)

    def test_every_DB_error_log_in_the_retrieval_modules_uses_it(self):
        """The guard was bypassed by the sources themselves in the first
        version: four logged `{e}` raw, `[SQL: ...]` tail and the bound
        user query included.

        The scope is neither arbitrary nor a list that rots: what is
        checked is exactly those `except` handlers whose `try` body
        contains a `.execute(...)` -- there and only there can a SQL tail
        arise, and only there does the cut help. An embedding or LLM error
        carries no `[SQL:` tail; the guard would be theatre. (Closing THAT
        gap needs a different guard, not the same one in more places.)

        Checked on the BODY (`ast`), not by text search over the file -- a
        text search would match my own comments.
        """
        import ast
        import pathlib as _pl

        root = _pl.Path(__file__).resolve().parents[2] / "src/backend/services"

        def has_execute(node) -> bool:
            for n in ast.walk(node):
                if (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "execute"):
                    return True
            return False

        suspicious: list[str] = []
        for name in ("rag_retrieval", "kg_retrieval", "memory_retrieval",
                     "lexical_retrieval", "skill_service",
                     "document_fact_retrieval", "note_retrieval"):
            path = root / f"{name}.py"
            tree = ast.parse(path.read_text())
            for try_node in (n for n in ast.walk(tree) if isinstance(n, ast.Try)):
                if not any(has_execute(a) for a in try_node.body):
                    continue
                for handler in try_node.handlers:
                    for n in ast.walk(handler):
                        if not isinstance(n, ast.JoinedStr):
                            continue
                        raw = ast.unparse(n)
                        if "{e}" in raw and "error_text_without_content" not in raw:
                            suspicious.append(f"{name}:{n.lineno}: {raw[:70]}")

        assert not suspicious, (
            "raw exception in a log line whose `try` contains a query -- the "
            f"SQL tail goes out uncut: {suspicious}")

    def test_the_structural_sweep_FINDS_the_obvious_case(self):
        """Negative control on the check itself: does the sweep find
        anything at all when there is something to find? Without this it
        could fail on a detail and the test would be silently green."""
        import ast

        source = (
            "async def f(self, sql):\n"
            "    try:\n"
            "        return await self.db.execute(sql)\n"
            "    except Exception as e:\n"
            "        logger.warning(f'broken: {e}')\n"
            "        return []\n"
        )
        tree = ast.parse(source)
        hits = []
        for try_node in (n for n in ast.walk(tree) if isinstance(n, ast.Try)):
            hat = any(
                isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute)
                and k.func.attr == "execute"
                for a in try_node.body for k in ast.walk(a))
            if not hat:
                continue
            for handler in try_node.handlers:
                for n in ast.walk(handler):
                    if isinstance(n, ast.JoinedStr) and "{e}" in ast.unparse(n):
                        hits.append(n.lineno)
        assert hits, "the sweep does not even find the obvious case"


class TestTheSummaryLine:
    """The one line per run -- level and content."""

    def test_a_crash_logs_at_WARNING_not_info(self):
        """A crashed source is not a footnote: the answer then rested on
        part of the retrieval. At INFO that would disappear among the
        ordinary lines."""
        from loguru import logger as _l

        captured: list[tuple[str, str]] = []
        sink_id = _l.add(
            lambda m: captured.append(
                (m.record["level"].name, m.record["message"])),
            level="INFO", format="{message}")
        try:
            _count(note=RuntimeError("broken"))
            assert "WARNING" in {s for s, _ in captured}
            assert any("note" in m for _, m in captured)
        finally:
            _l.remove(sink_id)

    def test_a_clean_run_logs_at_INFO(self):
        """Negative control: no warning without a crash, otherwise the
        warning level becomes worthless."""
        from loguru import logger as _l

        captured: list[str] = []
        sink_id = _l.add(lambda m: captured.append(m.record["level"].name),
                         level="INFO", format="{message}")
        try:
            _count()
            assert captured and "WARNING" not in set(captured)
        finally:
            _l.remove(sink_id)

    def test_the_duration_is_in_the_line(self):
        """The duration of the FAN-OUT, named as such. It includes the
        embedding calls and is therefore NOT comparable to the pure SQL
        numbers from the probe (56 versus 408 ms) -- a review finding.
        """
        from loguru import logger as _l

        lines: list[str] = []
        sink_id = _l.add(lambda m: lines.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _count(duration_s=0.408)
            assert any("fan-out 408 ms" in t for t in lines), lines
        finally:
            _l.remove(sink_id)


class TestQueryACTUALLYCallsIt:
    """The classes above test the COUNTING. This one tests that `query`
    calls it -- and that the path marker is right.

    The first version claimed here that it tested the ORDER
    ("classification before the `_wrap_*` helpers"). That was provably
    false: the helpers do not mutate their argument, they return a new
    list -- so the order was irrelevant and the test stayed green when you
    reversed it. A sham check at exactly the spot that called itself "the
    core". The claim is gone; wrapping and counting are one pass anyway.
    """

    @pytest.fixture
    def sources_fixture(self, monkeypatch):
        """All seven retrieval classes replaced, each with the shape its
        wrapper REALLY reads -- read off the wrappers, not guessed. The
        first version gave `rag` a shape without `chunk`/`document`, which
        logged a WARNING on every run and left the rag shape unchecked.

        `query` imports the classes lazily in the body, so patching the
        """
        from unittest.mock import MagicMock

        plan: dict[str, object] = {}

        def install(module_name, cls_name, mapping):
            import importlib
            module = importlib.import_module(f"services.{module_name}")
            inst = MagicMock()
            for methode, source in mapping.items():
                def make(source=source):
                    async def call(*a, **k):
                        value = plan.get(source, _WITH_HITS[source])
                        if isinstance(value, BaseException):
                            raise value
                        if value == "empty":
                            return _WITHOUT_HITS.get(source, [])
                        return value
                    return call
                setattr(inst, methode, make())
            monkeypatch.setattr(module, cls_name, MagicMock(return_value=inst))

        install("rag_retrieval", "RAGRetrieval", {"search": "rag"})
        install("kg_retrieval", "KGRetrieval", {"get_relevant_atoms": "kg"})
        install("memory_retrieval", "MemoryRetrieval", {"retrieve": "memory"})
        install("lexical_retrieval", "LexicalRetrieval", {
            "search_chunks_lexical": "lexical_chunks",
            "search_memories_lexical": "lexical_memories"})
        install("skill_service", "SkillService", {"find_similar": "skill"})
        install("document_fact_retrieval", "DocumentFactRetrieval",
               {"search": "document_fact"})
        install("note_retrieval", "NoteRetrieval", {"search": "note"})
        return plan

    async def _run(self, monkeypatch, **query_kwargs):
        from unittest.mock import MagicMock

        from loguru import logger as _l

        from services.polymorphic_atom_store import PolymorphicAtomStore
        from utils.config import settings

        # Graph expansion would hit the database after fusion.
        monkeypatch.setattr(settings, "graph_expansion_enabled", False)
        # `skill` and `note` are flag-gated. In the container they are off,
        # but live on BOTH instances they are `true` -- without setting them
        # the test measures a configuration that does not exist in production.
        monkeypatch.setattr(settings, "skills_enabled", True)
        monkeypatch.setattr(settings, "notes_enabled", True)

        lines: list[tuple[str, str]] = []
        sink_id = _l.add(
            lambda m: lines.append(
                (m.record["level"].name, m.record["message"])),
            level="INFO", format="{message}")
        try:
            await PolymorphicAtomStore(MagicMock()).query(
                "egal", asker_id=1, max_visible_tier=0, top_k=5,
                **query_kwargs)
        finally:
            _l.remove(sink_id)
        return lines

    @pytest.mark.asyncio
    async def test_a_clean_run_logs_the_summary_line(self, sources_fixture, monkeypatch):
        lines = await self._run(monkeypatch)
        lines_found = [m for _, m in lines if "Atom retrieval" in m]
        assert lines_found, f"no retrieval line in the log: {lines}"
        assert "8/8 sources delivered" in lines_found[0], lines_found[0]
        assert "fan-out" in lines_found[0]

    @pytest.mark.asyncio
    async def test_the_mock_shapes_produce_NO_stray_warning(self, sources_fixture, monkeypatch):
        """Negative control on the double itself. If a shape does not fit,
        the wrapper logs a WARNING -- and then there is no telling whether
        a test is green on the merits or despite a broken double."""
        lines = await self._run(monkeypatch)
        stray = [m for s, m in lines if s == "WARNING" and "Atom retrieval" not in m]
        assert not stray, f"the double triggers a warning: {stray}"

    @pytest.mark.asyncio
    async def test_a_crashing_source_is_COUNTED_through_query(
        self, sources_fixture, monkeypatch
    ):
        from sqlalchemy.exc import InvalidRequestError

        sources_fixture["rag"] = InvalidRequestError(
            "This session is provisioning a new connection; "
            "concurrent operations are not permitted")
        lines = await self._run(monkeypatch)
        summary = [m for _, m in lines if "sources delivered" in m]
        assert summary, f"keine Zusammenfassung: {lines}"
        assert "7/8 sources delivered" in summary[0], summary[0]
        assert "1 crashed" in summary[0]
        assert any(s == "WARNING" for s, m in lines if "sources delivered" in m)

    @pytest.mark.asyncio
    async def test_an_empty_KG_is_counted_as_empty_through_query(
        self, sources_fixture, monkeypatch
    ):
        """The review finding, all the way through `query` -- not only at
        the counting function."""
        sources_fixture["kg"] = "empty"
        lines = await self._run(monkeypatch)
        summary = [m for _, m in lines if "sources delivered" in m]
        assert "7/8 sources delivered" in summary[0], summary[0]
        assert "1 empty" in summary[0]


class TestTheFederatedPathIsMarkedBYTHECALLER:
    """The heaviest finding of the review. The line derived the path from
    `peer is not None` -- and `_peer_scope_for` returns `None` in the
    MAPPED case (`federation_query_responder.py:285`). Mapped is the path
    that runs live. So the one cold run the marker was built for was
    exactly the one that stayed unmarked.
    """

    def test_query_takes_the_path_as_its_OWN_keyword(self):
        import inspect

        from services.polymorphic_atom_store import PolymorphicAtomStore

        sig = inspect.signature(PolymorphicAtomStore.query)
        assert "federated" in sig.parameters
        assert sig.parameters["federated"].default is False, (
            "the default must be `False` -- the local path is the common one")

    def test_the_responder_SETS_it(self):
        """Checked on the BODY (`ast`, docstring stripped), not by text
        search -- a text search would find my own comments."""
        import ast
        import inspect

        from services.federation_query_responder import FederationQueryResponder

        source = inspect.getsource(FederationQueryResponder._retrieve)
        tree = ast.parse(source.lstrip() if source.startswith(" ") else source)
        was_set = False
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                for kw in n.keywords:
                    if kw.arg == "federated" and getattr(
                            kw.value, "value", None) is True:
                        was_set = True
        assert was_set, (
            "`_retrieve` must pass `federated=True` -- otherwise the cold\n"
            "run carries no marker, and cold versus warm is the whole\n"
            "difference")

    def test_peer_is_NOT_used_as_the_path_signal(self):
        """Negative control against sliding back into the old bug: the
        marker must not be derived from `peer` again. The EFFECT is what
        is checked -- `peer` set, `federated` not, so unmarked."""
        from loguru import logger as _l

        from services.polymorphic_atom_store import _wrap_and_count

        lines: list[str] = []
        sink_id = _l.add(lambda m: lines.append(m.record["message"]),
                         level="INFO", format="{message}")
        try:
            _wrap_and_count(
                _sources(), disabled=set(), duration_s=0.05,
                federated=False)
            assert not any("federated" in t for t in lines), lines
            lines.clear()
            _wrap_and_count(
                _sources(), disabled=set(), duration_s=0.05,
                federated=True)
            assert any("federated" in t for t in lines), lines
        finally:
            _l.remove(sink_id)
