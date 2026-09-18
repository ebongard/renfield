"""Tests for the shared `check_output` gate (services/output_gate.py).

Regression cover for #1269: the gate was documented as fail-closed and
firing before any response reaches the user, but only the orchestrator branch
ever called it. A plugin that registered the hook got no signal that its gate
was dead on the common path. These tests pin the CONTRACT, not the call site,
so a future refactor that moves the call cannot quietly drop it again.
"""

import pytest

from services.output_gate import apply_check_output_gate, stream_or_gate
from utils.hooks import clear_hooks, register_hook


@pytest.fixture(autouse=True)
def _isolate_hooks():
    clear_hooks()
    yield
    clear_hooks()


class _FakeSocket:
    """Captures what the handler would have sent to the browser."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def _chunks(*parts: str):
    for p in parts:
        yield p


# --- apply_check_output_gate ---


@pytest.mark.asyncio
async def test_no_handler_returns_content_unchanged():
    assert await apply_check_output_gate("Hallo") == "Hallo"


@pytest.mark.asyncio
async def test_handler_redaction_is_applied():
    async def _redact(content="", **kwargs):
        return content.replace("Kundennummer 4711", "[redigiert]")

    register_hook("check_output", _redact)
    out = await apply_check_output_gate("Die Kundennummer 4711 gehoert zu Meier GmbH.")
    assert "4711" not in out
    assert "[redigiert]" in out


@pytest.mark.asyncio
async def test_crashing_handler_fails_closed():
    """A broken redactor must NOT let the original text through."""
    async def _boom(**kwargs):
        raise RuntimeError("redactor down")

    register_hook("check_output", _boom)
    out = await apply_check_output_gate("Geheime Daten", lang="de")
    assert "Geheime Daten" not in out
    assert "nicht vollständig geprüft" in out


@pytest.mark.asyncio
async def test_extreme_redaction_is_treated_as_a_bug():
    """Losing >95 % of the answer is a redactor bug, not a redaction."""
    async def _nuke(content="", **kwargs):
        return "x"

    register_hook("check_output", _nuke)
    out = await apply_check_output_gate("A" * 500, lang="de")
    assert out == "[Inhalt zur Datenschutzprüfung zurückgehalten]"


@pytest.mark.asyncio
async def test_english_fallback_wording():
    async def _boom(**kwargs):
        raise RuntimeError("nope")

    register_hook("check_output", _boom)
    out = await apply_check_output_gate("secret", lang="en")
    assert out == "Response could not be fully validated. Please try again."


@pytest.mark.asyncio
async def test_handler_receives_role_and_user():
    seen: dict = {}

    async def _spy(content="", role=None, user_id=None, **kwargs):
        seen.update({"content": content, "role": role, "user_id": user_id})
        return None

    register_hook("check_output", _spy)
    await apply_check_output_gate("Text", role_name="release", user_id=7)
    assert seen == {"content": "Text", "role": "release", "user_id": 7}


@pytest.mark.asyncio
async def test_empty_content_is_not_gated():
    """An empty turn has nothing to redact and must not wake handlers."""
    called = False

    async def _spy(**kwargs):
        nonlocal called
        called = True

    register_hook("check_output", _spy)
    assert await apply_check_output_gate("") == ""
    assert called is False


# --- stream_or_gate: the streaming/buffering decision ---


@pytest.mark.asyncio
async def test_without_handler_chunks_stream_live():
    """No gate registered → byte-identical to the pre-#1269 behaviour."""
    ws = _FakeSocket()
    out = await stream_or_gate(_chunks("Hal", "lo ", "Welt"), ws)

    assert out == "Hallo Welt"
    assert [m["content"] for m in ws.sent] == ["Hal", "lo ", "Welt"]


@pytest.mark.asyncio
async def test_with_handler_nothing_is_sent_before_the_gate():
    """A gate that redacts after the tokens are on screen is not a gate."""
    async def _redact(content="", **kwargs):
        return content.replace("4711", "[redigiert]")

    register_hook("check_output", _redact)
    ws = _FakeSocket()
    out = await stream_or_gate(_chunks("Nummer ", "4711", " ist es"), ws)

    assert out == "Nummer [redigiert] ist es"
    assert len(ws.sent) == 1, "gated streams must be sent as ONE post-gate frame"
    assert ws.sent[0] == {"type": "stream", "content": "Nummer [redigiert] ist es"}
    assert "4711" not in ws.sent[0]["content"]


@pytest.mark.asyncio
async def test_with_crashing_handler_stream_never_leaks():
    async def _boom(**kwargs):
        raise RuntimeError("redactor down")

    register_hook("check_output", _boom)
    ws = _FakeSocket()
    out = await stream_or_gate(_chunks("Geheim", "nis"), ws, lang="de")

    assert "Geheimnis" not in out
    assert all("Geheimnis" not in m["content"] for m in ws.sent)
