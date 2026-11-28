"""Outbound calls (#1683, Telefonie Phase 4): numbers, audio, place_call, results, code call, tool, routes.

What must hold:
- only German mobile/landline numbers are callable (the gateway's rule, mirrored);
- the audio is WAV 8 kHz mono 16 bit with a lead-in, repeated when short, never > 58 s;
- place_call refuses when off, over the per-user hourly limit, or without a gateway (nothing pending left);
- call_result notifies ONLY the call's owner, only when not answered, and a replay does nothing;
- the code call stores the number only after the right code; wrong codes run out;
- the tool never takes a number from its arguments, and fails closed (auth off, unidentified turn,
  no permission, no confirmed number);
- the routes 404 while dark and refuse with auth off.
"""

import base64
import io
import json
import wave

import pytest

from services import telephony_client as tc
from services import telephony_outbound as to

pytestmark = pytest.mark.unit

MOBILE = "+491701234567"


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, k):
        return self.data.get(k)

    async def set(self, k, v, ex=None):
        self.data[k] = v
        if ex:
            self.ttls[k] = ex

    async def delete(self, k):
        self.data.pop(k, None)

    async def getdel(self, k):
        return self.data.pop(k, None)

    async def incr(self, k):
        self.data[k] = str(int(self.data.get(k, "0")) + 1)
        return int(self.data[k])

    async def expire(self, k, s):
        self.ttls[k] = s

    async def ttl(self, k):
        return self.ttls.get(k, -1)


def wav(seconds=1.0, rate=22050, channels=1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(seconds * rate) * channels)
    return buf.getvalue()


def info(data: bytes) -> tuple[int, int, int, float]:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes() / w.getframerate()


@pytest.fixture
def env(monkeypatch):
    redis = FakeRedis()
    sent: list[dict] = []
    notes: list[dict] = []
    state = {"gateway": True, "tts_seconds": 2.0}

    async def send_command(msg):
        if not state["gateway"]:
            raise tc.GatewayUnavailable()
        sent.append(msg)

    async def synth(text):
        return to.phone_wav(wav(state["tts_seconds"]))

    async def notify(user_id, title, message):
        notes.append({"user_id": user_id, "title": title, "message": message})

    import services.redis_client as rc

    monkeypatch.setattr(rc, "get_redis", lambda: redis)
    monkeypatch.setattr(tc, "send_command", send_command)
    monkeypatch.setattr(to, "synthesize", synth)
    monkeypatch.setattr(to, "_notify", notify)
    monkeypatch.setattr(to.settings, "telephony_enabled", True)
    monkeypatch.setattr(to.settings, "telephony_outbound_enabled", True)
    monkeypatch.setattr(to.settings, "telephony_call_per_hour", 2)
    return redis, sent, notes, state


# --------------------------------------------------------------------------- numbers / audio


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0170 1234567", MOBILE),
        ("+49 2161 5745898", "+4921615745898"),
        ("09001234567", None),
        ("01801234567", None),
        ("+44 20 7946 0958", None),
        ("110", None),
        ("abc", None),
    ],
)
def test_callable_number(value, expected):
    assert to.callable_number(value) == expected


def test_phone_wav_is_8k_mono_with_lead_in_and_repeat():
    ch, width, rate, secs = info(to.phone_wav(wav(2.0, rate=22050, channels=2)))
    assert (ch, width, rate) == (1, 2, 8000)
    assert secs == pytest.approx(2 * (to.LEAD_IN_SECONDS + 2.0), abs=0.05)  # short: said twice


def test_phone_wav_long_is_said_once_and_capped():
    assert info(to.phone_wav(wav(30.0)))[3] == pytest.approx(31.0, abs=0.05)
    assert info(to.phone_wav(wav(70.0)))[3] == pytest.approx(to.MAX_AUDIO_SECONDS, abs=0.01)


# --------------------------------------------------------------------------- place_call


