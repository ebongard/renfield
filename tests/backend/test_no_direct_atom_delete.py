"""Lint: only AtomPurgeService is allowed to delete atoms.

Background:
    The legal_hold discrimination for wb_field_provenance lives in
    ``services/atom_purge_service.py``. Any code path that deletes
    atoms directly bypasses the archive step and silently destroys
    BaFin-mandated audit trails.

This lint scans the backend source for direct atom-deletion patterns
and fails if any are found outside the allowlisted module.

Patterns blocked:
    - ``DELETE FROM atoms``  (raw SQL)
    - ``delete(Atom)``       (SQLAlchemy Core)
    - ``Atom.__table__.delete()``
    - ``session.delete(some_atom_instance)`` is harder to detect
      statically — flagged via a runtime guard in ``Atom`` if needed
      later; not in scope for sprint 2.

Allowlist:
    - services/atom_purge_service.py — the sanctioned path
    - tests/**                       — tests legitimately exercise both paths
    - alembic/versions/**            — migrations may need to touch atoms
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "src" / "backend"

ALLOWLIST = {
    "services/atom_purge_service.py",
}

# Patterns that indicate a direct atom deletion.
PATTERNS = [
    re.compile(r"DELETE\s+FROM\s+atoms\b", re.IGNORECASE),
    re.compile(r"\bdelete\(\s*Atom\s*\)"),
    re.compile(r"Atom\.__table__\.delete\(\s*\)"),
]


def _docstring_and_comment_lines(source: str) -> set[int]:
    """Line numbers of DOCSTRINGS — text that documents the rule, not code.

    The lint hunts `DELETE FROM atoms` in raw SQL, so plain strings must stay
    in scope — but a docstring explaining the rule (models/database.py does,
    at length) is not a violation, and a lint that flags its own documentation
    trains people to ignore it. Comments likewise.
    """
    import ast

    skip: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return skip
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            skip.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return skip


def _comment_starts(source: str) -> dict[int, int]:
    """line → column where a trailing comment begins.

    Skipping the whole LINE would let a real deletion escape by carrying a
    comment: `await db.execute(text("DELETE FROM atoms ..."))  # cleanup`.
    Only the comment itself is cut away.
    """
    import io
    import tokenize

    starts: dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                line, col = tok.start
                starts[line] = min(col, starts.get(line, col))
    except (tokenize.TokenError, IndentationError):
        pass
    return starts


@pytest.mark.unit
def test_no_direct_atom_delete_outside_purge_service():
    """Fail if any backend file (outside the allowlist) deletes atoms directly."""
    offenders: list[tuple[str, int, str]] = []

    for py_file in BACKEND_ROOT.rglob("*.py"):
        rel = py_file.relative_to(BACKEND_ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        if rel.startswith("alembic/versions/"):
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        skip = _docstring_and_comment_lines(text)
        comment_at = _comment_starts(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if lineno in skip:
                continue
            code = line[: comment_at[lineno]] if lineno in comment_at else line
            for pat in PATTERNS:
                if pat.search(code):
                    offenders.append((rel, lineno, line.strip()))

    if offenders:
        msg = (
            "Direct atom deletion detected. Route through "
            "AtomPurgeService.purge() to preserve legal_hold snapshots.\n"
        )
        for path, lineno, line in offenders:
            msg += f"  {path}:{lineno}  {line}\n"
        pytest.fail(msg)
