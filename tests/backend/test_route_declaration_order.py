"""Repo-wide guard: a static-path route MUST NOT be declared after a
parameterized route at the same prefix on the same HTTP method, unless
the param is type-pinned (`{id:int}`, `{id:uuid}`, etc.).

FastAPI walks routes in declaration order. `GET /things/{id}` declared
before `GET /things/stats` makes the second route unreachable —
`/things/stats` matches the first route with `id="stats"`, which then
either 404s (after a DB lookup miss) or 422s (when the param type is
``int``). The v2.10 admin console PR shipped one such bug
(``GET /api/trajectories/{trajectory_id}`` declared before
``GET /api/trajectories/stats``); #617 fixed it. This test prevents
the class.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROUTE_RE = re.compile(r'^@router\.(get|post|put|patch|delete)\("([^"]+)"')


def _route_dirs() -> list[Path]:
    """Locate the route module dirs in both layouts we run under:
    - Local: cwd is the repo root → ``src/backend/api/routes``.
    - .159 container: cwd is /, code mounted at /app (= src/backend)
      → ``/app/api/routes`` and ``/app/ha_glue/api/routes``.

    Anchor off ``models.database``'s file path so we always find the
    backend root regardless of cwd.
    """
    try:
        import models.database as _db_mod
        backend_root = Path(_db_mod.__file__).resolve().parent.parent
    except Exception:
        backend_root = Path("src/backend").resolve()
    candidates = [
        backend_root / "api" / "routes",
        backend_root / "ha_glue" / "api" / "routes",
    ]
    return [c for c in candidates if c.is_dir()]


# Resolved at import time so pytest parametrize sees the real list.
ROUTE_DIRS = _route_dirs()


def _has_param(path: str) -> bool:
    return "{" in path


def _shadows(static_path: str, param_path: str) -> bool:
    """Does ``param_path`` swallow ``static_path`` during FastAPI's
    declaration-order routing walk?

    Same segment count + every param segment either un-typed or typed
    with a converter that accepts the static segment's literal value.
    """
    s_parts = static_path.strip("/").split("/")
    p_parts = param_path.strip("/").split("/")
    if len(s_parts) != len(p_parts):
        return False
    for s_seg, p_seg in zip(s_parts, p_parts):
        if p_seg.startswith("{"):
            # ``{name}``  → matches anything → shadow risk
            # ``{name:int}`` → only digits
            # ``{name:uuid}`` → only UUIDs (static path almost never UUID)
            converter = None
            if ":" in p_seg:
                converter = p_seg.split(":", 1)[1].rstrip("}").lower()
            if converter == "int" and not s_seg.isdigit():
                return False
            if converter == "uuid":
                return False
            continue
        if s_seg != p_seg:
            return False
    return True


def _collect_routes(route_file: Path) -> list[tuple[int, str, str]]:
    routes: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(route_file.read_text().splitlines(), 1):
        m = ROUTE_RE.match(raw.strip())
        if m:
            routes.append((lineno, m.group(1), m.group(2)))
    return routes


def _find_shadows(route_file: Path) -> list[str]:
    routes = _collect_routes(route_file)
    issues: list[str] = []
    for i, (sn, sm, sp) in enumerate(routes):
        if _has_param(sp):
            continue
        for pn, pm, pp in routes[:i]:
            if _has_param(pp) and pm == sm and _shadows(sp, pp):
                issues.append(
                    f"{route_file}:{sn}  {sm.upper()} {sp}  shadowed by  "
                    f"L{pn} {pm.upper()} {pp}"
                )
    return issues


def _all_route_files() -> list[Path]:
    out: list[Path] = []
    for d in ROUTE_DIRS:
        if d.is_dir():
            out.extend(sorted(p for p in d.glob("*.py") if p.name != "__init__.py"))
    return out


def test_route_files_exist():
    """Sanity check — if the route dirs vanish, the rest of this test
    silently passes with zero files. Catch that immediately."""
    files = _all_route_files()
    assert len(files) > 5, f"Expected several route modules, found {files!r}"


@pytest.mark.parametrize(
    "route_file",
    _all_route_files(),
    ids=lambda p: str(p),
)
def test_no_static_shadowed_by_param(route_file: Path):
    """A static-path route must not be declared after a same-method
    untyped parameterized route at the same prefix.

    If this fails:
      - Move the static path ABOVE the parameterized one in the file
        (FastAPI matches in declaration order, so static-first wins).
      - OR pin the param to a converter: ``{thing_id:int}`` will
        refuse to match ``"stats"`` and stop shadowing.
    """
    issues = _find_shadows(route_file)
    assert not issues, "\n".join(["Route declaration shadow risk(s):", *issues])