async def test_place_call_sends_the_command_and_keeps_the_owner(env):
    redis, sent, _, _ = env
    placed = await to.place_call(user_id=7, number="0170 1234567", text="Der Ofen ist an")
    cmd = sent[0]
    assert cmd["type"] == "place_call" and cmd["id"] == placed.call_id and cmd["to"] == MOBILE
    assert info(base64.b64decode(cmd["audio_b64"]))[2] == 8000
    pending = json.loads(redis.data[f"{to.PENDING_PREFIX}{placed.call_id}"])
    assert pending == {"user_id": 7, "purpose": "message", "text": "Der Ofen ist an"}


async def test_place_call_refusals(env, monkeypatch):
    redis, sent, _, state = env
    with pytest.raises(to.CallError) as e:
        await to.place_call(user_id=7, number="09001234567", text="x")
    assert e.value.code == "target"
    state["gateway"] = False
    with pytest.raises(to.CallError) as e:
        await to.place_call(user_id=7, number=MOBILE, text="x")
    assert e.value.code == "gateway"
    assert not [k for k in redis.data if k.startswith(to.PENDING_PREFIX)]  # nothing left pending
    state["gateway"] = True
    await to.place_call(user_id=7, number=MOBILE, text="x")  # 2nd counted call this hour
    with pytest.raises(to.CallError) as e:
        await to.place_call(user_id=7, number=MOBILE, text="x")
    assert e.value.code == "rate"
    await to.place_call(user_id=8, number=MOBILE, text="x")  # per user
    monkeypatch.setattr(to.settings, "telephony_outbound_enabled", False)
    with pytest.raises(to.CallError) as e:
        await to.place_call(user_id=9, number=MOBILE, text="x")
    assert e.value.code == "disabled"
    assert len(sent) == 2


# --------------------------------------------------------------------------- call_result


async def test_unanswered_call_notifies_only_its_owner_once(env):
    _, _, notes, _ = env
    placed = await to.place_call(user_id=7, number=MOBILE, text="Der Ofen ist an")
    event = {"type": "call_result", "id": placed.call_id, "status": "no_answer"}
    await to.handle_call_result(event)
    await to.handle_call_result(event)  # replay after a reconnect
    assert notes == [{"user_id": 7, "title": "Anruf von Renfield nicht angenommen", "message": "Der Ofen ist an"}]


async def test_answered_or_foreign_results_notify_nobody(env):
    _, _, notes, _ = env
    placed = await to.place_call(user_id=7, number=MOBILE, text="x")
    await to.handle_call_result({"type": "call_result", "id": placed.call_id, "status": "answered"})
    await to.handle_call_result({"type": "call_result", "id": "f" * 32, "status": "no_answer"})
    await to.handle_call_result({"type": "call_result", "id": "short", "status": "no_answer"})
    assert notes == []


async def test_a_failing_result_handler_never_raises(env, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("redis down")

    import services.redis_client as rc

    monkeypatch.setattr(rc, "get_redis", lambda: type("R", (), {"getdel": boom})())
    await to.handle_call_result({"type": "call_result", "id": "a" * 32, "status": "no_answer"})


# --------------------------------------------------------------------------- code call


async def test_code_call_stores_nothing_until_the_right_code(env, monkeypatch):
    _, sent, notes, _ = env
    spoken = []

    async def synth(text):
        spoken.append(text)
        return to.phone_wav(wav(1.0))

    monkeypatch.setattr(to, "synthesize", synth)
    await to.start_verification(7, "0170 1234567")
    assert await to.pending_verification(7) == MOBILE
    code = "".join(c for c in spoken[0].split("lautet:")[1].split(".")[0] if c.isdigit())
    assert len(code) == 4
    wrong = "0000" if code != "0000" else "1111"
    assert await to.confirm_verification(7, wrong) is None
    assert await to.confirm_verification(8, code) is None  # another user's code store
    assert await to.confirm_verification(7, code) == MOBILE
    assert await to.pending_verification(7) is None
    # a missed code call tells the owner, without any code in the notice
    await to.handle_call_result({"type": "call_result", "id": sent[0]["id"], "status": "no_answer"})
    assert notes and code not in notes[0]["message"] and notes[0]["title"].startswith("Bestätigungsanruf")


async def test_wrong_codes_run_out(env, monkeypatch):
    async def synth(text):
        return to.phone_wav(wav(1.0))

    monkeypatch.setattr(to, "synthesize", synth)
    await to.start_verification(7, MOBILE)
    for _ in range(to.VERIFY_MAX_ATTEMPTS):
        await to.confirm_verification(7, "abcd")
    assert await to.pending_verification(7) is None


async def test_code_call_refuses_a_foreign_number(env):
    with pytest.raises(to.CallError):
        await to.start_verification(7, "+44 20 7946 0958")
    assert await to.pending_verification(7) is None


# --------------------------------------------------------------------------- tool


class _User:
    def __init__(self, number=MOBILE, verified=True):
        self.call_number = number
        self.call_number_verified_at = object() if verified else None


@pytest.fixture
def tool(env, monkeypatch):
    from services import phone_call_tool as pt

    placed = []
    users = {7: _User(), 8: _User(verified=False)}

    async def place_call(*, user_id, number, text, purpose="message"):
        placed.append((user_id, number, text))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _model, uid):
            return users.get(uid)

    import services.database as dbmod

    monkeypatch.setattr(to, "place_call", place_call)
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", Session)
    monkeypatch.setattr(pt.settings, "auth_enabled", True)
    return pt, placed


