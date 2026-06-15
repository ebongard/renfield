"""
Follow-up suggestion chips — platform service.

After an assistant answer, propose 2-4 short follow-up questions the user might
ask next, shown as tappable chips under the turn (chat-ui roadmap item 2).

Design (decided in /plan-eng-review): a SINGLE small best-effort LLM call in the
shared chat-handler seam — NOT a same-generation trailing block. Rationale: a
dedicated call's failure is harmless (no chips), whereas a trailing JSON block
rides on the agent's critical output and a bad parse could corrupt the answer.
The codebase already distrusts local-model structured output (radio/KG guards);
here that distrust is absorbed by returning `[]` on ANY failure.

Gated by `followup_chips_enabled` (dark by default). Bounded by the caller via
`asyncio.wait_for` so it can never block the turn's `done` frame.
"""

from __future__ import annotations

import json

from loguru import logger


# Keep chips short + scannable; drop anything that's really a sentence/paragraph.
_MAX_CHIP_CHARS = 80


def _parse_followups(raw: str, count: int) -> list[str]:
    """Tolerantly parse the model output into ≤count clean chip strings.

    Accepts a JSON array of strings (the requested format) OR, as a fallback,
    newline/bullet/numbered lines. Trims, de-dupes (case-insensitive), drops
    empties and overlong entries. Never raises.
    """
    if not raw or not raw.strip():
        return []

    candidates: list[str] = []

    # Preferred: a JSON array (possibly wrapped in prose / a ```json fence).
    text = raw.strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            arr = json.loads(text[start : end + 1])
            if isinstance(arr, list):
                # Keep only string items — a malformed array of objects/lists
                # ([{"q":"…"}], [["a"]]) must not render Python-repr garbage as
                # tappable chips.
                candidates = [x for x in arr if isinstance(x, str)]
        except (ValueError, TypeError):
            candidates = []

    # Fallback: line-based (strip bullets / numbering / quotes).
    if not candidates:
        for line in text.splitlines():
            s = line.strip().lstrip("-*•0123456789.)• ").strip().strip('"').strip()
            if s:
                candidates.append(s)

    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        s = c.strip().strip('"').strip()
        if not s or len(s) > _MAX_CHIP_CHARS:
            continue
        # Drop pure-punctuation noise ("{}", "[]", "---") — a real chip has words.
        if not any(ch.isalnum() for ch in s):
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= count:
            break
    return out


async def generate_followups(
    user_message: str,
    answer: str,
    lang: str = "de",
    *,
    model: str,
    count: int = 3,
) -> list[str]:
    """Best-effort: propose up to `count` short follow-up questions.

    Returns `[]` on ANY failure (model error, bad output, empty) — the caller
    treats no-chips as the normal degraded state. The caller is responsible for
    the timeout (`asyncio.wait_for`); this function does not block on its own.
    """
    user_message = (user_message or "").strip()
    answer = (answer or "").strip()
    if not answer:
        return []

    try:
        from utils.llm_client import extract_response_content, get_default_client

        lang_name = "Deutsch" if (lang or "de").startswith("de") else "English"
        system = (
            f"You suggest follow-up questions for a chat assistant. Given the user's "
            f"message and the assistant's answer, propose up to {count} SHORT, distinct "
            f"questions the user would plausibly ask NEXT. Write them in {lang_name}, "
            f"phrased as the user (first person), each under {_MAX_CHIP_CHARS} characters. "
            f'Return ONLY a JSON array of strings, e.g. ["...", "..."]. No prose, no keys.'
        )
        prompt = (
            f"User message:\n{user_message[:1000]}\n\n"
            f"Assistant answer:\n{answer[:2000]}\n\n"
            f"Up to {count} follow-up questions as a JSON array:"
        )

        client = get_default_client()
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            format="json",
            options={"temperature": 0.4},
        )
        raw = extract_response_content(response) or ""
        return _parse_followups(raw, count)
    except Exception as e:  # noqa: BLE001 — best-effort; no chips on any failure
        logger.debug(f"follow-up chip generation skipped: {e}")
        return []
