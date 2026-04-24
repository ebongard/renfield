"""Common assertions for per-area smoke tests.

Keep this module small — helpers should only wrap the three or four
patterns that repeat across every area. Area-specific assertions stay
in their own test file.
"""
from __future__ import annotations

import re

# Substring match, case-insensitive, used to filter "known harmless"
# console noise out of the critical-errors assertion. Anything matched
# here does NOT fail a smoke test on its own — failures must come from
# a message that is NOT in this allowlist.
HARMLESS_CONSOLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ssl certificate error", re.IGNORECASE),
    # Self-signed renfield.local; service worker registration is
    # blocked in headless Chromium but does not block app function.
    re.compile(r"failed to register a serviceworker", re.IGNORECASE),
    # PWA manifest may 404 on some builds; not a functional failure.
    re.compile(r"manifest\.webmanifest", re.IGNORECASE),
    # Favicon missing — cosmetic.
    re.compile(r"favicon", re.IGNORECASE),
)


def assert_no_critical_console_errors(errors: list[str]) -> None:
    """Fail the test with a readable diff if any console error is NOT
    in the harmless-allowlist above."""
    critical = [
        e for e in errors
        if not any(p.search(e) for p in HARMLESS_CONSOLE_PATTERNS)
    ]
    assert not critical, (
        "Critical console errors on this page:\n"
        + "\n".join(f"  - {e}" for e in critical)
    )


def assert_body_not_blank(body_text: str, *, min_chars: int = 50) -> None:
    """The page body must contain more than boilerplate — a blank white
    page (React crashed, auth redirected, route 404'd) shows up here."""
    assert len(body_text) >= min_chars, (
        f"Page body is suspiciously short ({len(body_text)} chars): "
        f"{body_text[:200]!r}"
    )
