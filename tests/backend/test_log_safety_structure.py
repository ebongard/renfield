"""No raw exception in a log line, no log sink that prints variable values --
checked on the code of the whole backend.

Three structural checks, each with a negative control on the SAME helper the
sweep uses:

  * `_raw_error_logs`: an `except` handler that puts its exception raw into a
    log call (the message, or a traceback, which ends in the message). A
    database error carries its statement, the bound parameters and the
    offending row in that text -- `utils/log_safety.py` says where.
  * `_sinks_that_print_values`: a loguru sink added without `diagnose=False`.
  * `_entry_points_without_the_sink`: a process entry point that never
    installs the sink and so logs through loguru's built-in one.

🛑 History of the scope, because the scope was the defect twice. First a hard
list of seven retrieval modules (missed 55 log sites in 24 other files), then
all of `services/` (#1562) -- with everything outside it left as "a sweep of
its own": 437 log lines in 75 files, counted on 2026-11-16 the way
`_raw_error_logs(..., every_handler=True)` counts. Since that day the scope is
`src/backend/`, and it is the sweep's own walk that decides it
(`_sweep`), tested against a planted tree.

All checks read the BODY (`ast`). A text search over the source would match
the comments and docstrings that describe the very thing it looks for.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src/backend"

#: Staged copies of other trees that `bin/stage-backend-build.sh` puts into
#: `src/backend/` for the image build (gitignored): not backend code, and
#: linted and tested where they live.
_STAGED_COPIES = ("satellite", "wakeword-models")

_LEVELS = {"trace", "debug", "info", "success", "warning", "error", "exception",
           "critical", "log"}
_DB_CALLS = {"execute", "commit", "flush"}
_SESSION_NAMES = {"db", "session", "db_session", "_db", "_session"}
#: Calls that may take the exception: the two cutters, and the two that read
#: only its class.
_CONTENT_FREE = {"error_text_without_content", "frames_without_content",
                 "type", "isinstance"}


def _raw_error_logs(tree, *, every_handler: bool) -> list[int]:
    """Line numbers of LOG calls inside an `except` handler that carry the
    handler's exception raw.

    WHICH HANDLERS. With `every_handler=True` all of them. That is the rule
    outside `services/`: routes, WebSocket handlers, workers and the start-up
    code are glue -- the query is always in another module, and following
    calls across modules is what this check cannot do. With
    `every_handler=False` only a handler around a `try` that REACHES THE
    DATABASE, which is the rule inside `services/` (there an embedding or LLM
    error far from any query keeps its full text; see the sweep test). A
    `try` reaches the database when its body

      * calls `.execute(...)`, `.commit(...)` or `.flush(...)`, or
      * opens a session (`async with <...Session...>(...)`), or
      * names a session (`db`, `session`, `self.db`, ... -- `_SESSION_NAMES`),
        i.e. hands one to whatever it calls, or
      * calls a function or method of the SAME module that does any of this,
        directly or through further functions of that module,

    or when it is WRITTEN INSIDE an `async with <...Session...>()` block or
    inside a function that takes a session as a parameter (by name or by an
    annotation that says `Session`), whatever it calls there.

    WHAT COUNTS AS RAW, in a call `<logger>.<level>(...)`:

      * the handler's name anywhere in the arguments -- `{e}`, `{e!r}`,
        `{str(e)}`, `e.args`, a positional `e` -- unless it sits inside a
        call of `_CONTENT_FREE`;
      * a traceback: `.exception(...)`, `.opt(exception=...)`, `exc_info=...`,
        `traceback.format_exc()` as an argument. It ends in `str(e)`.

    A logger is a receiver whose name contains `log` (`logger`, `log`,
    `self.logger`, `_migration_logger`), a name bound by `from loguru import
    logger as <x>` or one assigned from `...getLogger(...)`.

    🛑 WHAT THIS DOES NOT SEE. It is a floor under one shape, not a proof that
    no database text reaches a log:

      * with `every_handler=False`: a call on ANOTHER OBJECT or into another
        module that opens its own session (`other_module.work()`), when no
        session is in sight at the `try`. 254 raw log lines stood in
        `services/` on 2026-11-16 behind that rule; some of them are this;
      * the text taking a detour: `msg = str(e)` logged on the next line or
        after the handler, a helper that formats it (`_describe(e)`), an
        exception stored and logged elsewhere, `traceback.format_exc()`
        assigned first;
      * an attribute bound first (`code = e.status_code` is fine, `detail =
        e.args` is not -- neither is seen);
      * a logger under a name without `log` that is passed in or built by a
        factory;
      * `print(...)`, `traceback.print_exc()`, `warnings.warn(...)`;
      * what never passes a handler: an exception that leaves a route is
        logged by uvicorn with its traceback, raw message included; so is
        one that leaves an `asyncio` task ("Task exception was never
        retrieved");
      * every sink that is not a log: an `error_message` column, a pending
        record, a tool result, an HTTP `detail`, a WebSocket error frame.

    🛑 AND IT OVER-MATCHES. With `every_handler=False` any `.execute(` /
    `.commit(` / `.flush(` counts, so a Redis pipeline or a thread-pool
    executor make their `try` look like a database one; with
    `every_handler=True` an `OSError` from deleting a file is cut like a
    database error. `{e.status_code}` counts as raw as well. The price of the
    wrap is the cap at 200 characters; the price of an exemption list is that
    it rots -- it did, once.
    """
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

    def names_a_session(n) -> bool:
        return ((isinstance(n, ast.Name) and n.id in _SESSION_NAMES)
                or (isinstance(n, ast.Attribute) and n.attr in _SESSION_NAMES))

    def touches_db(node) -> bool:
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in _DB_CALLS):
                return True
            if opens_session(n) or names_a_session(n):
                return True
        return False

    def takes_a_session(fn) -> bool:
        a = fn.args
        return any(
            p.arg in _SESSION_NAMES
            or (p.annotation is not None and "Session" in ast.unparse(p.annotation))
            for p in a.posonlyargs + a.args + a.kwonlyargs)

    # Every `try` written INSIDE a session block or inside a function that was
    # handed a session: whatever it calls there can work on that session, also
    # where the call goes into another object (`svc.record_outcome(...)`).
    holds_a_session = [b for b in ast.walk(tree) if opens_session(b)]
    holds_a_session += [f for defs in functions.values() for f in defs
                        if takes_a_session(f)]
    inside_a_session = {id(t) for block in holds_a_session
                        for t in ast.walk(block) if isinstance(t, ast.Try)}

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

    # Loggers that do not say so in their name.
    bound_loggers = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "loguru":
            bound_loggers |= {a.asname or a.name for a in n.names}
        elif (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
              and getattr(n.value.func, "attr", "") == "getLogger"):
            bound_loggers |= {t.id for t in n.targets if isinstance(t, ast.Name)}

    def is_logger(v) -> bool:
        # Through `logger.opt(...)` / `logger.bind(...)` down to the name.
        while isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute):
            v = v.func.value
        name = getattr(v, "id", getattr(v, "attr", ""))
        return "log" in name.lower() or name in bound_loggers

    def is_log(call) -> bool:
        f = call.func
        return isinstance(f, ast.Attribute) and f.attr in _LEVELS and is_logger(f.value)

    def switched_on(keyword) -> bool:
        return not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)

    def carries_a_traceback(call) -> bool:
        if call.func.attr == "exception":
            return True
        if any(k.arg == "exc_info" and switched_on(k) for k in call.keywords):
            return True
        v = call.func.value
        while isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute):
            if v.func.attr == "opt" and any(
                    k.arg == "exception" and switched_on(k) for k in v.keywords):
                return True
            v = v.func.value
        return any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "format_exc"
                   for a in call.args for n in ast.walk(a))

    def raw(node, name) -> bool:
        if isinstance(node, ast.Call):
            callee = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if callee in _CONTENT_FREE:
                return False
        if isinstance(node, ast.Name) and node.id == name:
            return True
        return any(raw(child, name) for child in ast.iter_child_nodes(node))

    lines = []
    for try_node in (n for n in ast.walk(tree) if isinstance(n, ast.Try)):
        if not every_handler and id(try_node) not in inside_a_session and not any(
                reaches_db(a) for a in try_node.body):
            continue
        for handler in try_node.handlers:
            for call in (c for c in ast.walk(handler)
                         if isinstance(c, ast.Call) and is_log(c)):
                arguments = list(call.args) + [k.value for k in call.keywords]
                if carries_a_traceback(call) or (handler.name and any(
                        raw(a, handler.name) for a in arguments)):
                    lines.append(call.lineno)
    return sorted(set(lines))


def _sweep(root: pathlib.Path) -> tuple[dict[str, list[int]], set[str]]:
    """`_raw_error_logs` over every Python file below `root`: `(findings by
    relative path, the top-level names that were walked)`.

    The scope is decided HERE and nowhere else: everything except the staged
    copies, `services/` by the reach rule and the rest by every handler."""
    found: dict[str, list[int]] = {}
    walked: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] in _STAGED_COPIES:
            continue
        walked.add(rel.parts[0])
        hits = _raw_error_logs(ast.parse(path.read_text(encoding="utf-8")),
                               every_handler=rel.parts[0] != "services")
        if hits:
            found[rel.as_posix()] = hits
    return found, walked


class TestNoRawExceptionInALogLine:

    def test_nowhere_in_the_backend(self):
        """🛑 Every handler outside `services/`, every handler around database
        work inside it (`_raw_error_logs` for what both mean and for what
        neither sees).

        Why two rules and not the strict one everywhere: inside `services/`
        the cut would also take the tail of an LLM, MCP or HTTP error that
        has no `[SQL:` in it, and the 200-character cap costs exactly the
        diagnosis those lines exist for (the trade-off is written down in
        `utils/log_safety.py`). Outside `services/` the reach rule finds too
        little to be worth its name -- 200 of the 437 lines -- because the
        query is never written where the handler is.
        """
        found, walked = _sweep(BACKEND)
        # The walk went where it was meant to go -- an empty result from the
        # wrong directory is green too.
        assert {"api", "ha_glue", "workers", "utils", "auth", "services",
                "main.py", "alembic"} <= walked, walked
        assert not (walked & set(_STAGED_COPIES))
        assert not found, (
            "raw exception in a log line -- a database error goes out with its "
            "statement, parameters and row. Use `error_text_without_content(e)` "
            "for the message and `frames_without_content(e)` instead of a "
            f"traceback: {found}")

    def test_the_sweep_decides_the_scope_and_FINDS_a_planted_violation(self, tmp_path):
        """🛑 Negative control on the WALK, through the same `_sweep` the real
        check uses: one planted violation per kind of place. Found where it
        must be, by the rule that applies there, and not in a staged copy."""
        no_query = (
            "def f():\n"
            "    try:\n"
            "        work()\n"
            "    except Exception as e:\n"
            "        logger.error(f'failed: {e}')\n"          # 5
        )
        with_query = no_query.replace("work()", "db.execute(q)")
        for rel, source in (
            ("main.py", no_query),
            ("api/routes/planted.py", no_query),
            ("ha_glue/services/planted.py", no_query),
            ("workers/planted.py", no_query),
            ("services/with_query.py", with_query),
            ("services/sub/with_query.py", with_query),
            ("services/no_query.py", no_query),               # the reach rule
            ("satellite/planted.py", with_query),             # staged copy
            ("wakeword-models/planted.py", with_query),       # staged copy
            ("api/clean.py", no_query.replace("{e}", "{error_text_without_content(e)}")),
        ):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")

        found, walked = _sweep(tmp_path)
        assert found == {
            "main.py": [5],
            "api/routes/planted.py": [5],
            "ha_glue/services/planted.py": [5],
            "workers/planted.py": [5],
            "services/with_query.py": [5],
            "services/sub/with_query.py": [5],
        }
        assert walked == {"main.py", "api", "ha_glue", "workers", "services"}

    def test_a_violation_planted_into_REAL_files_is_reported(self):
        """The same on the real tree: a route module, the worker and the
        start-up code are clean, and each is reported once a handler of its
        own gets the raw line back. Proves the files are parsed and their
        handlers walked, not just that the directory exists."""
        for rel in ("api/lifecycle.py", "api/routes/knowledge.py",
                    "workers/document_processor_worker.py", "ha_glue/bootstrap.py",
                    "main.py"):
            tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"))
            assert _raw_error_logs(tree, every_handler=True) == [], rel

            handler = next(h for t in ast.walk(tree) if isinstance(t, ast.Try)
                           for h in t.handlers if h.name)
            raw = ast.parse(f"logger.error(f'failed: {{{handler.name}}}')").body[0]
            handler.body.insert(0, ast.copy_location(raw, handler.body[0]))
            ast.fix_missing_locations(tree)
            assert _raw_error_logs(tree, every_handler=True) != [], rel

    def test_the_responder_task_is_inside_the_sweep(self):
        """The function the reach rule was widened for in #1562 is reached by
        it -- on its BODY: with its handler put back to the raw form it is
        reported."""
        path = BACKEND / "services/federation_query_responder.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        (task,) = [n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_query"]
        assert _raw_error_logs(tree, every_handler=False) == []

        handler = next(h for t in ast.walk(task) if isinstance(t, ast.Try)
                       for h in t.handlers if h.name == "e")
        raw = ast.parse("logger.error(f'failed: {e}')").body[0]
        handler.body.insert(0, ast.copy_location(raw, handler.body[0]))
        ast.fix_missing_locations(tree)
        assert _raw_error_logs(tree, every_handler=False) != []

    def test_the_reach_rule_FINDS_the_obvious_case(self):
        """🛑 Negative control on the check itself. Any handler name counts,
        not only `e`; the wrapped form does not count; an f-string outside a
        logger call does not count; a handler around a non-query `try` does
        not count under the reach rule -- and does under `every_handler`."""
        source = (
            "async def f(self, sql):\n"
            "    try:\n"
            "        return await self.pool.execute(sql)\n"
            "    except Exception as exc:\n"
            "        logger.warning(f'broken: {exc}')\n"
            "        logger.warning(f'ok: {error_text_without_content(exc)}')\n"
            "        return {'message': f'tool result, not a log: {exc}'}\n"
            "    try:\n"
            "        return embed(sql)\n"
            "    except Exception as e:\n"
            "        logger.warning(f'not a query: {e}')\n"
        )
        tree = ast.parse(source)
        assert _raw_error_logs(tree, every_handler=False) == [5]
        assert _raw_error_logs(tree, every_handler=True) == [5, 11]

    def test_the_reach_rule_sees_a_query_that_is_REACHED_not_written_out(self):
        """🛑 The shape that got past the first guard: the handler sits in a
        function that never says `.execute` itself. Each case is one way a
        `try` reaches the database; the last two are the negative controls
        -- a helper that touches nothing, and a call into another module
        with no session in sight, which is deliberately not followed."""
        source = (
            "class R:\n"
            "    async def _retrieve(self, conn):\n"
            "        return await conn.execute('q')\n"
            "    async def _middle(self, conn):\n"
            "        return await self._retrieve(conn)\n"
            "    async def via_two_calls(self, conn):\n"
            "        try:\n"
            "            await self._middle(conn)\n"
            "        except Exception as e:\n"
            "            logger.error(f'two calls down: {e}')\n"          # 10
            "    async def via_own_session(self):\n"
            "        try:\n"
            "            async with AsyncSessionLocal() as s:\n"
            "                await other_module.work(s)\n"
            "        except Exception as e:\n"
            "            logger.error(f'own session: {e!r}')\n"           # 16
            "    async def via_commit(self, peer):\n"
            "        try:\n"
            "            peer.seen = 1\n"
            "            await self.pool.commit()\n"
            "        except Exception as e:\n"
            "            logger.warning(f'commit: {e}')\n"                # 22
            "    async def inside_a_session_block(self, ids):\n"
            "        async with AsyncSessionLocal() as s:\n"
            "            svc = Service(s)\n"
            "            for i in ids:\n"
            "                try:\n"
            "                    await svc.record(i)\n"
            "                except Exception as e:\n"
            "                    logger.warning(f'other object: {e}')\n"  # 30
            "    async def session_handed_on(self):\n"
            "        try:\n"
            "            await other_module.work(self.db)\n"
            "        except Exception as e:\n"
            "            logger.warning(f'handed on: {e}')\n"             # 35
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
            "async def a_route(item_id, db=Depends(get_db)):\n"
            "    svc = Service(db)\n"
            "    try:\n"
            "        await svc.save(item_id)\n"
            "    except Exception as e:\n"
            "        logger.warning(f'holds a session: {e}')\n"           # 53
            "async def by_annotation(item_id, s: AsyncSession):\n"
            "    svc = Service(s)\n"
            "    try:\n"
            "        await svc.save(item_id)\n"
            "    except Exception as e:\n"
            "        logger.warning(f'annotated: {e}')\n"                 # 59
        )
        assert _raw_error_logs(ast.parse(source), every_handler=False) == [
            10, 16, 22, 30, 35, 53, 59]

    def test_every_form_the_exception_can_take_in_a_log_call(self):
        """🛑 Until 2026-11-16 only `{e}` in an f-string on a logger NAMED
        `logger` was seen; every other line below was a documented blind
        spot. One line per form; the block at the end is what must NOT
        count."""
        source = (
            "from loguru import logger as _l\n"
            "audit = logging.getLogger('audit')\n"
            "def f(self):\n"
            "    try:\n"
            "        work()\n"
            "    except Exception as e:\n"
            "        logger.error(f'{str(e)}')\n"                          # 7
            "        logger.error(f'{repr(e)}')\n"                         # 8
            "        logger.error(f'{e.args}')\n"                          # 9
            "        logger.error('failed: {}', e)\n"                      # 10
            "        logger.error('failed: %s', str(e))\n"                 # 11
            "        logger.error('failed: ' + str(e))\n"                  # 12
            "        logger.error('failed', error=e)\n"                    # 13
            "        logger.exception('failed')\n"                         # 14
            "        logger.opt(exception=True).error('failed')\n"         # 15
            "        logger.opt(exception=e).error('failed')\n"            # 16
            "        logger.error('failed', exc_info=True)\n"              # 17
            "        logger.error(traceback.format_exc())\n"               # 18
            "        log.error(f'{e}')\n"                                  # 19
            "        self.logger.error(f'{e}')\n"                          # 20
            "        _l.error(f'{e}')\n"                                   # 21
            "        audit.error(f'{e}')\n"                                # 22
            "        logger.bind(a=1).warning(f'{e}')\n"                   # 23
            "        logger.error(f'{e:>20}')\n"                           # 24
            "        try:\n"
            "            undo()\n"
            "        except Exception:\n"
            "            logger.error(f'undo failed after {e}')\n"         # 28
            "        logger.error(f'{type(e).__name__}: {error_text_without_content(e)}')\n"
            "        logger.error(f'at {frames_without_content(e)}')\n"
            "        logger.opt(exception=False).error('no traceback')\n"
            "        logger.opt(depth=1).error('no traceback')\n"
            "        logger.error('no traceback', exc_info=False)\n"
            "        if isinstance(e, ValueError):\n"
            "            logger.error('a kind of error')\n"
            "        results.error(f'not a logger: {e}')\n"
            "        raise HTTPException(500, detail=str(e))\n"
        )
        assert _raw_error_logs(ast.parse(source), every_handler=True) == [*range(7, 25), 28]

    def test_an_unnamed_handler_cannot_hide_a_traceback(self):
        source = (
            "def f():\n"
            "    try:\n"
            "        work()\n"
            "    except Exception:\n"
            "        logger.opt(exception=True).warning('failed')\n"       # 5
            "    except:\n"
            "        logger.exception('failed')\n"                         # 7
        )
        assert _raw_error_logs(ast.parse(source), every_handler=True) == [5, 7]


# --- The sink ----------------------------------------------------------------


def _loguru_names(tree) -> set[str]:
    """Under which names loguru's logger is reachable in this module."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "loguru":
            names |= {a.asname or a.name for a in n.names if a.name == "logger"}
    return names


def _sinks_that_print_values(tree) -> tuple[list[int], int]:
    """`(lines where a loguru sink is added without diagnose=False, number of
    sinks seen)`.

    A sink is `<loguru logger>.add(...)` -- also through `.opt(...)` /
    `.bind(...)` and as `loguru.logger.add(...)` -- and every entry of
    `<loguru logger>.configure(handlers=[...])`. Only the literal `False`
    passes: a variable, `**kwargs` or a handler list built elsewhere is
    reported, because it cannot be read here.

    🛑 Not seen: a module that reaches loguru's logger under a name it did not
    import itself (`from utils.x import logger`), `add = logger.add`, and
    loguru's BUILT-IN sink, which exists without any call -- the entry-point
    check below is for that one.
    """
    names = _loguru_names(tree)

    def is_loguru(v) -> bool:
        while isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute):
            v = v.func.value
        if isinstance(v, ast.Name):
            return v.id in names
        return (isinstance(v, ast.Attribute) and v.attr == "logger"
                and getattr(v.value, "id", "") == "loguru")

    def off(node) -> bool:
        return isinstance(node, ast.Constant) and node.value is False

    lines, seen = [], 0
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        f = call.func
        if not (isinstance(f, ast.Attribute) and is_loguru(f.value)):
            continue
        if f.attr == "add":
            seen += 1
            if not any(k.arg == "diagnose" and off(k.value) for k in call.keywords):
                lines.append(call.lineno)
        elif f.attr == "configure":
            for k in call.keywords:
                if k.arg != "handlers":
                    continue
                entries = k.value.elts if isinstance(k.value, (ast.List, ast.Tuple)) else [None]
                for entry in entries:
                    seen += 1
                    keys = ({getattr(key, "value", None): value
                             for key, value in zip(entry.keys, entry.values, strict=True)}
                            if isinstance(entry, ast.Dict) else {})
                    if not ("diagnose" in keys and off(keys["diagnose"])):
                        lines.append(call.lineno)
    return sorted(set(lines)), seen


