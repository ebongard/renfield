"""Atom retrieval reports how many of its eight sources an answer rests on.

🛑 Why this file exists, 2026-10-29: the thin answer of the first successful
federated query. `PolymorphicAtomStore.query` spreads EIGHT retrieval sources
over ONE `AsyncSession` via `asyncio.gather(return_exceptions=True)`.

Measured on the build box against real Postgres, using the project's own
session factory, 8 concurrent queries, three runs, stable:

    one session, cold           1 of 8 delivered      52 ms
    one session, pre-warmed     8 of 8               408 ms   (SERIAL)
    one session per source      8 of 8                56 ms

🛑 And it could not be COUNTED. All six `_wrap_*` helpers start with
`if isinstance(x, Exception) or not x: return []` -- they conflate "crashed"
with "found nothing", and none of them logs. Of the eight sources only three
report a failure themselves; `rag` and `kg` not at all, and those two are
document retrieval and the knowledge graph.

This file tests VISIBILITY (step 1), not the fix. Without it the effect of
step 2 would not be provable in production.

Step 2 landed on 2026-11-15: the federated path gives every source a session of
its own. That is tested against real Postgres in
`test_atom_fanout_source_sessions_pg.py`; this file stays the test of the line.

🛑 The first version was taken apart in review, and two of the findings were
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

#: A shape WITH hits per source. 🛑 Not invented but read off the wrappers:
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

#: A shape WITHOUT hits per source. 🛑 For `kg` that is NOT `{}` but
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
    federated = overrides.pop("federated", False)
    _, counts = _wrap_and_count(
        _sources(**overrides), disabled=off, duration_s=duration, federated=federated)
    return counts




def _raw_db_error_logs(tree) -> list[int]:
    """Line numbers where an `except ... as <name>` handler around a `try`
    that REACHES THE DATABASE formats `<name>` raw into a LOGGER call.

    Only logger calls: a tool result handed back to the asking user's own
    agent is a different question (what the model sees), not a log leak.

    🛑 "Reaches the database" was `.execute(...)` written out inside the `try`
    until 2026-11-15. That missed the responder's own background task: its
    `try` opens a session and calls `self._retrieve(...)`, the query sits two
    calls further down, and the handler put the raw exception into the log
    AND into the audit row. A `try` now counts when its body

      * calls `.execute(...)`, `.commit(...)` or `.flush(...)`, or
      * opens a session (`async with <...Session...>(...)`), or
      * calls a function or method of the SAME module that does any of this,
        directly or through further functions of that module.

    A `try` also counts when it is WRITTEN INSIDE an `async with
    <...Session...>()` block, whatever it calls (2026-11-16: the handler
    around `svc.record_outcome(...)` -- a SELECT FOR UPDATE plus commit on
    another object -- sat inside such a block and was not seen).

    🛑 WHAT THIS DOES NOT SEE. It is a floor under one shape, not a proof
    that no database text reaches a log:

      * a call on ANOTHER OBJECT or into another module is not followed
        (`svc.save(...)`, `other_module.work(db)`) unless the `try` sits
        inside a session block -- that would need the whole program;
      * only an f-string that formats the handler's name DIRECTLY is found:
        not `{str(e)}`, `{repr(e)}`, `{e.args}`, not a variable assigned
        from `str(e)` and logged later, not positional arguments
        (`logger.error("{}", e)`), not `%`-formatting or `+`;
      * no traceback: `logger.opt(exception=True)`, `logger.exception(...)`,
        `traceback.format_exc()` carry the raw message in their last line
        (and, with the app's sink, the variable values of every frame);
      * only a logger NAMED `logger` -- not `log`, `self.logger`, `_l`;
      * only LOG calls: an `error_message` column, a pending record, a tool
        result, a health-page detail are other sinks with the same text;
      * only `services/` is scanned by the test below. `api/`, `ha_glue/`,
        `workers/` have raw exception log lines of their own (107 counted on
        2026-11-15) and are a sweep of their own.

    🛑 AND IT OVER-MATCHES. Any `.execute(` / `.commit(` / `.flush(` counts,
    so a Redis pipeline, a thread-pool executor or a file handle make their
    `try` look like a database one, and so does a session block around a
    handler whose own `try` only reads a file. Wrapping such a handler is
    harmless but not free: the text is capped at 200 characters. That is the
    price of a check without an exemption list -- a list is what rotted last
    time.
    """
    import ast

    levels = {"debug", "info", "warning", "error", "exception", "critical"}
    db_calls = {"execute", "commit", "flush"}

    functions: dict[str, list] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(n.name, []).append(n)

    def opens_session(n) -> bool:
        if not isinstance(n, ast.AsyncWith):
            return False
        for item in n.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call):
                callee = getattr(ctx.func, "id", getattr(ctx.func, "attr", ""))
                if "session" in callee.lower():
                    return True
        return False

    def touches_db(node) -> bool:
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in db_calls):
                return True
            if opens_session(n):
                return True
        return False

    # Every `try` written INSIDE an `async with <Session>()` block: whatever
    # it calls there works on that session, also where the call goes into
    # another object (`svc.record_outcome(...)`), which is not followed.
    inside_a_session = {
        id(t)
        for block in ast.walk(tree) if opens_session(block)
        for t in ast.walk(block) if isinstance(t, ast.Try)
    }

    def module_calls(node) -> set[str]:
        called = set()
        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Name) and f.id in functions:
                called.add(f.id)
            elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                  and f.value.id in ("self", "cls") and f.attr in functions):
                called.add(f.attr)
        return called

    reaching = {name for name, defs in functions.items()
                if any(touches_db(d) for d in defs)}
    grew = True
    while grew:
        grew = False
        for name, defs in functions.items():
            if name not in reaching and any(module_calls(d) & reaching for d in defs):
                reaching.add(name)
                grew = True

    def reaches_db(node) -> bool:
        return touches_db(node) or bool(module_calls(node) & reaching)

    def is_log(call) -> bool:
        f = call.func
        return (isinstance(f, ast.Attribute) and f.attr in levels
                and isinstance(f.value, ast.Name) and f.value.id == "logger")

    lines = []
    for try_node in (n for n in ast.walk(tree) if isinstance(n, ast.Try)):
        if id(try_node) not in inside_a_session and not any(
                reaches_db(a) for a in try_node.body):
            continue
        for handler in try_node.handlers:
            if not handler.name:
                continue
            for call in (c for c in ast.walk(handler)
                         if isinstance(c, ast.Call) and is_log(c)):
                lines.extend(
                    n.lineno for n in ast.walk(call)
                    if isinstance(n, ast.FormattedValue)
                    and isinstance(n.value, ast.Name)
                    and n.value.id == handler.name)
    return sorted(set(lines))


class TestItTellsCrashApartFromEmpty:
    """🛑 The distinction IS the point. An empty source is a measurement, a
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
        """🛑 The class, not the named instance: `kg` was the case that was
        found, but the question applies to all eight. Every source must
        come out as empty when given its own empty shape."""
        for name in _WRAPPERS:
            _, empty, crashed = _count(**{name: "empty"})
            assert empty == [name], f"{name}: empty={empty}"
            assert crashed == []


class TestDisabledSourcesAreNotEmptySources:
    """🛑 A disabled source was never asked. Counting it as empty would make
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
    real path cannot produce that state -- five sources caught their own exception and
    returned `[]`, so they arrived as "empty".

    Three of them (`lexical_chunks`, `lexical_memories`, `note`) have no caller
    outside the fan-out (measured) and now re-raise. `memory`,
    `document_fact` and `kg` have external callers and still swallow --
    that is an open decision, not an oversight, and this test pins down the
    current state so the number cannot quietly shift later.
    """

    def test_the_three_callerless_sources_report_a_crash(self):
        from sqlalchemy.exc import InvalidRequestError

        cold_error = InvalidRequestError(
            "This session is provisioning a new connection; "
            "concurrent operations are not permitted")
        _, _, crashed = _count(
            lexical_chunks=cold_error, lexical_memories=cold_error, note=cold_error)
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

    🛑 What this is NOT: a ban on content in the log. The repo logs the query
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

    @pytest.mark.parametrize("label", ["DETAIL", "HINT", "CONTEXT"])
    def test_the_lines_postgres_puts_row_values_in_are_cut_off(self, label):
        """They sit BEFORE the SQL tail. The real errors are raised in
        `test_log_safety_pg.py`; this is the shape, without a database."""
        text = error_text_without_content(RuntimeError(
            'duplicate key value violates unique constraint "uq_x"\n'
            f"{label}:  Key (name)=(Scheidungsurteil) already exists.\n"
            "[SQL: INSERT INTO x (name) VALUES (%(n)s)]"))
        assert text == 'duplicate key value violates unique constraint "uq_x"'

    def test_a_word_that_merely_contains_a_label_is_not_a_cut(self):
        """Negative control: `DETAILS` or a lower-case `detail:` is prose."""
        plain = "upstream said: detail: none, DETAILS follow, HINTS too"
        assert error_text_without_content(RuntimeError(plain)) == plain

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
        """🛑 The wiring: not just the function but its use in the crash line."""
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

    def test_no_raw_DB_error_log_anywhere_in_services(self):
        """🛑 The guard was bypassed by the sources themselves in the first
        version: four logged `{e}` raw, `[SQL: ...]` tail and the bound
        user query included.

        What is checked: every `except ... as <name>` handler whose `try`
        body reaches the database (see `_raw_db_error_logs` for what that
        means) -- there and only there can a SQL tail arise, and only there
        does the cut help -- must not put `<name>` raw into a log call. An
        embedding or LLM error carries no `[SQL:` tail; the guard would be
        theatre there. (Closing THAT gap needs a different guard, not the
        same one in more places.)

        🛑 The scope is ALL of `services/`. Until 2026-10-30 it was a hard
        list of seven retrieval modules whose comment claimed it was
        "neither arbitrary nor a list that rots" -- and it missed 55 log
        sites in 24 other files, among them `conversation_service` logging
        the raw INSERT of a chat message. All are fixed (#1433 and the
        follow-up); the interim per-file ratchet is gone with them.

        Checked on the BODY (`ast`), not by text search over the file -- a
        text search would match my own comments.
        """
        import ast
        import pathlib as _pl

        root = _pl.Path(__file__).resolve().parents[2] / "src/backend/services"
        found = {}
        for path in sorted(root.rglob("*.py")):
            hits = _raw_db_error_logs(ast.parse(path.read_text()))
            if hits:
                found[path.relative_to(root).as_posix()] = hits
        assert len(list(root.rglob("*.py"))) > 50, "scanned the wrong directory"
        assert not found, (
            "raw exception in a log line whose `try` contains a query -- the "
            "SQL tail goes out uncut; wrap it in "
            f"`error_text_without_content`: {found}")

    def test_the_structural_sweep_FINDS_the_obvious_case(self):
        """🛑 Negative control on the check itself -- on the SAME helper the
        sweep uses, not a copy of it (the first version tested its own
        re-implementation). Any handler name counts, not only `e`; the
        wrapped form does not count; an f-string outside a logger call does
        not count; a handler around a non-query `try` does not count."""
        import ast

        source = (
            "async def f(self, sql):\n"
            "    try:\n"
            "        return await self.db.execute(sql)\n"
            "    except Exception as exc:\n"
            "        logger.warning(f'broken: {exc}')\n"
            "        logger.warning(f'ok: {error_text_without_content(exc)}')\n"
            "        return {'message': f'tool result, not a log: {exc}'}\n"
            "    try:\n"
            "        return embed(sql)\n"
            "    except Exception as e:\n"
            "        logger.warning(f'not a query: {e}')\n"
        )
        assert _raw_db_error_logs(ast.parse(source)) == [5]

    def test_the_sweep_sees_a_query_that_is_REACHED_not_written_out(self):
        """🛑 The shape that got past the first guard: the handler sits in a
        function that never says `.execute` itself. Each case below is one
        way a `try` reaches the database; the last two are the negative
        controls -- a helper that touches nothing, and a call into another
        module, which is deliberately not followed."""
        import ast

        source = (
            "class R:\n"
            "    async def _retrieve(self, session):\n"
            "        return await session.execute('q')\n"
            "    async def _middle(self, session):\n"
            "        return await self._retrieve(session)\n"
            "    async def via_two_calls(self, session):\n"
            "        try:\n"
            "            await self._middle(session)\n"
            "        except Exception as e:\n"
            "            logger.error(f'two calls down: {e}')\n"          # 10
            "    async def via_own_session(self):\n"
            "        try:\n"
            "            async with AsyncSessionLocal() as session:\n"
            "                await other_module.work(session)\n"
            "        except Exception as e:\n"
            "            logger.error(f'own session: {e!r}')\n"           # 16
            "    async def via_commit(self, peer):\n"
            "        try:\n"
            "            peer.seen = 1\n"
            "            await self.db.commit()\n"
            "        except Exception as e:\n"
            "            logger.warning(f'commit: {e}')\n"                # 22
            "    async def inside_a_session_block(self, ids):\n"
            "        async with AsyncSessionLocal() as db:\n"
            "            svc = Service(db)\n"
            "            for i in ids:\n"
            "                try:\n"
            "                    await svc.record(i)\n"
            "                except Exception as e:\n"
            "                    logger.warning(f'other object: {e}')\n"      # 30
            "    async def harmless_helper(self):\n"
            "        return 1\n"
            "    async def no_database(self):\n"
            "        try:\n"
            "            await self.harmless_helper()\n"
            "        except Exception as e:\n"
            "            logger.warning(f'nothing to cut: {e}')\n"
            "    async def another_module(self):\n"
            "        try:\n"
            "            await other_module.work()\n"
            "        except Exception as e:\n"
            "            logger.warning(f'not followed: {e}')\n"
        )
        assert _raw_db_error_logs(ast.parse(source)) == [10, 16, 22, 30]

    def test_the_stack_is_kept_and_everything_else_dropped(self):
        """`frames_without_content`: where it was raised, without the message,
        the source lines or any value -- what replaces a full traceback at
        the handlers around database work."""
        from utils.log_safety import frames_without_content

        def inner(secret):
            raise RuntimeError(f"primary {secret} [SQL: SELECT 1] [parameters: ('{secret}',)]")

        def outer(secret):
            inner(secret)

        try:
            outer("zz-private-value-zz")
        except RuntimeError as e:
            text = frames_without_content(e)
        assert "zz-private-value-zz" not in text and "SQL" not in text
        # Outermost first, file and function named.
        assert text.index("in outer") < text.index("in inner")
        assert "test_atom_store_source_visibility.py:" in text
        assert frames_without_content(RuntimeError("never raised")) == "no stack"

    def test_no_traceback_is_logged_around_database_work_in_the_named_handlers(self):
        """🛑 The four handlers that logged `logger.opt(exception=True)` around
        work that can raise a database error (three in the orchestrator's
        sub-agent run, the `pre_mcp_call` handler loop): a traceback ends in
        the raw message. Checked on the body, per function."""
        import ast
        import pathlib as _pl

        services = _pl.Path(__file__).resolve().parents[2] / "src/backend/services"
        for file, function in (("orchestrator.py", "_run_sub_agent"),
                               ("action_executor.py", "execute")):
            tree = ast.parse((services / file).read_text())
            found = [n for n in ast.walk(tree)
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == function]
            assert found, f"{file}: no function {function} -- renamed?"
            for fn in found:
                tracebacks = [
                    n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "opt"
                    and any(k.arg == "exception" for k in n.keywords)]
                assert tracebacks == [], f"{file}:{tracebacks}"

    def test_the_responder_task_is_inside_the_sweep(self):
        """The function this widening was made for is actually reached by it
        -- on its BODY: with its handler put back to the raw form the sweep
        reports it."""
        import ast
        import pathlib as _pl

        path = (_pl.Path(__file__).resolve().parents[2]
                / "src/backend/services/federation_query_responder.py")
        tree = ast.parse(path.read_text())
        (task,) = [n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_query"]
        assert _raw_db_error_logs(tree) == []

        handler = next(h for t in ast.walk(task) if isinstance(t, ast.Try)
                       for h in t.handlers if h.name == "e")
        raw = ast.parse("logger.error(f'failed: {e}')").body[0]
        handler.body.insert(0, ast.copy_location(raw, handler.body[0]))
        ast.fix_missing_locations(tree)
        assert _raw_db_error_logs(tree) != []


class TestTheSummaryLine:
    """The one line per run -- level and content."""

    def test_a_crash_logs_at_WARNING_not_info(self):
        """🛑 A crashed source is not a footnote: the answer then rested on
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
    """🛑 The classes above test the COUNTING. This one tests that `query`
    calls it -- and that the path marker is right.

    🛑 The first version claimed here that it tested the ORDER
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

        `query` imports the classes lazily in the body, so the patch takes
        effect on the module.
        """
        from unittest.mock import MagicMock

        plan: dict[str, object] = {}

        def install(module_name, cls_name, mapping):
            import importlib
            module = importlib.import_module(f"services.{module_name}")
            inst = MagicMock()
            for method_name, source in mapping.items():
                def make(source=source):
                    async def call(*a, **k):
                        value = plan.get(source, _WITH_HITS[source])
                        if isinstance(value, BaseException):
                            raise value
                        if value == "empty":
                            return _WITHOUT_HITS.get(source, [])
                        return value
                    return call
                setattr(inst, method_name, make())
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
        # 🛑 `skill` and `note` are flag-gated. In the container they are off,
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
        """🛑 Negative control on the double itself. If a shape does not fit,
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
        assert summary, f"no summary: {lines}"
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
    """🛑 The heaviest finding of the review. The line derived the path from
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
        """🛑 Negative control against sliding back into the old bug: the
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
