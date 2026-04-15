"""
Phase 1 W4.2 — Platform → ha_glue import boundary enforcement.

After the Phase 1 extraction, platform code must not directly import from
`ha_glue.*`. The platform talks to ha_glue exclusively through the hook
system (`utils.hooks.run_hooks`). This lets the future X-idra/renfield
platform repo be shipped without ha_glue on disk and without import errors.

There are exactly three structural exceptions, all justified by deploy-time
flavor detection that cannot use the hook system because the hook handlers
themselves live in ha_glue:

1. `api/lifecycle.py` — Stage 0 bootstrap. Imports `ha_glue.bootstrap.register`
   inside a try/except. If the import succeeds, ha_glue registers all its
   hook handlers before the startup hook fires. If it fails (platform-only
   deploy), the system falls through and runs without ha_glue.

2. `alembic/env.py` — Alembic autogenerate target_metadata. Imports
   `ha_glue.models.database` inside a try/except so the 9 HA-specific
   SQLAlchemy classes register with the shared Base.metadata at env.py
   import time. On platform-only deploys the import fails silently and
   target_metadata stays lean.

3. `models/database.py` — PEP 562 `__getattr__` compat shim. Old code
   that still does `from models.database import Room` gets transparently
   redirected to `ha_glue.models.database`. This is a transition aid;
   the shim will be removed once all callers are migrated.

No new exceptions without a Phase 1 architecture decision.
"""
import ast
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "backend"

# Paths relative to src/backend/ that are allowed to import from ha_glue.
#
# Two categories of exception, both with structural justification:
#
#   1. Structural bootstrap: files that must load ha_glue at deploy time
#      to wire hooks or target_metadata. These must use try/except ImportError
#      at module level.
#
#   2. Lazy compat shims: files that redirect old import paths to ha_glue
#      on demand, wrapped in function-body or __getattr__ so platform-only
#      deploys never hit the import.
#
# Every other platform → ha_glue edge is now routed through the hook
# system (`run_hooks` / `register_hook`). If you feel the urge to add a
# new entry here, ask whether a hook event would do the job instead —
# that is the official boundary crossing, this allowlist is for the two
# special cases that cannot use hooks (the hook bus itself has to load
# somewhere, and old imports need a transition bridge).
ALLOWED_IMPORTERS = frozenset({
    # (1) Structural bootstrap
    "api/lifecycle.py",
    "alembic/env.py",
    # (2) Lazy compat shim
    "models/database.py",
})


def _iter_platform_py_files():
    """Yield every .py file under src/backend/ that is NOT inside ha_glue/."""
    for path in BACKEND_ROOT.rglob("*.py"):
        rel = path.relative_to(BACKEND_ROOT)
        parts = rel.parts
        if parts and parts[0] == "ha_glue":
            continue
        # Skip tests — they're allowed to import anything.
        if parts and parts[0] == "tests":
            continue
        yield path, rel


def _ha_glue_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Return [(lineno, import_spec), ...] for any ha_glue import in tree."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "ha_glue" or mod.startswith("ha_glue."):
                names = ", ".join(alias.name for alias in node.names)
                hits.append((node.lineno, f"from {mod} import {names}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "ha_glue" or alias.name.startswith("ha_glue."):
                    hits.append((node.lineno, f"import {alias.name}"))
    return hits


def test_platform_does_not_import_ha_glue():
    """Every platform .py file outside the allowlist must be ha_glue-free."""
    violations: list[str] = []
    for path, rel in _iter_platform_py_files():
        rel_posix = rel.as_posix()
        if rel_posix in ALLOWED_IMPORTERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            pytest.fail(f"cannot parse {rel_posix}: {e}")
        for lineno, spec in _ha_glue_imports(tree):
            violations.append(f"  {rel_posix}:{lineno}: {spec}")

    if violations:
        header = (
            "Platform files are importing from ha_glue. Use the hook system "
            "(run_hooks / register_hook) instead, or add the file to "
            "ALLOWED_IMPORTERS in this test with a justification.\n\n"
            "Violations:\n"
        )
        pytest.fail(header + "\n".join(violations))


def test_allowed_importers_actually_import_ha_glue():
    """
    Guard against the allowlist going stale. If a file is on the allowlist
    but no longer imports ha_glue, remove it from the allowlist.
    """
    stale: list[str] = []
    for rel_posix in sorted(ALLOWED_IMPORTERS):
        path = BACKEND_ROOT / rel_posix
        if not path.exists():
            stale.append(f"{rel_posix} (file does not exist)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _ha_glue_imports(tree):
            stale.append(f"{rel_posix} (no ha_glue imports found)")

    if stale:
        pytest.fail(
            "Stale entries in ALLOWED_IMPORTERS — remove them:\n  " + "\n  ".join(stale)
        )


def test_allowed_importers_use_try_except_or_lazy_pattern():
    """
    Every structural exception must either:
    - Wrap the `from ha_glue...` import in a try/except ImportError, OR
    - Do the import lazily inside a function body (e.g. PEP 562 __getattr__).

    This enforces the "platform-only deploy must not crash" contract. A plain
    top-level `from ha_glue...` would crash X-idra/renfield at import time.
    """
    violations: list[str] = []
    for rel_posix in sorted(ALLOWED_IMPORTERS):
        path = BACKEND_ROOT / rel_posix
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        # Walk all ha_glue imports and check their ancestor chain.
        for node in ast.walk(tree):
            is_ha_import = (
                isinstance(node, ast.ImportFrom)
                and (node.module == "ha_glue" or (node.module or "").startswith("ha_glue."))
            ) or (
                isinstance(node, ast.Import)
                and any(a.name == "ha_glue" or a.name.startswith("ha_glue.") for a in node.names)
            )
            if not is_ha_import:
                continue

            # Find the ancestor chain by re-walking with parent tracking.
            if not _is_guarded_import(tree, node):
                violations.append(
                    f"  {rel_posix}:{node.lineno}: ha_glue import is not wrapped "
                    f"in try/except ImportError and is not inside a function body"
                )

    if violations:
        pytest.fail(
            "Structural ha_glue imports must be guarded. Either wrap in "
            "try/except ImportError at module level, or place inside a "
            "function body so the import happens lazily.\n\n"
            "Violations:\n" + "\n".join(violations)
        )


def _is_guarded_import(tree: ast.AST, target: ast.AST) -> bool:
    """Return True if `target` is inside a try/except ImportError or a function body."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    cur: ast.AST | None = parents.get(id(target))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        if isinstance(cur, ast.Try):
            for handler in cur.handlers:
                if handler.type is None:
                    return True
                if isinstance(handler.type, ast.Name) and handler.type.id in (
                    "ImportError",
                    "ModuleNotFoundError",
                    "Exception",
                    "BaseException",
                ):
                    return True
        cur = parents.get(id(cur))
    return False
