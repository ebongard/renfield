"""End to end: a job that failed asynchronously reaches the PROMPT.

🛑 Why this file exists. On 2026-10-26 the owner asked three times for "scanne
nach Haushalt". The first scan failed on a paper jam; to the other two the
agent answered "der Scan laeuft bereits im Hintergrund, Du wirst
benachrichtigt" and NEVER called `scan_document`. An invented reassurance about
a scan that was not running.

Two gaps, and they were only lethal together:

1. `handle_job_event` wrote the completion notice without `action_success`, so
   the stale-error marker did not fire.
2. A live session's history is loaded once per socket; an asynchronous
   producer does not touch it, so the notice never reached the agent context
   at all.

Both repairs had tests -- but against MOCKS. The first checked that the field
is PASSED, not that a marker in the prompt ever comes out of it. That is
exactly why gap 2 slipped through and was only found in the review. This test
closes the chain: a real write -> a real load -> a real prompt.
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent_service import AgentContext, AgentService
from services.agent_tools import AgentToolRegistry
from services.conversation_service import ConversationService

pytestmark = [pytest.mark.backend, pytest.mark.database]


MARKER = "[VORHERIGE_FEHLGESCHLAGENE_AKTION]"


def _history_line(prompt: str, needle: str) -> str:
    """The history line that contains `needle`.

    🛑 Never search the whole prompt for the marker: the template's
    explanatory text NAMES it ("Zeilen, die mit ... beginnen, beschreiben
    historische Tool-Fehler"), so a substring test always matches and proves
    nothing. That exact mistake was in the first version of this test.
    """
    hits = [z for z in prompt.splitlines() if needle in z and "beschreiben" not in z]
    assert hits, f"no history line with {needle!r} in the prompt"
    return hits[0]


def _agent() -> AgentService:
    registry = MagicMock(spec=AgentToolRegistry)
    registry._tools = {}
    registry.get_tool_names.return_value = []
    registry.build_tools_prompt.return_value = "TOOLS: none"
    role = MagicMock()
    role.max_steps = 5
    role.prompt_key = "agent_prompt"
    role.model = None
    role.ollama_url = None
    role.name = "documents"
    return AgentService(registry, role=role)


async def _history_after_the_jam(db: AsyncSession, sid: str) -> list[dict]:
    """The measured history: the request, the promise, then the asynchronous
    jam notice."""
    service = ConversationService(db)
    await service.save_message(sid, "user", "scanne nach Haushalt")
    await service.save_message(
        sid, "assistant",
        "Der Scan für den Haushalt wurde gestartet. Du wirst benachrichtigt.",
        metadata={"action_success": True})
    # This is what `handle_job_event` writes -- asynchronously, after the turn.
    await service.save_message(
        sid, "assistant",
        "Der Scan ist fehlgeschlagen: Papierstau im Einzug.",
        metadata={"scanner_job": {"job_id": "b1abb766", "status": "failed"},
                  "action_success": False})
    return await service.load_context(sid, max_messages=10)


async def test_the_failed_scan_is_marked_in_the_built_prompt(db_session: AsyncSession):
    """The marker stands in the PROMPT -- not only in the database, not only
    in the loaded history."""
    history = await _history_after_the_jam(db_session, "prompt-kette-1")

    prompt = await _agent()._build_agent_prompt(
        "scanne nach Haushalt",
        AgentContext(original_message="scanne nach Haushalt"),
        conversation_history=history,
        lang="de",
    )

    jam_line = _history_line(prompt, "Papierstau")
    assert MARKER in jam_line, (
        f"the stale-error marker is missing on the jam line — the agent cannot "
        f"tell the failed scan apart from the present: {jam_line}")
    # And it sits ONLY there, not on the successful promise before it.
    promise_line = _history_line(prompt, "wurde gestartet")
    assert MARKER not in promise_line, (
        f"the successful promise was wrongly marked: {promise_line}")


async def test_the_template_keeps_its_hint_about_the_marker(db_session: AsyncSession):
    """Guards the SECOND half: the hint in the template, not the marker.

    🛑 The marker alone is NOT enough. Without the hint that such lines are
    HISTORICAL, the model reads the old failure as the present state and
    answers out of it -- instead of calling the tool again. Both halves have to
    stand in the prompt together; the rule says explicitly that they must never
    be changed one-sidedly."""
    history = await _history_after_the_jam(db_session, "prompt-kette-2")

    prompt = await _agent()._build_agent_prompt(
        "scanne nach Haushalt",
        AgentContext(original_message="scanne nach Haushalt"),
        conversation_history=history,
        lang="de",
    )

    # The line that EXPLAINS the marker -- not the one that carries it.
    hint = [z for z in prompt.splitlines() if MARKER in z and "beschreiben" in z]
    assert hint, (
        "the prompt carries the marker, but no line explains that it is "
        "HISTORICAL — then the model answers out of the old failure")
    lowered = hint[0].lower()
    assert any(w in lowered for w in ("neu aus", "erneut", "nochmal", "wiederhol")), (
        f"the hint does not call for a re-run: {hint[0]}")


async def test_without_the_metadata_there_is_no_marker(db_session: AsyncSession):
    """The negative control INSIDE the test: with `action_success` missing,
    the marker is missing. That was the state until 2026-10-26, and it is the
    reason the agent took the scan for running."""
    service = ConversationService(db_session)
    sid = "prompt-kette-3"
    await service.save_message(sid, "assistant", "Der Scan wurde gestartet.",
                               metadata={"action_success": True})
    await service.save_message(
        sid, "assistant", "Der Scan ist fehlgeschlagen: Papierstau im Einzug.",
        metadata={"scanner_job": {"job_id": "x", "status": "failed"}})  # WITHOUT the marker

    history = await service.load_context(sid, max_messages=10)
    prompt = await _agent()._build_agent_prompt(
        "scanne nach Haushalt",
        AgentContext(original_message="scanne nach Haushalt"),
        conversation_history=history, lang="de",
    )
    jam_line = _history_line(prompt, "Papierstau")
    assert MARKER not in jam_line, (
        f"without `action_success: False` the history line must not carry a "
        f"marker — otherwise the test above proves nothing: {jam_line}")


# ---------------------------------------------------------------------------
# A REPEATED request is a NEW request
# ---------------------------------------------------------------------------

async def test_a_repeat_after_a_completed_action_is_steered_to_rerun(db_session: AsyncSession):
    """🛑 The second defect from the same incident, and the fix above is what
    made it visible in the first place.

    Measured on 2026-10-26, 12:37 UTC. The owner said "scanne nach haushalt"
    after a scan had run through successfully. The agent answered -- verbatim
    from the log:

        "der Scan fuer den Haushalt ist bereits abgeschlossen und das Dokument
         (Dokument 515) wurde erfolgreich an die Instanz household uebergeben"

    with the reasoning: "Der Konversationsverlauf zeigt, dass der Scan bereits
    gestartet und abgeschlossen wurde." It took the NEW request for a question
    about the OLD scan and did not call the tool.

    Before the freshness check it did not see the completion notice at all --
    so the defect was unreachable. The `[VORHERIGE_FEHLGESCHLAGENE_AKTION]`
    marker covers only FAILED turns; for successful ones the counterpart was
    missing.
    """
    service = ConversationService(db_session)
    sid = "wiederholung-1"
    await service.save_message(sid, "user", "scanne nach haushalt")
    await service.save_message(sid, "assistant", "Der Scan wurde gestartet.",
                               metadata={"action_success": True})
    await service.save_message(
        sid, "assistant",
        "Der Scan ist fertig: 4 Seiten an „household“ übergeben.",
        metadata={"scanner_job": {"job_id": "x", "status": "done"},
                  "action_success": True})
    history = await service.load_context(sid, max_messages=10)

    prompt = await _agent()._build_agent_prompt(
        "scanne nach haushalt",
        AgentContext(original_message="scanne nach haushalt"),
        conversation_history=history, lang="de",
    )

    # 🛑 What this test PROVES and what it does not: it shows the hint stands
    # in the built prompt. It does NOT show that the model follows it -- that
    # is not available from a unit test and stays a question for acceptance
    # against the running system. The history built above is not decoration:
    # it reproduces exactly the situation in which it went wrong.
    assert "an einem GERAET ist eine NEUE Bitte" in prompt, (
        "the hint is missing — then the model answers out of the old run")
    # And it has to call for execution, not merely describe.
    hint = next(z for z in prompt.splitlines() if "an einem GERAET" in z)
    assert "fuehre das Tool aus" in hint, hint
    assert "schon erledigt" in hint, (
        "the hint must explicitly forbid the wrong answer")
    # 🛑 And the counter-rule has to stand in the prompt WITH it. Without it
    # the same sentence would send an email twice -- found in the review of
    # #1410: `send_email` is an active tool, and `broadcast_announcement` warns
    # itself, "Do NOT retry ... it would re-announce".
    assert "FRAGE ZURUECK" in prompt, (
        "the counter-rule for everything that SENDS something out is missing — the "
        "hint alone can repeat a send that cannot be recalled")


def test_the_repeat_hint_exists_in_both_languages():
    """🛑 The rule `agent-loop.md` says it for the failure marker, and it
    holds here just the same: both language variants carry the hint, or an
    English session runs without it.
    """
    from pathlib import Path

    import yaml

    p = Path(__file__).resolve().parents[2] / "src" / "backend" / "prompts" / "agent.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    # The templates live per language under `de:` / `en:` -- not at the top.
    assert {"de", "en"} <= set(d), sorted(d)
    de = d["de"]["conv_context_template"]
    en = d["en"]["conv_context_template"]
    # 🛑 Check BOTH halves equally hard. The first version checked only the
    # heading in English and the instruction as well in German -- the weaker
    # variant stayed uncovered, and unequal prompts produce unequal behaviour
    # per language.
    for name, v, marker, action, forbidden in (
        ("de", de, "an einem GERAET ist eine NEUE Bitte",
         "fuehre das Tool aus", "schon erledigt"),
        ("en", en, "at a DEVICE is a NEW request",
         "run the tool", "already done"),
    ):
        assert marker in v, f"the {name} template lacks the hint"
        line = next(z for z in v.splitlines() if marker in z)
        assert action in line, f"{name}: the hint does not call for execution: {line}"
        assert forbidden in line, f"{name}: the wrong behaviour is not forbidden"

    # And the counter-rule for everything that SENDS something out --
    # without it the hint could send an email twice (found in the review of
    # #1410).
    assert "FRAGE ZURUECK" in de, "the de template lacks the counter-rule"
    assert "ASK BACK" in en, "the en template lacks the counter-rule"
    # And the OLD marker has to keep carrying both; the rule explicitly
    # requires that the halves are never changed one-sidedly.
    for name, v in (("de", de), ("en", en)):
        assert "[VORHERIGE_FEHLGESCHLAGENE_AKTION]" in v, name
