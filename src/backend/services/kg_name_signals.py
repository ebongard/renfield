"""Name-only signals shared by the KG resolve path and the reconciler.

A leaf module (no service imports) so ``knowledge_graph_service`` and
``kg_reconciler_service`` can both use it — the reconciler already imports the
graph service, so the predicate cannot live in either of them without a cycle.
"""
from __future__ import annotations

from collections import Counter


def names_differ_only_in_digit_tokens(name_a: str | None, name_b: str | None) -> bool:
    """True when two names differ, and every token they do NOT share contains a digit.

    Tokens are whitespace tokens of the stripped, lower-cased name (the
    reconciler's ``_norm(...).split()``), so "1.2.3" is ONE token. A
    digit-bearing token is an identifier — a version, an ordinal, a year, a room
    or account number — and a different identifier names a different thing:
    "Product A - 1.2.4" vs "Product A - 1.2.3" (#1329 field data, two releases),
    "Konto 01" vs "Konto 02", "Bericht 2025" vs "Bericht". The comparison is a
    multiset difference, not positional, so an ADDED or DROPPED number counts as
    well as a changed one; a mere reordering is no difference. A difference in
    any token WITHOUT a digit is left to the other signals — this test only
    claims what the digits alone prove.
    """
    ca = Counter((name_a or "").strip().lower().split())
    cb = Counter((name_b or "").strip().lower().split())
    diff = list((ca - cb).elements()) + list((cb - ca).elements())
    return bool(diff) and all(any(ch.isdigit() for ch in tok) for tok in diff)