def _entry_points_without_the_sink(tree) -> bool:
    """True when the module is a process entry point -- it has an `if
    __name__ == "__main__":` block -- and neither that block nor the module's
    top level calls `install_log_sink(...)`.

    Such a process logs through loguru's built-in sink: `diagnose=True`."""
    def is_main_block(n) -> bool:
        return (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and getattr(n.test.left, "id", "") == "__name__")

    if not any(is_main_block(n) for n in tree.body):
        return False
    top_level = [n for n in tree.body
                 if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return not any(
        isinstance(c, ast.Call) and getattr(c.func, "id", getattr(c.func, "attr", "")) == "install_log_sink"
        for n in top_level for c in ast.walk(n))


def _backend_modules():
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] not in _STAGED_COPIES:
            yield rel.as_posix(), ast.parse(path.read_text(encoding="utf-8"))


class TestNoSinkPrintsVariableValues:
    """loguru's `diagnose` defaults to True: under every source line of a
    traceback it prints the values of the names on it. A probe on 2026-11-16
    showed a failing query's statement and parameters coming out of the
    frames."""

    def test_every_sink_in_the_backend_is_added_with_diagnose_off(self):
        found, seen = {}, 0
        for rel, tree in _backend_modules():
            lines, n = _sinks_that_print_values(tree)
            seen += n
            if lines:
                found[rel] = lines
        # The detector saw the real one -- zero findings among zero sinks
        # would be green as well.
        assert seen >= 1, "no loguru sink found at all -- the detector went blind"
        assert not found, (
            "a loguru sink without `diagnose=False` prints variable values "
            f"into tracebacks; use `utils.log_safety.install_log_sink`: {found}")

    def test_the_sink_check_FINDS_every_way_to_get_it_wrong(self):
        """🛑 Negative control, on the same helper."""
        source = (
            "import loguru\n"
            "from loguru import logger\n"
            "from loguru import logger as _l\n"
            "logger.add(sys.stderr, level='INFO')\n"                              # 4
            "logger.add(sys.stderr, diagnose=True)\n"                             # 5
            "logger.add(sys.stderr, diagnose=flag)\n"                             # 6
            "logger.add(sys.stderr, **options)\n"                                 # 7
            "_l.add('file.log')\n"                                                # 8
            "loguru.logger.add(sys.stderr)\n"                                     # 9
            "logger.configure(handlers=[{'sink': sys.stderr}])\n"                 # 10
            "logger.configure(handlers=built_elsewhere)\n"                        # 11
            "logger.add(sys.stderr, backtrace=True, diagnose=False)\n"
            "logger.configure(handlers=[{'sink': sys.stderr, 'diagnose': False}])\n"
            "logger.configure(extra={'a': 1})\n"
            "seen.add(item)\n"
            "self.items.add(item)\n"
        )
        assert _sinks_that_print_values(ast.parse(source)) == ([4, 5, 6, 7, 8, 9, 10, 11], 10)

    def test_every_process_installs_the_sink(self):
        """The three workers are processes of their own (`python -m
        workers.<name>`) and never import `main.py`; until 2026-11-16 they
        logged through the built-in sink."""
        entry_points, missing = [], []
        for rel, tree in _backend_modules():
            if any(isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)
                   for n in tree.body):
                entry_points.append(rel)
                if _entry_points_without_the_sink(tree):
                    missing.append(rel)
        assert {"main.py", "workers/document_processor_worker.py",
                "workers/meeting_worker.py",
                "workers/pdf_split_worker.py"} <= set(entry_points), entry_points
        assert not missing, (
            "an entry point that does not call `install_log_sink` logs through "
            f"loguru's built-in sink (diagnose=True): {missing}")

    def test_the_entry_point_check_FINDS_a_process_without_the_sink(self):
        """🛑 Negative control: a call inside a function that is never the
        entry does not count; a module without a `__main__` block is not an
        entry point."""
        block = "if __name__ == '__main__':\n    asyncio.run(main())\n"
        assert _entry_points_without_the_sink(ast.parse(block))
        assert _entry_points_without_the_sink(ast.parse(
            "def setup():\n    install_log_sink('INFO')\n" + block))
        assert not _entry_points_without_the_sink(ast.parse(
            "if __name__ == '__main__':\n    install_log_sink('INFO')\n    asyncio.run(main())\n"))
        assert not _entry_points_without_the_sink(ast.parse(
            "install_log_sink(os.getenv('LOG_LEVEL', 'INFO'))\n" + block))
        assert not _entry_points_without_the_sink(ast.parse("def main():\n    pass\n"))

    def test_the_installed_sink_prints_the_stack_and_no_value(self):
        """The behaviour, with the options `install_log_sink` really passes
        (read off its body, so a changed option changes this test's subject):
        the traceback is there, the value of a variable on its lines is not.
        The same probe with loguru's default shows the value -- proof that
        the probe can see what it claims is absent."""
        from loguru import logger as _l

        import utils.log_safety as log_safety

        tree = ast.parse(pathlib.Path(log_safety.__file__).read_text(encoding="utf-8"))
        (install,) = [n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "install_log_sink"]
        (add,) = [c for c in ast.walk(install) if isinstance(c, ast.Call)
                  and getattr(c.func, "attr", "") == "add"]
        options = {k.arg: ast.literal_eval(k.value) for k in add.keywords
                   if k.arg in ("backtrace", "diagnose")}
        assert options["diagnose"] is False

        def work(argument):
            raise RuntimeError("primary")

        def probe(**sink_options) -> str:
            lines: list[str] = []
            sink_id = _l.add(lambda m: lines.append(str(m)), level="ERROR",
                             colorize=False, **sink_options)
            try:
                held = "zz-private-" + "value-zz"
                try:
                    work(held)
                except RuntimeError:
                    _l.opt(exception=True).error("failed")
            finally:
                _l.remove(sink_id)
            return "".join(lines)

        installed = probe(**options)
        assert "in work" in installed and "RuntimeError: primary" in installed
        assert "zz-private-value-zz" not in installed

        assert "zz-private-value-zz" in probe(diagnose=True)
