"""
Unit tests for the two-step, human-gated Simba bridge:
`internal.forward_attachment_to_simba` (preview + token, NEVER uploads) and
`internal.simba_commit_upload` (the real upload, only after the user confirms).
"""
import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from services.simba_forward_tool import (
    SIMBA_FORWARD_TOOL,
    forward_attachment_to_simba,
    simba_commit_upload,
)

pytestmark = pytest.mark.unit

FILE_BYTES = b"%PDF-1.4 hello"
B64 = base64.b64encode(FILE_BYTES).decode("ascii")


def _upload(uid=5, filename="2026_07_14.pdf", path="/data/x.pdf"):
    u = MagicMock()
    u.id = uid
    u.filename = filename
    u.file_path = path
    u.status = "completed"
    return u


def _mcp(result=None):
    m = MagicMock()
    m.execute_tool = AsyncMock(return_value=result or {"success": True, "message": "{}"})
    return m


def _db_patch(upload):
    db = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=upload)
    db.execute = AsyncMock(return_value=res)

    @asynccontextmanager
    async def _session(*_a, **_k):
        yield db

    return patch("services.database.AsyncSessionLocal", lambda *a, **k: _session())


def _fs():
    return patch("pathlib.Path.is_file", return_value=True), patch(
        "builtins.open", mock_open(read_data=FILE_BYTES)
    )


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def setex(self, k, _ttl, v):
        self.store[k] = v

    async def get(self, k):
        return self.store.get(k)

    async def delete(self, k):
        self.store.pop(k, None)


def _redis_patch(fake):
    return patch("services.redis_client.get_redis", return_value=fake)


# ---------------------------------------------------------------------------
# Tool defs
# ---------------------------------------------------------------------------

def test_both_tools_defined():
    assert "internal.forward_attachment_to_simba" in SIMBA_FORWARD_TOOL
    assert "internal.simba_commit_upload" in SIMBA_FORWARD_TOOL


# ---------------------------------------------------------------------------
# forward: preview only, NEVER uploads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_previews_and_stores_token_never_uploads():
    upload = _upload()
    mcp = _mcp({"success": True, "message": '{"modus":"Probelauf"}'})
    redis = FakeRedis()
    p_isfile, p_open = _fs()
    with _db_patch(upload), p_isfile, p_open, _redis_patch(redis):
        r = await forward_attachment_to_simba(
            {"category": "Posteingang", "type": "Schriftverkehr", "month": 7, "year": 2026},
            mcp_manager=mcp, session_id="sess", user_id=1,
        )
    # dry-run only — no real upload
    assert mcp.execute_tool.await_count == 1
    dry_args = mcp.execute_tool.await_args.args[1]
    assert dry_args["dry_run"] is True
    assert dry_args["files"][0]["content_base64"] == B64
    # returns a confirm token, nothing uploaded
    assert r["action_taken"] is False
    assert r["data"]["action_required"] == "simba_confirm"
    token = r["data"]["confirm_token"]
    assert f"simba:pending:{token}" in redis.store


@pytest.mark.asyncio
async def test_forward_requires_category_and_type():
    r = await forward_attachment_to_simba({"category": "Belege"}, mcp_manager=_mcp(), session_id="s")
    assert r["success"] is False and "type" in r["message"]


@pytest.mark.asyncio
async def test_forward_dryrun_validation_failure_returns_error_no_token():
    upload = _upload()
    mcp = _mcp({"success": True, "message": '{"fehler":[{"file":"x","error":"maximal 15 MB"}]}'})
    redis = FakeRedis()
    p_isfile, p_open = _fs()
    with _db_patch(upload), p_isfile, p_open, _redis_patch(redis):
        r = await forward_attachment_to_simba(
            {"category": "Belege", "type": "x"}, mcp_manager=mcp, session_id="s",
        )
    assert r["success"] is False
    assert "15 MB" in r["message"]
    assert redis.store == {}  # no token persisted on a bad file


