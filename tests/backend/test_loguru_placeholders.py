"""Logzeilen dürfen ihre Argumente nicht still verlieren.

🛑 Der Anlass, in der Browser-Abnahme zum Deploy vom 2026-10-26 live gefunden:
eine frisch ausgerollte Diagnosezeile druckte wörtlich

    📬 Historie veraltet (Datenbank %s > bekannt %s) — …

Der Logger des Backends ist **loguru**, und loguru ersetzt nur geschweifte
Klammern. Bei `%s` bleibt der Platzhalter stehen und die Argumente fallen
still weg. Gegengemessen im Produktionsbild:

    NEU: (Datenbank 12 > bekannt 10) — ersetzt?
    ALT: (Datenbank %s > bekannt %s) — ersetzt?

Ausgerechnet die beiden Zahlen, die man zum Nachmessen gebraucht hätte. Eine
Diagnosezeile, die ihre Diagnose verliert, ist schlimmer als keine — sie
täuscht Beobachtbarkeit vor.

Beim Auszählen waren es **40 Aufrufe in 13 Dateien**, darunter
`"Could not parse LLM JSON: %s | raw=%r"`, das Fehler UND Rohdaten verlor.
Dieser Test hält die Klasse zu, nicht den einen Fall.
"""
import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

_LOG_METHODEN = {"debug", "info", "warning", "error", "exception",
                 "critical", "trace", "success"}
#: `%%` ist ein echtes Prozentzeichen und harmlos.
_PLATZHALTER = re.compile(r"%[sdifr]")


def _verlierende_aufrufe(wurzel: Path) -> list[str]:
    treffer = []
    for datei in sorted(wurzel.rglob("*.py")):
        quelle = datei.read_text()
        if "from loguru import logger" not in quelle:
            continue
        try:
            baum = ast.parse(quelle)
        except SyntaxError:
            continue
        for knoten in ast.walk(baum):
            if not (isinstance(knoten, ast.Call)
                    and isinstance(knoten.func, ast.Attribute)
                    and knoten.func.attr in _LOG_METHODEN
                    and isinstance(knoten.func.value, ast.Name)
                    and knoten.func.value.id == "logger"):
                continue
            # Ohne weitere Argumente gibt es nichts zu verlieren.
            if len(knoten.args) < 2:
                continue
            erstes = knoten.args[0]
            if (isinstance(erstes, ast.Constant) and isinstance(erstes.value, str)
                    and _PLATZHALTER.search(erstes.value)):
                treffer.append(
                    f"{datei}:{knoten.lineno} — {erstes.value[:60]!r}")
    return treffer


def test_no_log_line_loses_its_arguments():
    """🛑 loguru ersetzt `{}`, nicht `%s`. Wer Argumente übergibt, muss
    geschweifte Klammern benutzen — sonst verschwinden sie spurlos."""
    wurzel = Path(__file__).resolve().parents[2] / "src" / "backend"
    verlierend = _verlierende_aufrufe(wurzel)
    assert not verlierend, (
        "diese Logaufrufe verlieren ihre Argumente still (loguru ersetzt "
        "`{}`, nicht `%s`):\n  " + "\n  ".join(verlierend))


def test_the_checker_actually_finds_something():
    """Negativkontrolle IM Test: der Prüfer muss einen künstlichen Fall finden.
    Ohne das wäre „null Treffer" auch dann grün, wenn er gar nichts prüft."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "boese.py"
        p.write_text(
            "from loguru import logger\n"
            'logger.info("verliert %s und %r", 1, 2)\n'
            'logger.info("harmlos {} ", 1)\n'
            'logger.info("ohne Argumente %s")\n'
        )
        treffer = _verlierende_aufrufe(Path(d))
    assert len(treffer) == 1, treffer
    assert "verliert" in treffer[0]
