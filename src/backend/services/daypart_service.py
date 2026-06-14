"""
Day/night awareness — compute the current time-of-day ("daypart") so the agent
LLM can see it in its prompt and features can react to day/evening/night
transitions via the `daypart_changed` hook.

Everything here is best-effort and must NEVER break the agent path:
``build_time_context`` wraps all work in try/except and returns "" on any error.

Daypart windows are configurable HH:MM strings in the local timezone
(``settings.daypart_*``). The timezone is resolved from
``settings.daypart_timezone`` (if set), else ha_glue's
``presence_analytics_timezone`` (import-guarded — ha_glue may be absent), else
UTC.
"""

from datetime import datetime, time as dt_time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from utils.config import settings

Daypart = Literal["day", "evening", "night"]

# Safe fallback windows — used only if a configured HH:MM string is malformed.
_DEFAULT_NIGHT_START = dt_time(22, 0)
_DEFAULT_NIGHT_END = dt_time(7, 0)
_DEFAULT_EVENING_START = dt_time(18, 0)

# Localized weekday names (Mon=0 .. Sun=6), keyed by language.
_WEEKDAYS = {
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}

# Localized daypart labels.
_DAYPART_LABELS = {
    "de": {"day": "Tag", "evening": "Abend", "night": "Nacht"},
    "en": {"day": "Day", "evening": "Evening", "night": "Night"},
}


def _resolve_tz() -> ZoneInfo:
    """Resolve the local timezone for daypart computation.

    Order: ``settings.daypart_timezone`` (if non-empty) → ha_glue's
    ``presence_analytics_timezone`` (import-guarded) → ``"UTC"``. An invalid
    name falls back to UTC without raising.
    """
    name = (settings.daypart_timezone or "").strip()
    if not name:
        try:
            from ha_glue.utils.config import ha_glue_settings

            name = (ha_glue_settings.presence_analytics_timezone or "").strip()
        except Exception:  # noqa: BLE001 — ha_glue may be absent
            name = ""
    if not name:
        name = "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(f"Invalid daypart timezone {name!r}; falling back to UTC")
        return ZoneInfo("UTC")


def _parse_hhmm(value: str, fallback: dt_time) -> dt_time:
    """Parse an ``HH:MM`` string into a ``time``; return ``fallback`` on bad input."""
    try:
        parts = str(value).strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"out of range: {value!r}")
        return dt_time(hour, minute)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Invalid daypart time {value!r}; using fallback {fallback}: {e}")
        return fallback


def _now_local(_now: datetime | None = None) -> datetime:
    """Return the current local wall-clock time in the resolved timezone.

    ``_now`` (test seam) may be naive (treated as already-local) or aware
    (converted into the resolved tz).
    """
    tz = _resolve_tz()
    if _now is not None:
        if _now.tzinfo is None:
            return _now
        return _now.astimezone(tz)
    return datetime.now(tz)


def get_current_daypart(_now: datetime | None = None) -> Daypart:
    """Compute the current daypart from the configured clock windows.

    - night: if ``night_start > night_end`` (the usual 22:00→07:00 wrap),
      night = ``t >= night_start or t < night_end``; otherwise (no wrap)
      night = ``night_start <= t < night_end``.
    - evening: ``evening_start <= t < night_start``.
    - day: everything else.
    """
    night_start = _parse_hhmm(settings.daypart_night_start, _DEFAULT_NIGHT_START)
    night_end = _parse_hhmm(settings.daypart_night_end, _DEFAULT_NIGHT_END)
    evening_start = _parse_hhmm(settings.daypart_evening_start, _DEFAULT_EVENING_START)

    t = _now_local(_now).time()

    if night_start > night_end:
        is_night = t >= night_start or t < night_end
    else:
        is_night = night_start <= t < night_end
    if is_night:
        return "night"

    # Evening runs from evening_start until night begins. Like night, it can wrap
    # midnight — e.g. an early-morning night window (night_start < evening_start)
    # means evening spans evening_start → midnight → night_start.
    if evening_start <= night_start:
        is_evening = evening_start <= t < night_start
    else:
        is_evening = t >= evening_start or t < night_start
    if is_evening:
        return "evening"

    return "day"


def is_night(_now: datetime | None = None) -> bool:
    """True if the current daypart is night."""
    return get_current_daypart(_now) == "night"


def get_daypart_info(_now: datetime | None = None) -> dict:
    """Return a small dict for the ``daypart_changed`` hook payload.

    Keys: ``daypart`` (day/evening/night), ``local_time`` (HH:MM), ``local_date``
    (YYYY-MM-DD), all in the resolved local timezone.
    """
    now = _now_local(_now)
    return {
        "daypart": get_current_daypart(_now),
        "local_time": now.strftime("%H:%M"),
        "local_date": now.strftime("%Y-%m-%d"),
    }


def build_time_context(lang: str = "de") -> str:
    """Build a short prompt string describing the current time-of-day.

    Example (de): ``"ZEITKONTEXT: Aktuelle Zeit: 22:14 Uhr (Nacht, Donnerstag)"``
    Example (en): ``"TIME CONTEXT: Current time: 22:14 (Night, Thursday)"``

    Wraps everything in try/except and returns "" on ANY error so the agent
    path can never be broken by this helper.
    """
    try:
        lang_key = "en" if str(lang).lower().startswith("en") else "de"
        now = _now_local()
        daypart = get_current_daypart()
        label = _DAYPART_LABELS[lang_key][daypart]
        weekday = _WEEKDAYS[lang_key][now.weekday()]
        hhmm = now.strftime("%H:%M")
        if lang_key == "en":
            return f"TIME CONTEXT: Current time: {hhmm} ({label}, {weekday})"
        return f"ZEITKONTEXT: Aktuelle Zeit: {hhmm} Uhr ({label}, {weekday})"
    except Exception as e:  # noqa: BLE001 — must never break the agent
        logger.warning(f"build_time_context failed, returning empty: {e}")
        return ""
