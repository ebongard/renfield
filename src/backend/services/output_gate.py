"""The `check_output` gate — the LAST thing that touches text before a user sees it.

`utils.hooks` documents `check_output` as a **fail-closed** gate: a plugin may
redact the synthesized answer, and a redactor that crashes must never let
unredacted text ship. Until #1269 that promise held on exactly one path — the
orchestrator branch of the chat WebSocket. Single-domain turns (the common
case) and the whole REST path never fired it, and a plugin registering the hook
got no signal that its gate was dead. This module is the shared implementation
so that "every path that sends text" is one place instead of a dozen.

Two entry points:

* :func:`apply_check_output_gate` — for text that already exists in one piece.
* :func:`stream_or_gate` — for token streams. It streams live when **no**
  handler is registered (byte-identical to the previous behaviour, so an
  instance without a gate loses nothing) and buffers only when a handler IS
  registered, because a gate that redacts after the tokens are on screen is
  not a gate.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

# A redaction that removes >95 % of the answer is far more likely a redactor
# bug than a legitimate redaction, and shipping the remnant would be a silent
# data-shaped failure. Treated as fail-closed.
_EXTREME_REDACTION_RATIO = 0.05


def _fallback_unchecked(lang: str) -> str:
    return (
        "Antwort konnte nicht vollständig geprüft werden. Bitte versuche es erneut."
        if lang.startswith("de")
        else "Response could not be fully validated. Please try again."
    )


def _fallback_withheld(lang: str) -> str:
    return (
        "[Inhalt zur Datenschutzprüfung zurückgehalten]"
        if lang.startswith("de")
        else "[content withheld for privacy review]"
    )


async def apply_check_output_gate(
    content: str,
    *,
    role_name: str | None = None,
    user_id: int | None = None,
    lang: str = "de",
) -> str:
    """Run the `check_output` handlers over *content* and return what may ship.

    Fail-closed in both directions: a crashing handler replaces the answer with
    a neutral retry message, and an implausibly extreme redaction is treated as
    a redactor bug rather than trusted. An empty *content* is returned as-is —
    there is nothing to gate and no handler should see an empty turn.
    """
    if not content:
        return content

    from utils.hooks import run_hooks_with_errors

    results, errors = await run_hooks_with_errors(
        "check_output",
        content=content,
        role=role_name,
        user_id=user_id,
    )

    if errors:
        logger.error(
            f"check_output handlers crashed ({len(errors)} failures) — refusing "
            f"to send unredacted response. Failed: "
            f"{[fn.__qualname__ for fn, _ in errors]}"
        )
        return _fallback_unchecked(lang)

    for rr in results:
        if not (isinstance(rr, str) and rr and rr != content):
            continue
        ratio = len(rr) / max(1, len(content))
        if ratio < _EXTREME_REDACTION_RATIO:
            logger.error(
                f"check_output extreme redaction: {len(content)} → {len(rr)} chars "
                f"(ratio={ratio:.3f}) — treating as fail-closed"
            )
            return _fallback_withheld(lang)
        logger.info(f"check_output redacted ({len(content)} → {len(rr)} chars)")
        return rr

    return content


async def stream_or_gate(
    chunks: AsyncIterator[str],
    websocket: Any,
    *,
    role_name: str | None = None,
    user_id: int | None = None,
    lang: str = "de",
) -> str:
    """Forward *chunks* to the client, or buffer them behind the gate.

    No `check_output` handler registered →every chunk goes out as it arrives,
    exactly as before. Handler registered → nothing is sent until the complete
    answer has passed the gate, because redacting text the user has already
    read is theatre.

    Returns the full text (post-gate), which callers persist as the assistant
    message — so history and transcript carry the redacted version too.
    """
    from utils.hooks import has_hook

    gated = has_hook("check_output")
    full = ""

    async for chunk in chunks:
        full += chunk
        if not gated:
            await websocket.send_json({"type": "stream", "content": chunk})

    if gated:
        full = await apply_check_output_gate(
            full, role_name=role_name, user_id=user_id, lang=lang
        )
        if full:
            await websocket.send_json({"type": "stream", "content": full})

    return full
