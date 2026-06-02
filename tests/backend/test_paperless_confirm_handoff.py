"""Tests for the Paperless cold-start confirm → commit handoff bridge.

forward_attachment_to_paperless leaves a paperless_pending_confirms row and
shows the user a preview; the user's NEXT message is their reply. chat_handler
bridges that deterministically: a fresh pending confirm + a confirm-like reply
routes straight to internal.paperless_commit_upload (no router/agent guessing).

These cover the two pure-ish seams: the confirm-reply heuristic and the
per-session pending lookup. Import-stub pattern matches test_media_transport.py.
"""
import sys
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
import importlib as _importlib
for _mod in _missing_stubs:
    if _mod in sys.modules:
        continue
    try:
        _importlib.import_module(_mod)
    except Exception:  # noqa: BLE001
        sys.modules[_mod] = MagicMock()

import pytest

from api.websocket.chat_handler import (
    _looks_like_confirm_reply,
    _pending_paperless_confirm,
)


class TestLooksLikeConfirmReply:
    @pytest.mark.unit
    @pytest.mark.parametrize("msg", [
        "ja", "Ja", "JA", "ja, mach das", "nein", "nein danke", "yes", "no",
        "ok", "okay", "abbrechen", "cancel", "neu", "new", "x",
        "1:neu, 2:x 3:n 4:n5;n",       # the exact failing input from the report
        "1: 2, 2: neu",                # spaced per-field
        "3: x",
    ])
    def test_confirm_like_replies_match(self, msg):
        assert _looks_like_confirm_reply(msg) is True

    @pytest.mark.unit
    @pytest.mark.parametrize("msg", [
        "Was weißt du über mich?",
        "Bitte lade noch ein weiteres Dokument hoch",
        "Wie ist das Wetter morgen?",
        "",
        "   ",
        "Erzähl mir von der Rechnung",
    ])
    def test_non_confirm_messages_do_not_match(self, msg):
        assert _looks_like_confirm_reply(msg) is False


def _mock_session_returning(row):
    """AsyncSessionLocal stand-in whose query result yields `row` (or None)."""
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=row)

    async def _execute(_stmt):
        return mock_result

    mock_db.execute = _execute

    @asynccontextmanager
    async def _session():
        yield mock_db

    return _session


@pytest.mark.asyncio
class TestPendingPaperlessConfirm:
    @pytest.mark.unit
    async def test_returns_token_when_fresh_row_exists(self, monkeypatch):
        row = MagicMock()
        row.confirm_token = "7e171057-beef-dead"
        monkeypatch.setattr(
            "api.websocket.chat_handler.AsyncSessionLocal",
            _mock_session_returning(row),
        )
        token = await _pending_paperless_confirm("session-abc")
        assert token == "7e171057-beef-dead"

    @pytest.mark.unit
    async def test_returns_none_when_no_row(self, monkeypatch):
        monkeypatch.setattr(
            "api.websocket.chat_handler.AsyncSessionLocal",
            _mock_session_returning(None),
        )
        token = await _pending_paperless_confirm("session-abc")
        assert token is None

    @pytest.mark.unit
    async def test_lookup_failure_is_swallowed(self, monkeypatch):
        """The bridge is best-effort — a DB error must not raise into the chat
        loop; it returns None and the message falls through to the agent."""
        @asynccontextmanager
        async def _boom():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(
            "api.websocket.chat_handler.AsyncSessionLocal", _boom,
        )
        token = await _pending_paperless_confirm("session-abc")
        assert token is None
