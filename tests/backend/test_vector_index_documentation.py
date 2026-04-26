"""
Doc-rot guards for the vector-index documentation (W2 cleanup).

WICHTIG audit W2 was originally flagged as "PARTIAL: 5+ files use HNSW
but 2 old migrations still have USING ivfflat (b2c3d4e5f6g7,
h8i9j0k1l2m3)". Investigation showed the IVFFlat indexes those
migrations create are dropped + replaced by `j0k1l2m3n4o5_add_fk_indexes_and_hnsw`,
so production runs purely on HNSW. The audit's "PARTIAL" was a grep
false positive.

What WAS still wrong was the documentation: a stale comment in
`models/database.py:DocumentChunk` claimed the index was IVFFlat. These
tests guard against that doc rot reappearing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PY = REPO_ROOT / "src" / "backend" / "models" / "database.py"
MIGRATIONS_DIR = REPO_ROOT / "src" / "backend" / "alembic" / "versions"


@pytest.mark.unit
def test_document_chunk_comment_describes_hnsw_not_ivfflat():
    """`models/database.py` line 274-ish carries a comment describing how
    the vector-search index is created at migration time. That comment
    must reflect the current HNSW reality (with halfvec cast for high-dim
    embeddings), not the original IVFFlat scheme that's no longer in use.

    The comment IS allowed to mention IVFFlat in a "originally created
    as IVFFlat by ..." context — that's accurate history. What's banned
    is a CREATE-INDEX-style snippet that suggests IVFFlat is the current
    definition.
    """
    src = DATABASE_PY.read_text()

    # Pull out lines around the DocumentChunk vector-index comment.
    # Grep-style: any line in the file mentioning ivfflat in a CREATE
    # INDEX context (rather than a historical reference).
    bad_lines = [
        (lineno, line.strip())
        for lineno, line in enumerate(src.splitlines(), start=1)
        if "USING ivfflat" in line
    ]
    assert not bad_lines, (
        "models/database.py contains a `USING ivfflat` snippet, suggesting "
        "the current index definition is IVFFlat. Production runs HNSW since "
        "j0k1l2m3n4o5_add_fk_indexes_and_hnsw — update the comment to reflect "
        f"that. Offending lines: {bad_lines}"
    )

    # Positive guard: the comment block must mention HNSW so future
    # readers see the real index type.
    assert "USING hnsw" in src, (
        "models/database.py should describe the current HNSW index in a "
        "comment near DocumentChunk so the model file's reader doesn't "
        "have to dig through the migration chain"
    )


@pytest.mark.unit
def test_legacy_ivfflat_migrations_carry_succession_note():
    """The 2 legacy migrations that create IVFFlat indexes must carry a
    docstring note pointing forward to j0k1l2m3n4o5 — the migration that
    drops + replaces them with HNSW. Without this, future audits keep
    re-flagging the same false positive ("USING ivfflat in source files").
    """
    cases = [
        "b2c3d4e5f6g7_add_rag_tables.py",
        "h8i9j0k1l2m3_add_intent_corrections.py",
    ]
    for filename in cases:
        path = MIGRATIONS_DIR / filename
        # Look at just the module docstring (everything before the first
        # `from typing` import) so test only enforces docstring content.
        text = path.read_text()
        if "\nfrom typing" in text:
            docstring_section = text[: text.index("\nfrom typing")]
        else:
            docstring_section = text[:1000]

        assert "j0k1l2m3n4o5" in docstring_section, (
            f"{filename} creates a legacy IVFFlat index but its docstring "
            "doesn't point forward to j0k1l2m3n4o5_add_fk_indexes_and_hnsw "
            "(which drops + replaces the IVFFlat with HNSW). Add a NOTE to "
            "prevent future audits from re-flagging this as a 'still uses "
            "IVFFlat' false positive."
        )
