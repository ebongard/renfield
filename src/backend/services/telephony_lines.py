"""Telephony line alerts (#1680): tell the owner admin when a number of this
instance drops off the Telekom registration, or the gateway itself is gone.

Two signals, one alert path (``ops_alert``: owner admin, dedup ledger, re-alert TTL):

- ``line_status`` from the gateway: a full snapshot of THIS instance's numbers
  ``{"lines": [{"did": "+49…", "state": "up" | "down"}]}``. The gateway already
  debounced it (``down`` only after 2 min) and repeats it while something is down,
  so a snapshot can arrive many times: the ledger turns that into one alert, and
  the first ``up`` after an alert into one all-clear.
- The client could not reach the gateway for ``TELEPHONY_GATEWAY_ALERT_AFTER_SECONDS``
  (the LXC is down, the gateway crashed, the network between the zones broke) --
  then no call, voicemail, fax or line_status arrives at all.

An all-clear is sent only for an alert that REACHED the admin (``_told``; the
ops_alert ledger also holds deferred keys that never did). An all-clear that does
not get through stays pending: the gateway repeats its snapshot every 10 min, so
the next ``up`` tries again -- and also catches up on an ``up`` missed while this
instance was disconnected. Both are in-process: after a pod restart an ongoing
outage alerts once more and a recovery meanwhile sends no all-clear -- the safe
direction. Nothing here raises: an alert must never break the client. Logs carry
no number.
"""

from __future__ import annotations

import logging

from services import ops_alert
from utils.config import settings

logger = logging.getLogger(__name__)

LINE_STATUS_TYPE = "line_status"
LINE_PREFIX = "telephony_line:"
GATEWAY_KEY = "telephony_gateway"
RETRY_SECONDS = 300.0  # an alert that did not reach the admin is retried after this
MAX_LINES = 20

# Keys whose alert reached the admin and still await an all-clear.
_told: set[str] = set()


def reset() -> None:
    """Test hook."""
    _told.clear()


def enabled() -> bool:
    return bool(settings.telephony_line_alerts_enabled and settings.telephony_enabled)


def _lines(event: dict) -> list[tuple[str, str]]:
    """The vetted (did, state) pairs of a snapshot; anything else is dropped."""
    from services.telephony_client import _plain_number

    raw = event.get("lines")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_LINES]:
        if not isinstance(item, dict):
            continue
        did = _plain_number(item.get("did"))
        state = item.get("state")
        if did and state in ("up", "down"):
            out.append((did, state))
    return out


async def _alert(key: str, title: str, message: str, data: dict) -> None:
    if not ops_alert.should_alert(key):
        return
    if await ops_alert.notify_admin(
        title=title, message=message, dedup_key=key, data=data, event_type="telephony_ops", source="telephony"
    ):
        _told.add(key)
    else:
        ops_alert.defer_alert(key, RETRY_SECONDS)


async def _all_clear(key: str, title: str, message: str, data: dict) -> None:
    ops_alert.clear_alert(key)  # recovered: a new outage alerts at once (also drops a pending retry)
    if key not in _told:
        return  # the admin was never told (or the pod restarted since): nothing to clear
    if await ops_alert.notify_admin(
        title=title, message=message, dedup_key=f"{key}:ok", data=data, event_type="telephony_ops",
        source="telephony", urgency="info",
    ):
        _told.discard(key)  # else: the next "up" (the gateway repeats it) tries again


async def handle_line_status(event: dict) -> None:
    if not enabled():
        return
    from services.telephony_client import format_number

    try:
        for did, state in _lines(event):
            key = f"{LINE_PREFIX}{did}"
            shown = format_number(did)
            data = {"did": did, "line_state": state}
            if state == "down":
                await _alert(
                    key, "Telefonie: Nummer abgemeldet",
                    f"{shown} ist nicht mehr bei der Telekom angemeldet — Anrufe auf diese Nummer kommen nicht an.",
                    data,
                )
            else:
                await _all_clear(key, "Telefonie: Nummer wieder angemeldet",
                                 f"{shown} ist wieder bei der Telekom angemeldet.", data)
    except Exception as e:  # never break the event stream
        logger.warning("Telephony line alert failed: %s", type(e).__name__)


async def handle_gateway_state(connected: bool, down_for_seconds: float) -> None:
    """Called by the client: on every welcome (connected) and after every failed attempt."""
    if not enabled():
        return
    try:
        if connected:
            await _all_clear(GATEWAY_KEY, "Telefonie-Gateway wieder erreichbar",
                             "Renfield ist wieder mit dem Telefonie-Gateway verbunden.", {})
        elif down_for_seconds >= settings.telephony_gateway_alert_after_seconds:
            minutes = max(1, int(down_for_seconds // 60))
            await _alert(
                GATEWAY_KEY, "Telefonie-Gateway nicht erreichbar",
                f"Renfield erreicht das Telefonie-Gateway seit {minutes} Minuten nicht — Anrufmeldungen, "
                "Anrufbeantworter und Fax kommen nicht an.",
                {"down_for_s": int(down_for_seconds)},
            )
    except Exception as e:
        logger.warning("Telephony gateway alert failed: %s", type(e).__name__)
