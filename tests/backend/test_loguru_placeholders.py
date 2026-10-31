"""Log lines must not lose their arguments silently.

🛑 The occasion, found live in the browser acceptance for the 2026-10-26
deploy: a freshly rolled-out diagnostic line printed, verbatim,

    📬 Historie veraltet (Datenbank %s > bekannt %s) -- ...

The backend's logger is **loguru**, and loguru only substitutes curly braces.
With `%s` the placeholder stays put and the arguments fall away silently.
Counter-measured in the production image:

    NEW: (Datenbank 12 > bekannt 10) -- substituted?
    OLD: (Datenbank %s > bekannt %s) -- substituted?

Of all things the two numbers one would have needed to measure with. A
diagnostic line that loses its diagnosis is worse than none -- it feigns
observability.

The count came to **40 calls in 13 files**, among them
`"Could not parse LLM JSON: %s | raw=%r"`, which lost the error AND the raw
data.

🛑 **The first version of this checker was itself blind.** It searched for
`%[sdifr]` -- the letter DIRECTLY after the percent sign. That let `%.3f`,
`%5d`, `%-10s` and `%(name)s` through, and that is exactly how
`meeting_fingerprint_service.py:177` survived the sweep: `"... fingerprint {}
(%.3f)"` with four arguments against three braces. The similarity score fell
away as well. The negative control checked only `%s`/`%r` and therefore did
not notice the blindness. Now it covers the whole printf grammar -- the control
is the part that counts.
"""
import ast
import re
import string
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

_LOG_METHODEN = {"debug", "info", "warning", "error", "exception",
                 "critical", "trace", "success"}

#: The printf grammar: `%(name)s`, `%-10s`, `%5d`, `%.3f`, `%05.2f`.
#:
#: 🛑 `%%` is read as a token OF ITS OWN and then discarded, not excluded by
#: lookahead. Otherwise exactly what the negative control found here happens:
#: in `"echtes Prozent 50%% fertig"` the regex grabs at the SECOND percent
#: sign and reads `% f` as a placeholder -- the space flag plus the conversion
#: `f` out of "fertig". A false match on harmless text.
#:
#: 🛑 The space flag (`"% d"`) is deliberately NOT in the flag set. It
#: appears nowhere in this tree and is vanishingly rare elsewhere, while prose
#: like "50% fertig" or "80% der Seiten" is plausible. Trading an implausible
#: false negative for a plausible false positive would be the wrong
#: direction.
_TOKEN = re.compile(
    r"%%|%(?:\([^)]*\))?[-+#0]*\d*(?:\.\d+)?[hlL]?[sdifrgeExXoucab]")


def _hat_platzhalter(text: str) -> bool:
    """Does the text carry a real printf placeholder (not just `%%`)?"""
    return any(m.group(0) != "%%" for m in _TOKEN.finditer(text))

#: loguru is also imported under a different name -- do not pin the filter to
#: the exact line (`alembic/versions/...` uses
#: `from loguru import logger as _migration_logger`).
_LOGURU_IMPORT = re.compile(r"^\s*from\s+loguru\s+import\s+logger(?:\s+as\s+(\w+))?",
                            re.MULTILINE)


def _loguru_namen(quelle: str) -> set[str]:
    """Under which names is loguru reachable in this file?"""
    return {m.group(1) or "logger" for m in _LOGURU_IMPORT.finditer(quelle)}


def _logaufrufe(quelle: str, namen: set[str]):
    """Every `<name>.<method>(...)` -- including through `.opt(...)`/`.bind(...)`."""
    try:
        baum = ast.parse(quelle)
    except SyntaxError:
        return
    for knoten in ast.walk(baum):
        if not (isinstance(knoten, ast.Call)
                and isinstance(knoten.func, ast.Attribute)
                and knoten.func.attr in _LOG_METHODEN):
            continue
        # The root of the attribute chain: `logger`, `logger.opt(...)`, `logger.bind(...)`
        wurzel = knoten.func.value
        while isinstance(wurzel, ast.Call):
            wurzel = wurzel.func
        while isinstance(wurzel, ast.Attribute):
            wurzel = wurzel.value
        if isinstance(wurzel, ast.Name) and wurzel.id in namen:
            yield knoten


