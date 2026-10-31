"""Every entry point into the agent has to forward `corrected_intent`.

🛑 The defect that was fixed WAS a parameter left out one layer up: "correct &
re-answer" sent the tool name, the `chat_handler` derived only the role from it
and threw the rest away -- and in every misfire that was reported the role was
already the right one. So the correction had no effect, and it had none
silently.

Nothing protected against a REPEAT at first. Evidenced by a mutation test in
the review of #1414: removing `corrected_intent=` at the call site in the
`chat_handler`, or at either of the two forwardings in `agent_service`, left
ALL tests green. Two further call sites (`orchestrator.py`, `routes/chat.py`)
had never had the parameter and nobody had noticed.

This is the class, not the instance: one structural test over the call sites
instead of one test per site. Related precedents in this project:
`test_ha_glue_boundary.py`, `test_loguru_placeholders.py`.
"""
import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

WURZEL = Path(__file__).resolve().parents[2] / "src" / "backend"

#: The files that start the agent. If the list grows, the new call site has to
#: carry the parameter -- or be excepted here with a reason.
#: 🛑 Do not enter an exception without writing the reason next to it.
_AUSNAHMEN: dict[str, str] = {}


def _aufrufstellen() -> list[tuple[str, int, set[str]]]:
    """`(relative path, line, set of keyword names)` per entry point.

    What is searched for is `agent.run(...)` AND
    `orchestrator.run_orchestrated(...)`, each as an attribute call -- not
    `run(` as a free function and not `asyncio.run`/`loop.run`. The
    orchestrator belongs in it because it passes its arguments on to
    `agent.run` via `**agent_kwargs`: whoever fills it decides whether the
    correction arrives there.
    """
    treffer = []
    for datei in sorted(WURZEL.rglob("*.py")):
        if "/tests/" in str(datei):
            continue
        try:
            baum = ast.parse(datei.read_text(), filename=str(datei))
        except SyntaxError:  # pragma: no cover
            continue
        for knoten in ast.walk(baum):
            if not isinstance(knoten, ast.Call):
                continue
            f = knoten.func
            if not isinstance(f, ast.Attribute):
                continue
            # 🛑 `run_orchestrated` belongs in it TOO. The first version of
            # this test took only `.run`, which left unguarded exactly the site
            # that FILLS `agent_kwargs` (`chat_handler` -> `run_orchestrated`
            # -> `agent.run(**agent_kwargs)`) -- precisely the hole the review
            # of #1414 had found. My own mutation attempt happened to delete
            # this line, the test stayed green, and only counting the
            # occurrences showed why.
            if f.attr not in ("run", "run_orchestrated"):
                continue
            # The receiver has to be named "agent" or "orchestrator",
            # otherwise it is somebody else's `run` (asyncio.run, loop.run,
            # ...).
            empfaenger = f.value
            name = (empfaenger.id if isinstance(empfaenger, ast.Name)
                    else empfaenger.attr if isinstance(empfaenger, ast.Attribute)
                    else "")
            if not any(w in name.lower() for w in ("agent", "orchestrator")):
                continue
            schluessel = {k.arg for k in knoten.keywords if k.arg}
            hat_sternchen = any(k.arg is None for k in knoten.keywords)
            if hat_sternchen:
                schluessel.add("**")
            treffer.append(
                (str(datei.relative_to(WURZEL)), knoten.lineno, schluessel))
    return treffer


def test_the_call_sites_are_found_at_all():
    """🛑 First prove that the search takes hold.

    A structural test that finds nothing is always green and says nothing.
    Exactly this negative control was missing from the tests that failed to
    notice the parameter being left out.
    """
    stellen = _aufrufstellen()
    assert len(stellen) >= 4, (
        f"only {len(stellen)} entry points found — the search no longer "
        f"takes hold, and then the test below checks nothing: {stellen}")
    dateien = {d for d, _, _ in stellen}
    assert any("chat_handler" in d for d in dateien), (
        f"the site in the chat_handler is missing: {sorted(dateien)}")
    # 🛑 And the orchestrator site, where the correction was missing first.
    assert any("orchestrator" in d for d in dateien), (
        f"the orchestrator site is missing: {sorted(dateien)}")
    # Exactly two sites in the chat_handler: `agent.run` and `run_orchestrated`.
    im_handler = [z for d, z, _ in stellen if "chat_handler" in d]
    assert len(im_handler) == 2, (
        f"expected: agent.run AND run_orchestrated in the chat_handler, "
        f"found: {im_handler}")


def test_every_agent_entry_point_forwards_the_correction():
    """Either an explicit `corrected_intent=`, or `**kwargs`."""
    fehlend = [
        f"{datei}:{zeile}"
        for datei, zeile, schluessel in _aufrufstellen()
        if "corrected_intent" not in schluessel
        and "**" not in schluessel
        and datei not in _AUSNAHMEN
    ]
    assert not fehlend, (
        "an agent call site does not forward `corrected_intent` "
        "— a user correction silently has no effect there:\n  "
        + "\n  ".join(fehlend))
