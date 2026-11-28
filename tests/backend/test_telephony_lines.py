"""Telephony line alerts (#1680): line_status / unreachable gateway -> ops alert.

What must hold:
- flag off: nothing is alerted, line_status is still consumed (as before);
- a repeated ``down`` snapshot alerts ONCE; the first ``up`` after it sends ONE all-clear;
- ``up`` without a prior alert sends nothing; an alert that did not reach the admin is retried later;
- untrusted fields are vetted (only plain numbers, only up/down); no number in the log;
- the gateway alert fires only after the configured time, the all-clear on the next welcome;
- the client hands line_status to the handler and reports its connection state, and a
  failing hook never breaks the connection loop.
"""

import asyncio
import logging

import pytest

from services import ops_alert
from services import telephony_client as tc
from services import telephony_lines as tl
from tests.backend.test_telephony_client import MemoryStore, _wait_for, gateway  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit

DID = "+4921615745957"


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []
    state = {"told": True}

    async def notify_admin(**kw):
        calls.append(kw)
        return state["told"]

    ops_alert.reset_ledger()
    monkeypatch.setattr(ops_alert, "notify_admin", notify_admin)
    monkeypatch.setattr(tl.settings, "telephony_enabled", True)
    monkeypatch.setattr(tl.settings, "telephony_line_alerts_enabled", True)
    monkeypatch.setattr(tl.settings, "telephony_gateway_alert_after_seconds", 300)
    yield calls, state
    ops_alert.reset_ledger()


def snap(state, did=DID):
    return {"type": "line_status", "lines": [{"did": did, "state": state}]}


async def test_flag_off_alerts_nothing(sent, monkeypatch):
    calls, _ = sent
    monkeypatch.setattr(tl.settings, "telephony_line_alerts_enabled", False)
    await tl.handle_line_status(snap("down"))
    await tl.handle_gateway_state(False, 10_000)
    assert calls == []


async def test_repeated_down_alerts_once_and_up_clears_once(sent):
    calls, _ = sent
    for _ in range(3):
        await tl.handle_line_status(snap("down"))
    assert [c["title"] for c in calls] == ["Telefonie: Nummer abgemeldet"]
    assert "021615745957" in calls[0]["message"]
    await tl.handle_line_status(snap("up"))
    await tl.handle_line_status(snap("up"))
    assert [c["title"] for c in calls][1:] == ["Telefonie: Nummer wieder angemeldet"]
    await tl.handle_line_status(snap("down"))  # a new outage alerts again at once
    assert len(calls) == 3


async def test_up_without_an_alert_sends_nothing(sent):
    calls, _ = sent
    await tl.handle_line_status({"type": "line_status", "lines": [{"did": DID, "state": "up"},
                                                                  {"did": "+4921615745898", "state": "up"}]})
    assert calls == []


async def test_an_alert_that_was_not_delivered_is_retried_later(sent, monkeypatch):
    calls, state = sent
    state["told"] = False
    await tl.handle_line_status(snap("down"))
    await tl.handle_line_status(snap("down"))
    assert len(calls) == 1  # deferred, not every snapshot
    now = ops_alert._now()
    monkeypatch.setattr(ops_alert, "_now", lambda: now + tl.RETRY_SECONDS + 1)
    state["told"] = True
    await tl.handle_line_status(snap("down"))
    assert len(calls) == 2


async def test_untrusted_fields_are_vetted_and_no_number_is_logged(sent, caplog):
    calls, _ = sent
    bad = {"type": "line_status", "lines": [
        {"did": "<b>x</b>", "state": "down"}, {"did": DID, "state": "broken"}, "junk",
        {"did": DID, "state": "down", "title": "forged"},
    ]}
    with caplog.at_level(logging.INFO):
        await tl.handle_line_status(bad)
        await tl.handle_line_status({"type": "line_status", "lines": "nope"})
    assert len(calls) == 1 and calls[0]["data"] == {"did": DID, "line_state": "down"}
    assert "5745957" not in caplog.text


async def test_gateway_alert_after_the_threshold_and_clear_on_welcome(sent):
    calls, _ = sent
    await tl.handle_gateway_state(False, 120)
    assert calls == []
    await tl.handle_gateway_state(False, 360)
    await tl.handle_gateway_state(False, 420)
    assert [c["title"] for c in calls] == ["Telefonie-Gateway nicht erreichbar"]
    assert "6 Minuten" in calls[0]["message"]
    await tl.handle_gateway_state(True, 0)
    await tl.handle_gateway_state(True, 0)
    assert [c["title"] for c in calls][1:] == ["Telefonie-Gateway wieder erreichbar"]


async def test_a_failing_alert_path_never_raises(sent, monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(ops_alert, "notify_admin", boom)
    await tl.handle_line_status(snap("down"))
    await tl.handle_gateway_state(False, 10_000)


async def test_client_hands_line_status_to_the_handler_and_reports_state(gateway, monkeypatch):  # noqa: F811
    seen, states = [], []

    async def handler(event):
        seen.append(event)

    async def hook(connected, down_for):
        states.append(connected)
        raise RuntimeError("hook broken")  # must not break the loop

    monkeypatch.setattr(tl, "handle_line_status", handler)
    store, delivered = MemoryStore(), []

    async def deliver(event):
        delivered.append(event)

    client = tc.TelephonyClient(gateway.url, "tok", store, deliver, on_gateway_state=hook)
    task = asyncio.create_task(client.run())
    try:
        await _wait_for(lambda: gateway.conns)
        gateway.add(type="line_status", lines=[{"did": DID, "state": "down"}])
        gateway.add(type="line_status", lines=[{"did": DID, "state": "down"}])  # no call_id, not deduplicated
        gateway.add(type="incoming_call", call_id="c1", did=DID, caller="+491701234567")
        await _wait_for(lambda: store.seq == 3)
        assert len(seen) == 2 and [e["call_id"] for e in delivered] == ["c1"]
        await gateway.kick_all()
        await _wait_for(lambda: False in states and states.count(True) >= 2)
    finally:
        task.cancel()
    assert states[0] is True