# ---------------------------------------------------------------------------
# commit: real upload only after the user confirms
# ---------------------------------------------------------------------------

def _seed(redis, token="tok-1", session_id="sess", **over):
    rec = {
        "attachment_id": 5, "filename": "2026_07_14.pdf", "category": "Posteingang",
        "type": "Schriftverkehr", "description": None, "comment": None,
        "session_id": session_id, "user_id": 1, "month": 7, "year": 2026,
    }
    rec.update(over)
    redis.store[f"simba:pending:{token}"] = json.dumps(rec)
    return token


@pytest.mark.asyncio
async def test_commit_yes_does_real_upload():
    redis = FakeRedis()
    _seed(redis)
    upload = _upload()
    mcp = _mcp({"success": True, "message": '{"uebertragen":1,"fehlgeschlagen":0}'})
    p_isfile, p_open = _fs()
    with _redis_patch(redis), _db_patch(upload), p_isfile, p_open:
        r = await simba_commit_upload(
            {"confirm_token": "tok-1", "user_response_text": "ja, bitte übertragen"},
            mcp_manager=mcp, session_id="sess",
        )
    args = mcp.execute_tool.await_args.args[1]
    assert args["dry_run"] is False and args["confirm"] is True
    assert args["files"][0]["content_base64"] == B64
    assert r["success"] is True and r["action_taken"] is True
    assert "simba:pending:tok-1" not in redis.store  # single-use


@pytest.mark.asyncio
async def test_commit_no_aborts_without_upload():
    redis = FakeRedis()
    _seed(redis)
    mcp = _mcp()
    with _redis_patch(redis):
        r = await simba_commit_upload(
            {"confirm_token": "tok-1", "user_response_text": "nein, lieber nicht"},
            mcp_manager=mcp, session_id="sess",
        )
    mcp.execute_tool.assert_not_awaited()
    assert r["action_taken"] is False
    assert "NICHTS" in r["message"]
    assert "simba:pending:tok-1" not in redis.store


@pytest.mark.asyncio
async def test_commit_ambiguous_asks_again_no_upload():
    redis = FakeRedis()
    _seed(redis)
    mcp = _mcp()
    with _redis_patch(redis):
        r = await simba_commit_upload(
            {"confirm_token": "tok-1", "user_response_text": "was meinst du?"},
            mcp_manager=mcp, session_id="sess",
        )
    mcp.execute_tool.assert_not_awaited()
    assert r["action_taken"] is False
    assert "simba:pending:tok-1" in redis.store  # kept for a retry


@pytest.mark.asyncio
async def test_commit_unknown_token():
    redis = FakeRedis()
    mcp = _mcp()
    with _redis_patch(redis):
        r = await simba_commit_upload(
            {"confirm_token": "missing", "user_response_text": "ja"}, mcp_manager=mcp, session_id="sess",
        )
    assert r["success"] is False and "abgelaufen" in r["message"].lower()
    mcp.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_cross_session_token_rejected():
    redis = FakeRedis()
    _seed(redis, session_id="other-sess")
    mcp = _mcp()
    with _redis_patch(redis):
        r = await simba_commit_upload(
            {"confirm_token": "tok-1", "user_response_text": "ja"}, mcp_manager=mcp, session_id="my-sess",
        )
    assert r["success"] is False
    mcp.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_yes_but_zero_transferred_is_honest_failure():
    redis = FakeRedis()
    _seed(redis)
    upload = _upload()
    mcp = _mcp({
        "success": True,
        "message": '{"uebertragen":0,"fehlgeschlagen":1,"ergebnisse":[{"ok":false,"status":401,"response":"denied"}]}',
    })
    p_isfile, p_open = _fs()
    with _redis_patch(redis), _db_patch(upload), p_isfile, p_open:
        r = await simba_commit_upload(
            {"confirm_token": "tok-1", "user_response_text": "ja"}, mcp_manager=mcp, session_id="sess",
        )
    assert r["success"] is False and "NICHT angekommen" in r["message"]
    assert "401" in r["message"]
