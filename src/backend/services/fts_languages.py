"""Single source of truth for the Postgres FTS languages we support.

Renfield is multilingual by design — households speak DE / EN / FR / IT / ES / NL.
Postgres FTS stemming and stop-words are language-specific: ``to_tsvector('german',
'esse')`` and ``to_tsvector('english', 'esse')`` produce different tokens. To match
queries in any of the supported languages against content written in any other,
we union the tsvectors across all configs on both the indexing side (GENERATED
column expression in the migration) and the query side (``websearch_to_tsquery``
unions in the retriever).

Adding a 7th language: append to the tuple below and write a follow-up migration
that DROPs + re-ADDs the GENERATED column with the new expression (Postgres does
not let you ALTER a generated column's expression). Both helpers below produce
the right SQL fragment automatically once the tuple changes — the migration body
is a 5-line copy-paste.

Removing a language: same rule, follow-up migration. Don't bother for storage
reasons (the GIN index size grows linearly with the language count and the
personal corpus is small).
"""
from __future__ import annotations


# Postgres ships these six text-search configurations in the default
# install (``pg_catalog.*``). All have full stemming + stop-word support.
# Adding any non-default config would require ensuring it's available
# everywhere the schema lands (dev, build box, prod cluster) — out of
# scope until we hit a deployment that needs it.
FTS_LANGUAGES: tuple[str, ...] = (
    "german",
    "english",
    "french",
    "italian",
    "spanish",
    "dutch",
)


def build_generated_tsvector_expression(content_column: str) -> str:
    """Return the SQL expression for a GENERATED tsvector column unioning
    all supported FTS languages over ``content_column``.

    Used by migrations that create or rebuild a ``search_vector``
    GENERATED column. The expression is IMMUTABLE for any fixed set of
    language literals, so Postgres accepts it inside ``GENERATED ALWAYS
    AS (...) STORED``.

    Example output for content_column='content':
        to_tsvector('german', coalesce(content, '')) ||
        to_tsvector('english', coalesce(content, '')) || ...

    The migration should embed the result verbatim — do not parametrize
    the language names; Postgres requires literals in the generated
    expression for the IMMUTABLE check to pass.
    """
    parts = [
        f"to_tsvector('{lang}', coalesce({content_column}, ''))"
        for lang in FTS_LANGUAGES
    ]
    return " || ".join(parts)


def build_tsquery_union_sql(query_param: str) -> str:
    """Return the SQL fragment for a ``websearch_to_tsquery`` union over
    all supported FTS languages, parameterized on ``:{query_param}``.

    Used by the lexical retriever. Pairs with the GENERATED column from
    ``build_generated_tsvector_expression`` — every stem produced by any
    of the 6 stemmers on either side will match across the union.

    ``websearch_to_tsquery`` is chosen over ``to_tsquery`` because it
    never raises on malformed input (sanitizes user-typed punctuation
    instead) and OR-defaults bare tokens, which is what natural-language
    queries need.

    Example output for query_param='or_query':
        websearch_to_tsquery('german', :or_query) ||
        websearch_to_tsquery('english', :or_query) || ...
    """
    parts = [
        f"websearch_to_tsquery('{lang}', :{query_param})"
        for lang in FTS_LANGUAGES
    ]
    return " || ".join(parts)
