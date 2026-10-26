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

🛑 **Die erste Fassung dieses Prüfers war selbst blind.** Sie suchte
`%[sdifr]` — also den Buchstaben DIREKT nach dem Prozentzeichen. Damit fielen
`%.3f`, `%5d`, `%-10s` und `%(name)s` durch, und genau so überlebte
`meeting_fingerprint_service.py:177` den Sweep: `"… fingerprint {} (%.3f)"`
mit vier Argumenten bei drei Klammern. Der Ähnlichkeitswert fiel weiter weg.
Die Negativkontrolle prüfte nur `%s`/`%r` und bemerkte die Blindheit deshalb
nicht. Jetzt deckt sie die ganze printf-Grammatik ab — die Kontrolle ist der
Teil, der zählt.
"""
import ast
import re
import string
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

_LOG_METHODEN = {"debug", "info", "warning", "error", "exception",
                 "critical", "trace", "success"}

#: Die printf-Grammatik: `%(name)s`, `%-10s`, `%5d`, `%.3f`, `%05.2f`.
#:
#: 🛑 `%%` wird als EIGENES Zeichen mitgelesen und dann verworfen, nicht per
#: Vorschau ausgeschlossen. Sonst passiert genau das, was die Negativkontrolle
#: hier gefunden hat: in `"echtes Prozent 50%% fertig"` greift die Regex am
#: ZWEITEN Prozentzeichen und liest `% f` als Platzhalter — Leerzeichen-Flag
#: plus Umwandlung `f` aus „fertig". Ein Falschtreffer auf harmlosem Text.
#:
#: 🛑 Das Leerzeichen-Flag (`"% d"`) ist absichtlich NICHT in der Flagmenge.
#: Es kommt in diesem Baum nirgends vor und ist auch sonst verschwindend
#: selten, waehrend Prosa wie „50% fertig" oder „80% der Seiten" plausibel
#: ist. Einen unplausiblen Falsch-Negativ gegen einen plausiblen
#: Falsch-Positiv zu tauschen waere die falsche Richtung.
_TOKEN = re.compile(
    r"%%|%(?:\([^)]*\))?[-+#0]*\d*(?:\.\d+)?[hlL]?[sdifrgeExXoucab]")


def _hat_platzhalter(text: str) -> bool:
    """Traegt der Text einen echten printf-Platzhalter (nicht bloss `%%`)?"""
    return any(m.group(0) != "%%" for m in _TOKEN.finditer(text))

#: loguru wird auch unter einem anderen Namen importiert — den Filter nicht an
#: der genauen Zeile festmachen (`alembic/versions/…` nutzt
#: `from loguru import logger as _migration_logger`).
_LOGURU_IMPORT = re.compile(r"^\s*from\s+loguru\s+import\s+logger(?:\s+as\s+(\w+))?",
                            re.MULTILINE)


def _loguru_namen(quelle: str) -> set[str]:
    """Unter welchen Namen ist loguru in dieser Datei erreichbar?"""
    return {m.group(1) or "logger" for m in _LOGURU_IMPORT.finditer(quelle)}


def _logaufrufe(quelle: str, namen: set[str]):
    """Jeder `<name>.<methode>(…)` — auch durch `.opt(...)`/`.bind(...)` hindurch."""
    try:
        baum = ast.parse(quelle)
    except SyntaxError:
        return
    for knoten in ast.walk(baum):
        if not (isinstance(knoten, ast.Call)
                and isinstance(knoten.func, ast.Attribute)
                and knoten.func.attr in _LOG_METHODEN):
            continue
        # Die Wurzel der Attributkette: `logger`, `logger.opt(…)`, `logger.bind(…)`
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
                continue            # ohne Argumente gibt es nichts zu verlieren
            erstes = knoten.args[0]
            if (isinstance(erstes, ast.Constant) and isinstance(erstes.value, str)
                    and _hat_platzhalter(erstes.value)):
                treffer.append(f"{datei}:{knoten.lineno} — {erstes.value[:60]!r}")
    return treffer, geprueft


def _wurzel() -> Path:
    w = Path(__file__).resolve().parents[2] / "src" / "backend"
    # 🛑 Ohne diese Zusicherung waere der Test bei falscher Wurzel STILL GRUEN:
    # `rglob` auf ein fehlendes Verzeichnis liefert leer, ohne Fehler.
    assert w.is_dir(), f"Wurzel nicht gefunden: {w}"
    return w


def test_no_log_line_loses_its_arguments():
    """🛑 loguru ersetzt `{}`, nicht `%s`. Wer Argumente uebergibt, muss
    geschweifte Klammern benutzen — sonst verschwinden sie spurlos."""
    verlierend, geprueft = _verlierende_aufrufe(_wurzel())
    # Dass ueberhaupt etwas geprueft wurde, ist Teil der Zusicherung.
    assert geprueft > 50, f"nur {geprueft} Dateien mit loguru gesehen — prueft der Test ueberhaupt?"
    assert not verlierend, (
        "diese Logaufrufe verlieren ihre Argumente still (loguru ersetzt "
        "`{}`, nicht `%s`):\n  " + "\n  ".join(verlierend))


def test_every_loguru_literal_is_formattable():
    """🛑 Ein NEUER Fehlermodus, den der Umstieg auf Klammern eroeffnet.

    Gemessen: `logger.info("stray brace { here {}", 1)` wirft
    `ValueError: unexpected '{' in field name` — am AUFRUFORT, loguru faengt es
    nicht. Mit `%s` war das unmoeglich. Eine Logzeile, die den Aufrufer
    mitnimmt, ist schlimmer als eine, die nichts sagt.
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
        "diese Logzeilen werfen beim Formatieren und nehmen den Aufrufer mit:"
        "\n  " + "\n  ".join(kaputt))


