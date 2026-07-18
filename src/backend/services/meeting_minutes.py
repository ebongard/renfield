"""Meeting minutes extraction (§2 Phase 3).

From a completed meeting's speaker-attributed segments, one LLM pass produces a
DRAFT minutes object — ``{summary, decisions[], action_items[]}`` — which the
owner reviews/edits/confirms before it is finalized (never auto-committed).

Mirrors ``schicht_a_extractor`` (thinking-model-aware chat kwargs, strict-JSON
response, best-effort — a malformed response yields an empty draft rather than
raising). Action-items are meeting-scoped and human-confirmed; they deliberately
do NOT feed the obligation notifier (D14 — no phantom obligations from small
talk). See docs/design/meeting-minutes.md.
"""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import (
    extract_response_content,
    get_classification_chat_kwargs,
    get_default_client,
)

# Shape caps (DoS / runaway-generation guard, mirrors artifact_service).
_MAX_SUMMARY = 4000
_MAX_ITEM_TEXT = 1000
_MAX_NAME = 200
_MAX_DECISIONS = 100
_MAX_ACTION_ITEMS = 200
# Cap the transcript fed to the model (very long meetings) — head is where the
# agenda/decisions usually land; keep generous.
_MAX_TRANSCRIPT_CHARS = 24000


def _parse_llm_json(raw: str) -> dict | None:
    """Best-effort JSON out of an LLM reply (tolerates ```json fences / prose)."""
    if not raw:
        return None
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fence.group(1) if fence else raw
    m = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _clean(value: Any, cap: int) -> str:
    return (str(value).strip())[:cap] if value is not None else ""


def _normalize_minutes(payload: dict) -> dict:
    """Coerce a raw LLM payload into the validated minutes shape, dropping junk.
    Always returns the full shape (empty lists rather than missing keys)."""
    summary = _clean(payload.get("summary"), _MAX_SUMMARY)

    decisions: list[dict] = []
    for d in (payload.get("decisions") or [])[:_MAX_DECISIONS]:
        if isinstance(d, str):
            text = _clean(d, _MAX_ITEM_TEXT)
            made_by = ""
        elif isinstance(d, dict):
            text = _clean(d.get("text"), _MAX_ITEM_TEXT)
            made_by = _clean(d.get("made_by"), _MAX_NAME)
        else:
            continue
        if text:
            decisions.append({"text": text, "made_by": made_by})

    action_items: list[dict] = []
    for a in (payload.get("action_items") or [])[:_MAX_ACTION_ITEMS]:
        if isinstance(a, str):
            text, owner, due = _clean(a, _MAX_ITEM_TEXT), "", ""
        elif isinstance(a, dict):
            text = _clean(a.get("text"), _MAX_ITEM_TEXT)
            owner = _clean(a.get("owner"), _MAX_NAME)
            due = _clean(a.get("due_hint"), _MAX_NAME)
        else:
            continue
        if text:
            action_items.append({"text": text, "owner": owner, "due_hint": due})

    return {"summary": summary, "decisions": decisions, "action_items": action_items}


def empty_minutes() -> dict:
    return {"summary": "", "decisions": [], "action_items": []}


def _segments_to_text(segments: list[dict]) -> str:
    lines: list[str] = []
    for seg in segments or []:
        speaker = seg.get("speaker", "Sprecher ?")
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)[:_MAX_TRANSCRIPT_CHARS]


class MinutesExtractor:
    """One per request; ``llm_client`` injectable for tests."""

    def __init__(self, llm_client: Any = None):
        self._llm_client = llm_client

    async def extract(self, segments: list[dict], *, lang: str = "de") -> dict:
        """Return validated minutes ``{summary, decisions[], action_items[]}``.
        Never raises — a missing model / malformed response yields empty minutes."""
        transcript = _segments_to_text(segments)
        if not transcript.strip():
            return empty_minutes()

        model = settings.ollama_chat_model or settings.ollama_model
        if not model:
            logger.warning("meeting minutes: no chat model configured")
            return empty_minutes()

        system = prompt_manager.get(
            "meeting_minutes", "system",
            default=(
                "Du erstellst ein Besprechungsprotokoll aus einem Transkript. "
                "Antworte AUSSCHLIESSLICH als JSON: "
                '{"summary": "...", "decisions": [{"text": "...", "made_by": "..."}], '
                '"action_items": [{"text": "...", "owner": "...", "due_hint": "..."}]}. '
                "made_by/owner/due_hint nur wenn im Transkript genannt, sonst leer."
            ),
            lang=lang,
        )
        user = prompt_manager.get(
            "meeting_minutes", "user", default="Transkript:\n{transcript}",
            lang=lang, transcript=transcript,
        )

        try:
            client = self._llm_client or get_default_client()
            response = await client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                options={"temperature": 0.2},
                **get_classification_chat_kwargs(model),
            )
            payload = _parse_llm_json(extract_response_content(response) or "")
            if payload is None:
                raise ValueError("unparseable minutes response")
            return _normalize_minutes(payload)
        except Exception as e:  # noqa: BLE001 — best-effort; never break the request
            logger.warning(f"meeting minutes extraction failed: {e}")
            return empty_minutes()


def render_minutes_markdown(minutes: dict, *, lang: str = "de") -> str:
    """Render confirmed minutes as a markdown section appended to the transcript
    document. Returns "" for empty minutes (nothing to append)."""
    minutes = minutes or {}
    summary = (minutes.get("summary") or "").strip()
    decisions = minutes.get("decisions") or []
    action_items = minutes.get("action_items") or []
    if not summary and not decisions and not action_items:
        return ""

    de = lang.startswith("de")
    h_min = "Protokoll" if de else "Minutes"
    h_sum = "Zusammenfassung" if de else "Summary"
    h_dec = "Entscheidungen" if de else "Decisions"
    h_act = "Aufgaben" if de else "Action items"

    out = [f"\n## {h_min}\n"]
    if summary:
        out.append(f"### {h_sum}\n\n{summary}\n")
    if decisions:
        out.append(f"### {h_dec}\n")
        for d in decisions:
            who = (d.get("made_by") or "").strip()
            out.append(f"- {d.get('text', '').strip()}" + (f" — _{who}_" if who else ""))
        out.append("")
    if action_items:
        out.append(f"### {h_act}\n")
        for a in action_items:
            owner = (a.get("owner") or "").strip()
            due = (a.get("due_hint") or "").strip()
            suffix = ""
            if owner:
                suffix += f" — **{owner}**"
            if due:
                suffix += f" ({due})"
            out.append(f"- [ ] {a.get('text', '').strip()}{suffix}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