def _verlierende_aufrufe(wurzel: Path) -> tuple[list[str], int]:
    treffer: list[str] = []
    geprueft = 0
    for datei in sorted(wurzel.rglob("*.py")):
        quelle = datei.read_text(encoding="utf-8")
        namen = _loguru_namen(quelle)
        if not namen:
            continue
        geprueft += 1
        for knoten in _logaufrufe(quelle, namen):
            if len(knoten.args) < 2:
                continue            # with no arguments there is nothing to lose
            erstes = knoten.args[0]
            if (isinstance(erstes, ast.Constant) and isinstance(erstes.value, str)
                    and _hat_platzhalter(erstes.value)):
                treffer.append(f"{datei}:{knoten.lineno} — {erstes.value[:60]!r}")
    return treffer, geprueft


def _wurzel() -> Path:
    w = Path(__file__).resolve().parents[2] / "src" / "backend"
    # 🛑 Without this assertion the test would be SILENTLY GREEN on a wrong
    # root: `rglob` on a missing directory yields nothing, without an error.
    assert w.is_dir(), f"root not found: {w}"
    return w


def test_no_log_line_loses_its_arguments():
    """🛑 loguru substitutes `{}`, not `%s`. Whoever passes arguments has to
    use curly braces -- otherwise they vanish without a trace."""
    verlierend, geprueft = _verlierende_aufrufe(_wurzel())
    # That anything was checked at all is part of the assertion.
    assert geprueft > 50, f"only {geprueft} files with loguru seen — does the test check anything at all?"
    assert not verlierend, (
        "these log calls silently lose their arguments (loguru substitutes "
        "`{}`, not `%s`):\n  " + "\n  ".join(verlierend))


def test_every_loguru_literal_is_formattable():
    """🛑 A NEW failure mode that the move to braces opens up.

    Measured: `logger.info("stray brace { here {}", 1)` raises
    `ValueError: unexpected '{' in field name` -- AT THE CALL SITE, loguru does
    not catch it. With `%s` that was impossible. A log line that takes its
    caller down with it is worse than one that says nothing.
    """
    kaputt = []
    for datei in sorted(_wurzel().rglob("*.py")):
        quelle = datei.read_text(encoding="utf-8")
        namen = _loguru_namen(quelle)
        if not namen:
            continue
        for knoten in _logaufrufe(quelle, namen):
            if not knoten.args:
                continue
            erstes = knoten.args[0]
            if not (isinstance(erstes, ast.Constant) and isinstance(erstes.value, str)):
                continue
            try:
                list(string.Formatter().parse(erstes.value))
            except ValueError as exc:
                kaputt.append(f"{datei}:{knoten.lineno} — {exc}")
    assert not kaputt, (
        "these log lines raise when formatted and take the caller down:"
        "\n  " + "\n  ".join(kaputt))


def test_the_checker_sees_the_whole_printf_grammar():
    """A negative control, and it is the part that counts.

    🛑 The first version searched only for `%[sdifr]` and was therefore blind
    to `%.3f` -- that is exactly how `meeting_fingerprint_service.py:177`
    survived the sweep. The control at the time checked only `%s`/`%r` and did
    not notice the gap. This one lists every form that can occur, AND the alias
    case.
    """
    import tempfile

    faelle = ["%s", "%r", "%d", "%.3f", "%5d", "%-10s", "%(name)s", "%05.2f", "%x"]
    with tempfile.TemporaryDirectory() as d:
        wurzel = Path(d)
        for i, form in enumerate(faelle):
            (wurzel / f"f{i}.py").write_text(
                "from loguru import logger\n"
                f'logger.info("verliert {form}", 1)\n', encoding="utf-8")
        # The alias -- the first version filtered on the exact import line.
        (wurzel / "alias.py").write_text(
            "from loguru import logger as _mig\n"
            '_mig.warning("auch hier %s", 1)\n', encoding="utf-8")
        # Through `.opt(...)`.
        (wurzel / "opt.py").write_text(
            "from loguru import logger\n"
            'logger.opt(depth=1).info("und hier %d", 1)\n', encoding="utf-8")
        # And four that must NOT count. This very control uncovered the
        # last two on its first run: `%%` was read as a placeholder, because
        # the regex grabbed at the second percent sign and understood `% f` as
        # the space flag plus a conversion.
        (wurzel / "harmlos.py").write_text(
            "from loguru import logger\n"
            'logger.info("klammern {} ", 1)\n'
            'logger.info("ohne Argumente %s")\n'
            'logger.info("echtes Prozent 50%% fertig", 1)\n'
            'logger.info("Prosa: 80% der Seiten fertig", 1)\n', encoding="utf-8")

        treffer, geprueft = _verlierende_aufrufe(wurzel)

    assert geprueft == len(faelle) + 3, geprueft
    assert len(treffer) == len(faelle) + 2, (
        f"expected {len(faelle) + 2} hits (every printf form, alias, opt), "
        f"found {len(treffer)}:\n  " + "\n  ".join(treffer))