def test_the_checker_sees_the_whole_printf_grammar():
    """Negativkontrolle, und sie ist der Teil, der zaehlt.

    🛑 Die erste Fassung suchte nur `%[sdifr]` und war damit fuer `%.3f` blind
    — genau so ueberlebte `meeting_fingerprint_service.py:177` den Sweep. Die
    damalige Kontrolle prueffte nur `%s`/`%r` und bemerkte die Luecke nicht.
    Diese hier fuehrt jede Form auf, die vorkommen kann, UND den Aliasfall.
    """
    import tempfile

    faelle = ["%s", "%r", "%d", "%.3f", "%5d", "%-10s", "%(name)s", "%05.2f", "%x"]
    with tempfile.TemporaryDirectory() as d:
        wurzel = Path(d)
        for i, form in enumerate(faelle):
            (wurzel / f"f{i}.py").write_text(
                "from loguru import logger\n"
                f'logger.info("verliert {form}", 1)\n', encoding="utf-8")
        # Alias — die erste Fassung filterte auf die genaue Importzeile.
        (wurzel / "alias.py").write_text(
            "from loguru import logger as _mig\n"
            '_mig.warning("auch hier %s", 1)\n', encoding="utf-8")
        # Durch `.opt(...)` hindurch.
        (wurzel / "opt.py").write_text(
            "from loguru import logger\n"
            'logger.opt(depth=1).info("und hier %d", 1)\n', encoding="utf-8")
        # Und vier, die NICHT zaehlen duerfen. Die letzten beiden hat genau
        # diese Kontrolle beim ersten Lauf aufgedeckt: `%%` wurde als
        # Platzhalter gelesen, weil die Regex am zweiten Prozentzeichen griff
        # und `% f` als Leerzeichen-Flag plus Umwandlung verstand.
        (wurzel / "harmlos.py").write_text(
            "from loguru import logger\n"
            'logger.info("klammern {} ", 1)\n'
            'logger.info("ohne Argumente %s")\n'
            'logger.info("echtes Prozent 50%% fertig", 1)\n'
            'logger.info("Prosa: 80% der Seiten fertig", 1)\n', encoding="utf-8")

        treffer, geprueft = _verlierende_aufrufe(wurzel)

    assert geprueft == len(faelle) + 3, geprueft
    assert len(treffer) == len(faelle) + 2, (
        f"erwartet {len(faelle) + 2} Treffer (jede printf-Form, Alias, opt), "
        f"gefunden {len(treffer)}:\n  " + "\n  ".join(treffer))
