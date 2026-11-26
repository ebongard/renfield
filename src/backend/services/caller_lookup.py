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
import re
from dataclasses import dataclass

from loguru import logger

from utils.config import settings
from utils.log_safety import error_text_without_content

SOURCE_RANK = {"manual": 0, "vcard": 1, "chat": 2, "document": 3}
MAX_NAMES = 2
MAX_NAME_LENGTH = 80
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


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


def _clean(name: str) -> str:
    """Untrusted text from the KG: one line, bounded."""
    return _CONTROL_RE.sub(" ", str(name or "")).strip()[:MAX_NAME_LENGTH]


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


async def _lookup(raw: str, recipient_id: int | None) -> CallerHint | None:
    from services.contact_points import ContactPointService, normalize_phone
    from services.database import AsyncSessionLocal

    if normalize_phone(raw) is None:
        return None  # withheld / not a number: nothing to say
    async with AsyncSessionLocal() as db:
        hits = await ContactPointService(db).lookup_number(raw, recipient_id, limit=20)
    hits = [h for h in hits if h.point.kind != "fax"]
    if not hits:
        return CallerHint(place=offline_place(raw))
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
        return CallerHint(place=offline_place(raw))
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