async def test_tool_calls_the_own_number_never_an_argument(tool):
    pt, placed = tool
    res = await pt.phone_call({"message": "Der Ofen ist an", "number": "+4930999", "to": "+4930999"},
                              user_id=7, user_permissions=["telephony.call"])
    assert res["success"] and placed == [(7, MOBILE, "Der Ofen ist an")]


@pytest.mark.parametrize(
    "kwargs,auth",
    [
        ({"user_id": 7, "user_permissions": None}, True),  # unidentified turn
        ({"user_id": None, "user_permissions": ["telephony.call"]}, True),
        ({"user_id": 7, "user_permissions": ["chat.own"]}, True),  # no permission
        ({"user_id": 8, "user_permissions": ["telephony.call"]}, True),  # number not confirmed
        ({"user_id": 7, "user_permissions": ["telephony.call"]}, False),  # auth off
    ],
)
async def test_tool_fails_closed(tool, monkeypatch, kwargs, auth):
    pt, placed = tool
    monkeypatch.setattr(pt.settings, "auth_enabled", auth)
    res = await pt.phone_call({"message": "x"}, **kwargs)
    assert not res["success"] and placed == []


async def test_tool_is_registered_only_while_on(env, monkeypatch):
    from services import agent_tools

    def names():
        reg = agent_tools.AgentToolRegistry.__new__(agent_tools.AgentToolRegistry)
        reg._tools = {}
        reg._register_internal_tools()
        return set(reg._tools)

    assert "internal.phone_call" in names()
    monkeypatch.setattr(to.settings, "telephony_outbound_enabled", False)
    assert "internal.phone_call" not in names()


def test_admin_and_family_may_be_called_guests_not():
    from models.permissions import DEFAULT_ROLES, Permission

    roles = {r["name"]: r["permissions"] for r in DEFAULT_ROLES}
    assert Permission.TELEPHONY_CALL.value in roles["Admin"]
    assert Permission.TELEPHONY_CALL.value in roles["Familie"]
    assert Permission.TELEPHONY_CALL.value not in roles["Gast"]


# --------------------------------------------------------------------------- client / routes


async def test_send_command_without_a_client_is_gateway_unavailable(monkeypatch):
    monkeypatch.setattr(tc, "_active", None)
    with pytest.raises(tc.GatewayUnavailable):
        await tc.send_command({"type": "place_call"})


def test_routes_404_while_dark_and_refuse_auth_off(monkeypatch):
    from fastapi import HTTPException

    from api.routes import preferences as pr

    monkeypatch.setattr(to.settings, "telephony_enabled", True)
    monkeypatch.setattr(to.settings, "telephony_outbound_enabled", False)
    with pytest.raises(HTTPException) as e:
        pr._call_number_user(object())
    assert e.value.status_code == 404
    monkeypatch.setattr(to.settings, "telephony_outbound_enabled", True)
    monkeypatch.setattr(pr.settings, "auth_enabled", False)
    with pytest.raises(HTTPException) as e:
        pr._call_number_user(object())
    assert e.value.status_code == 403
