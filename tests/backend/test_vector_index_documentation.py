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
def test_document_chunk_comment_describes_hnsw_index():
    """`models/database.py` near DocumentChunk carries a comment describing
    how the vector-search index is created at migration time. The comment
    must reference `USING hnsw` AND `halfvec` so a future reader of the
    model file sees both the current algorithm and the dimensionality
    workaround without having to dig through the migration chain.

    Deliberately NO assertion on absence of "ivfflat": the comment is
    expected to mention IVFFlat in historical / supersedence context
    ("originally created as IVFFlat by ..."), and a substring ban would
    over-match that legitimate text. The positive HNSW + halfvec checks
    alone catch the regression we care about (someone replacing the
    current comment with the old IVFFlat-only one).
    """
    src = DATABASE_PY.read_text()

    assert "USING hnsw" in src, (
        "models/database.py should describe the current HNSW index in a "
        "comment near DocumentChunk so the model file's reader doesn't "
        "have to dig through the migration chain"
    )
    assert "halfvec" in src, (
        "The HNSW comment in models/database.py should mention the "
        "`halfvec` cast — production embeddings exceed pgvector's 2000-dim "
        "limit on regular `vector`, and halfvec is how the index works "
        "around it. Without this note, the next eng to look at this file "
        "won't know why a cast appears in the migration"
    )


@pytest.mark.unit
def test_document_chunk_comment_matches_real_migration():
    """The HNSW + halfvec definition in the model-file comment must match
    something that ACTUALLY exists in the migration chain.

    Cross-references the doc against the live migration files: at least
    one alembic migration must combine all three of `USING hnsw`,
    `halfvec`, AND `document_chunks` in its source. Without this guard,
    the model comment could drift away from reality (e.g. the migration
    quietly switches algorithms while the comment keeps claiming HNSW
    for years).

    Today, both `cce1984705df_resize_embedding_vectors_768_to_2560.py`
    and `p1q2r3s4t5u6_fix_kb_performance_indexes.py` satisfy this.
    """
    matching_migrations: list[str] = []
    for path in MIGRATIONS_DIR.glob("*.py"):
        text = path.read_text()
        if "USING hnsw" in text and "halfvec" in text and "document_chunks" in text:
            matching_migrations.append(path.name)

    assert matching_migrations, (
        "models/database.py:DocumentChunk comments claim the live "
        "vector index uses `USING hnsw ((embedding::halfvec(...)) "
        "halfvec_cosine_ops)`, but NO migration in alembic/versions/ "
        "actually creates such an index. The doc has drifted away from "
        "reality — either fix the comment to describe the current "
        "migration's index definition, or add the migration that the "
        "comment claims exists."
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
