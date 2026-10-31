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

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "src" / "backend"

#: The files that start the agent. If the list grows, the new call site has to
#: carry the parameter -- or be excepted here with a reason.
#: 🛑 Do not enter an exception without writing the reason next to it.
_EXCEPTIONS: dict[str, str] = {}


def _call_sites() -> list[tuple[str, int, set[str]]]:
    """`(relative path, line, set of keyword names)` per entry point.

    What is searched for is `agent.run(...)` AND
    `orchestrator.run_orchestrated(...)`, each as an attribute call -- not
    `run(` as a free function and not `asyncio.run`/`loop.run`. The
    orchestrator belongs in it because it passes its arguments on to
    `agent.run` via `**agent_kwargs`: whoever fills it decides whether the
    correction arrives there.
    """
    hits = []
    for source_file in sorted(BACKEND_ROOT.rglob("*.py")):
        if "/tests/" in str(source_file):
            continue
        try:
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
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
            receiver = f.value
            name = (receiver.id if isinstance(receiver, ast.Name)
                    else receiver.attr if isinstance(receiver, ast.Attribute)
                    else "")
            if not any(w in name.lower() for w in ("agent", "orchestrator")):
                continue
            kw_names = {k.arg for k in node.keywords if k.arg}
            has_double_star = any(k.arg is None for k in node.keywords)
            if has_double_star:
                kw_names.add("**")
            hits.append(
                (str(source_file.relative_to(BACKEND_ROOT)), node.lineno, kw_names))
    return hits


def test_the_call_sites_are_found_at_all():
    """🛑 First prove that the search takes hold.

    A structural test that finds nothing is always green and says nothing.
    Exactly this negative control was missing from the tests that failed to
    notice the parameter being left out.
    """
    sites = _call_sites()
    assert len(sites) >= 4, (
        f"only {len(sites)} entry points found — the search no longer "
        f"takes hold, and then the test below checks nothing: {sites}")
    files = {d for d, _, _ in sites}
    assert any("chat_handler" in d for d in files), (
        f"the site in the chat_handler is missing: {sorted(files)}")
    # 🛑 And the orchestrator site, where the correction was missing first.
    assert any("orchestrator" in d for d in files), (
        f"the orchestrator site is missing: {sorted(files)}")
    # Exactly two sites in the chat_handler: `agent.run` and `run_orchestrated`.
    in_handler = [z for d, z, _ in sites if "chat_handler" in d]
    assert len(in_handler) == 2, (
        f"expected: agent.run AND run_orchestrated in the chat_handler, "
        f"found: {in_handler}")


def test_every_agent_entry_point_forwards_the_correction():
    """Either an explicit `corrected_intent=`, or `**kwargs`."""
    missing = [
        f"{source_file}:{line}"
        for source_file, line, kw_names in _call_sites()
        if "corrected_intent" not in kw_names
        and "**" not in kw_names
        and source_file not in _EXCEPTIONS
    ]
    assert not missing, (
        "an agent call site does not forward `corrected_intent` "
        "— a user correction silently has no effect there:\n  "
        + "\n  ".join(missing))
