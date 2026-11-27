"""Who is calling? Resolve a caller number for ONE recipient (#1240 follow-up).

Design: ``docs/design/caller-lookup.md``. Dark behind ``CALLER_LOOKUP_ENABLED``.

- The name comes from the KG contact points through ``ContactPointService.lookup_number``:
  circle-filtered for the RECIPIENT (value AND entity), live values only, a shared
  document number names nobody (``DOCUMENT_AMBIGUITY_SQL``).
- CallerID is spoofable: a name is a HINT, never an identity. The caller shows it
  next to the raw number and marks it "nicht geprüft"; it never goes into TTS or
  an LLM prompt (contact names are untrusted text).
- Fax numbers never answer for a caller.
- Ranking: manual > vCard > chat > document; at the top rank, two different
  entities read "A oder B", more add "und weitere".
- A miss falls back to an OFFLINE hint from ``phonenumbers`` (the town of a
  landline, "Mobilfunk") -- nothing leaves the instance.
- Never raises: any failure or a timeout yields ``None`` and the plain number.
"""
from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass

from loguru import logger

from utils.config import settings
from utils.log_safety import error_text_without_content

SOURCE_RANK = {"manual": 0, "vcard": 1, "chat": 2, "document": 3}
MAX_NAMES = 2
MAX_NAME_LENGTH = 80
# Unicode categories removed from an untrusted name: controls (C0/C1), format
# characters (bidi overrides/isolates, zero-width), line/paragraph separators. A
# U+202E in a document-extracted name would otherwise reorder the number and the
# "nicht geprüft" caveat printed after it.
_STRIP_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}


@dataclass(frozen=True)
class CallerHint:
    names: tuple[str, ...] = ()      # 1..MAX_NAMES entity names; empty = no hit
    more: bool = False               # further entities at the top rank
    source: str | None = None        # source_type of the top rank
    place: str | None = None         # offline hint on a miss

    def label(self) -> str | None:
        if self.names:
            text = " oder ".join(self.names)
            return f"{text} und weitere" if self.more else text
        return None


def _clean(name: str, limit: int = MAX_NAME_LENGTH) -> str:
    """Untrusted text (KG name, voicemail transcript): one line, bounded."""
    text = "".join(
        " " if unicodedata.category(ch) in _STRIP_CATEGORIES else ch for ch in str(name or "")
    )
    return " ".join(text.split())[:limit]


def offline_place(raw: str) -> str | None:
    """'Mönchengladbach' for a landline, 'Mobilfunk' for a mobile, else None (offline)."""
    import phonenumbers
    from phonenumbers import PhoneNumberType, geocoder

    try:
        parsed = phonenumbers.parse(raw, settings.contact_points_default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    if phonenumbers.number_type(parsed) == PhoneNumberType.MOBILE:
        return "Mobilfunk"
    place = geocoder.description_for_number(parsed, "de")
    return _clean(place) or None


async def _place(raw: str) -> str | None:
    # The geocoder loads its data on first use (synchronous): off the event loop,
    # so the timeout still holds and the gateway client keeps running.
    return await asyncio.to_thread(offline_place, raw)


async def _lookup(raw: str, recipient_id: int | None) -> CallerHint | None:
    from services.contact_points import ContactPointService, normalize_phone
    from services.database import AsyncSessionLocal

    if normalize_phone(raw) is None:
        return None  # withheld / not a number: nothing to say
    async with AsyncSessionLocal() as db:
        # Generous limit: lookup_number orders by name, the ranking happens here.
        hits = await ContactPointService(db).lookup_number(raw, recipient_id, limit=100)
    hits = [h for h in hits if h.point.kind != "fax"]
    if not hits:
        return CallerHint(place=await _place(raw))
    top = min(SOURCE_RANK.get(h.point.source_type, len(SOURCE_RANK)) for h in hits)
    names: list[str] = []
    source = None
    for h in hits:
        if SOURCE_RANK.get(h.point.source_type, len(SOURCE_RANK)) != top:
            continue
        source = source or h.point.source_type
        name = _clean(h.entity_name)
        if name and name not in names:
            names.append(name)
    if not names:
        return CallerHint(place=await _place(raw))
    return CallerHint(names=tuple(names[:MAX_NAMES]), more=len(names) > MAX_NAMES, source=source)


async def resolve_caller(raw: str | None, recipient_id: int | None) -> CallerHint | None:
    """The hint for ``recipient_id`` or None (flag off, no number, failure, timeout)."""
    if not (settings.caller_lookup_enabled and settings.contact_points_enabled) or not raw:
        return None
    try:
        return await asyncio.wait_for(_lookup(raw, recipient_id), settings.caller_lookup_timeout_s)
    except TimeoutError:
        logger.info("Caller lookup timed out; announcing the plain number")
        return None
    except Exception as e:
        logger.warning(f"Caller lookup failed: {error_text_without_content(e)}")
        return None
