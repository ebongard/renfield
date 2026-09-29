"""#875 Stufe 1 — enforcement, not discipline (docs/design/kg-bitemporal-edges.md §6).

Every function in the KG code that filters a RELATION on ``is_active`` must also
apply the validity filter (``live_clause`` / ``live_conditions`` from
``services.kg_validity_sql``) — or be listed below with the reason it must not.
A new file or a new function that reads live relations without the filter fails
this test. Crude by design: it catches the forgotten seventh file in six months.

The pattern is the RELATION-specific one the design counts with, so
``KGEntity.is_active`` sites do not trip it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import services.database as _db

_BACKEND = Path(_db.__file__).resolve().parents[1]

# Raw SQL in this codebase puts `FROM kg_relations r` and `WHERE r.is_active`
# on DIFFERENT lines, so the kg_relations form must span newlines — a
# same-line-only pattern let exactly that idiom through (review finding).
_RELATION_ACTIVE = re.compile(
    r"KGRelation\.is_active|\br\.is_active|\brel\.is_active|\brelation\.is_active"
    r"|kg_relations\b[\s\S]{0,400}?\bis_active|\bk\.is_active"
)
_CONDITIONS_CALL = re.compile(r"\*\s*live_conditions\(")
_CLAUSE_ASSIGN = re.compile(r"(\w+)\s*,\s*\w+\s*=\s*live_clause\(")

# (file relative to the backend root, function name) -> why no validity filter.
EXEMPT: dict[tuple[str, str], str] = {
    ("services/knowledge_graph_service.py", "merge_entities"):
        "Merge dedup stays on is_active — design §9: no change to merge_entities.",
    ("services/knowledge_graph_service.py", "update_relation"):
        "By-id admin edit must reach an expired edge too.",
    ("services/knowledge_graph_service.py", "delete_relation"):
        "By-id admin delete must reach an expired edge too.",
    ("services/note_links.py", "sync_note_links"):
        "Structural wikilink diff; note_link edges never expire (§6.1).",
    ("services/note_links.py", "deactivate_note_links"):
        "Write path — deactivates the note's outgoing links.",
    ("services/kg_cleanup_service.py", "cleanup_invalid_entities"):
        "Cascade write + its dry-run count: must also retire EXPIRED edges.",
}

# Files that are allowed to mention relation is_active at all. A new file here is
# a new read path to classify — add it deliberately, not by accident.
_SCANNED = [
    "services/knowledge_graph_service.py",
    "services/note_links.py",
    "services/kg_retrieval.py",
    "services/graph_expansion.py",
    "services/kg_graph_service.py",
    "services/kg_cleanup_service.py",
    "api/routes/knowledge_graph.py",
]


def _functions_with_relation_filter(path: Path) -> dict[str, str]:
    """{function name: its source} for every function whose body filters a
    relation on is_active."""
    src = path.read_text()
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = ast.get_source_segment(src, node) or ""
            if _RELATION_ACTIVE.search(body):
                # Keep the innermost match: nested defs are walked too, and the
                # outer function's text contains the inner one.
                out[node.name] = body
    return out


def _applies_filter(body: str) -> bool:
    """The filter must reach the SQL, not merely be computed.

    ORM: ``*live_conditions(...)`` spread into a ``.where``. Raw SQL: the clause
    returned by ``live_clause()`` must be interpolated as ``{name}`` — calling it
    and forgetting the placeholder (mutant M1) left the query unfiltered while a
    call-presence check stayed green.
    """
    if _CONDITIONS_CALL.search(body):
        return True
    return any("{" + name + "}" in body for name in _CLAUSE_ASSIGN.findall(body))


@pytest.mark.unit
def test_every_live_relation_read_applies_the_validity_filter():
    missing = []
    for rel in _SCANNED:
        for name, body in _functions_with_relation_filter(_BACKEND / rel).items():
            if (rel, name) in EXEMPT:
                continue
            if not _applies_filter(body):
                missing.append(f"{rel}::{name}")
    assert not missing, (
        "These functions filter kg_relations on is_active but not on validity. "
        "Append live_clause()/live_conditions() (services/kg_validity_sql.py) or "
        f"add them to EXEMPT with a reason: {missing}"
    )


@pytest.mark.unit
def test_exemptions_still_exist():
    """A stale exemption would silently cover a renamed function's successor."""
    for rel, name in EXEMPT:
        assert name in _functions_with_relation_filter(_BACKEND / rel), (
            f"EXEMPT lists {rel}::{name}, which no longer filters relations on is_active"
        )


@pytest.mark.unit
def test_no_unscanned_file_reads_live_relations():
    scanned = {str(_BACKEND / p) for p in _SCANNED}
    stray = []
    for path in list((_BACKEND / "services").glob("*.py")) + list(
        (_BACKEND / "api" / "routes").glob("*.py")
    ):
        if str(path) in scanned:
            continue
        text = path.read_text()
        if re.search(r"KGRelation\.is_active|kg_relations\b[\s\S]{0,400}?\bis_active", text):
            stray.append(str(path.relative_to(_BACKEND)))
    assert not stray, f"New live-relation reader(s) outside the scanned set: {stray}"
