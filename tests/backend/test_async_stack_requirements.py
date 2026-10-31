"""The dependencies without which the async path dies AT IMPORT.

🛑 THE OUTAGE THIS FILE IS WRITTEN AGAINST (2026-10-04)
=======================================================
`requirements.txt` carried `sqlalchemy>=2.0.25` -- without the `[asyncio]`
extra, and `greenlet` was declared nowhere. The async path has always needed
it; it only came along transitively, because SQLAlchemy 2.0 pulled it in
implicitly.

On 2026-10-04 the build jumped across the open lower bound from 2.0.54 to
2.1.3. There greenlet is no longer implicit -- and every backend pod died at
import:

    ImportError: The SQLAlchemy asyncio module requires that the Python
    'greenlet' library is installed.

The household backend and all three workers went into `CrashLoopBackOff`. The
service was down until it was rolled back to the last good image. No test
noticed beforehand, because the harness runs in the OLD image -- that one had
greenlet.

This file checks the DECLARATION, not the environment. An environment check
would be worthless here: it would be green precisely where it proves nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

_REQUIREMENTS = Path(__file__).resolve().parents[2] / "src" / "backend" / "requirements.txt"


def _lines() -> list[str]:
    """The requirement lines without comments and blank lines."""
    return [
        z.split("#", 1)[0].strip()
        for z in _REQUIREMENTS.read_text().splitlines()
        if z.split("#", 1)[0].strip()
    ]


def _entry(package: str) -> str:
    """The line that declares `package` -- extras included."""
    pattern = re.compile(rf"^{re.escape(package)}(\[[^\]]*\])?\s*[<>=!~]", re.I)
    matches = [z for z in _lines() if pattern.match(z)]
    assert len(matches) == 1, f"{package}: expected exactly one line, found {matches}"
    return matches[0]


def test_sqlalchemy_declares_the_asyncio_extra():
    """🛑 The code imports `sqlalchemy.ext.asyncio`; that path needs greenlet.
    Without the extra it hangs on the whim of the dependency resolver -- and on
    2026-10-04 that whim fell out differently than the day before.

    The extra is what the error message itself recommends.
    """
    line = _entry("sqlalchemy")
    assert "[asyncio]" in line.lower(), (
        f"sqlalchemy declared without the [asyncio] extra: {line!r}. "
        "The async path needs greenlet; without the extra it only comes along by chance."
    )


def test_sqlalchemy_is_bounded_above():
    """🛑 The other half: a deploy must not be an uninvited dependency
    upgrade.

    The open lower bound let the build cross a minor-version boundary at which
    the dependency semantics changed. The jump to 2.1 is a step of its own,
    checked -- not a side effect of somebody having built on a different day.
    """
    line = _entry("sqlalchemy")
    assert re.search(r"<\s*\d", line), (
        f"sqlalchemy without an upper bound: {line!r}. Any build can then jump across a "
        "version boundary without anybody having decided it."
    )


def test_the_async_imports_the_code_actually_uses_are_declared():
    """A counter-check that this test does not hang off the wrong package:
    the code really does import `sqlalchemy.ext.asyncio`. If that import ever
    goes away, the two tests above are moot and should show up rather than
    running on silently."""
    quelle = (
        Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "database.py"
    ).read_text()
    assert "from sqlalchemy.ext.asyncio import" in quelle, (
        "services/database.py no longer imports `sqlalchemy.ext.asyncio` — "
        "the reason for the [asyncio] extra needs to be re-checked."
    )
    assert "async_sessionmaker" in quelle
