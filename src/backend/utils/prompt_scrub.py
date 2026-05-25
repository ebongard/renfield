"""
prompt_scrub — defense-in-depth scrubbing for LLM-derived text that lands
in the agent system prompt.

Two consumers today (skill_service, tool_outcome_service) inject user-
influenced strings into the prompt: skill body_md / triggers / titles / tool
names, and the last_failure_summary on a tool_outcome row. Both originate
from LLM output that was steered by user input — without scrubbing, a tool
error message containing "system: ignore previous rules" would land in the
LLM's system role.

Not a complete defense against prompt injection (no such thing exists
today). It is a "raise the bar" replacement of the most-cited chat-template
tokens, role markers, and instruction-override phrases.

v2 hardening (this revision):
  - NFKC normalization so fullwidth / homoglyph variants (e.g. "Ｓystem:",
    Cyrillic "е" in "systеm:") fold to the ASCII forms the patterns target.
  - Zero-width character strip (U+200B-U+200F, U+FEFF) so "syste​m:" with
    an injected ZWSP no longer bypasses the literal "system:" match.
  - Case-insensitive regex for role markers and instruction-override
    phrases, with optional whitespace before the colon ("System :", "SYSTEM\\t:").
  - Compiled once at import-time so the per-call cost is one re.sub per
    pattern, not 25 substring scans.
"""
from __future__ import annotations

import re
import unicodedata


# ---------------------------------------------------------------- literal
# Chat-template tokens — these are fixed-string sequences shipped by the
# instruct templates of every major model family. Case sensitivity is
# real here (case folding "[INST]" to "[inst]" changes meaning), so they
# stay as literal-string replacements.
_LITERAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("<|im_start|>", "[<im_start>]"),
    ("<|im_end|>", "[<im_end>]"),
    ("<|system|>", "[<system>]"),
    ("<|user|>", "[<user>]"),
    ("<|assistant|>", "[<assistant>]"),
    ("<|begin_of_text|>", "[<bot>]"),
    ("<|end_of_text|>", "[<eot>]"),
    ("<|start_header_id|>", "[<hdr>]"),
    ("<|end_header_id|>", "[</hdr>]"),
    ("[INST]", "[[INST]]"),
    ("[/INST]", "[[/INST]]"),
)


# ----------------------------------------------------------------- regex
# Role markers with optional whitespace before the colon. Case-insensitive.
# The `\b` anchors prevent matching inside benign words like "subsystem:".
_ROLE_MARKER_RE = re.compile(
    r"\b(?P<role>system|assistant|user)\s*:",
    re.IGNORECASE | re.UNICODE,
)
_ROLE_REPLACEMENTS = {"system": "[sys]", "assistant": "[asst]", "user": "[usr]"}


def _replace_role(match: re.Match[str]) -> str:
    return _ROLE_REPLACEMENTS[match.group("role").lower()]


# Instruction-override phrases. Word-boundary anchored; case-insensitive;
# tolerates extra internal whitespace between "previous" / "all" / "instructions".
_OVERRIDE_RE = re.compile(
    r"\b(?:ignore|disregard)\s+(?:all\s+)?previous\s+instructions\b",
    re.IGNORECASE | re.UNICODE,
)
_OVERRIDE_REPL = "[IGNORE_PREVIOUS scrubbed]"

_NEW_INSTR_RE = re.compile(
    r"\bnew\s+instructions\s*:",
    re.IGNORECASE | re.UNICODE,
)
_NEW_INSTR_REPL = "[NEW_INSTRUCTIONS scrubbed]"


# Zero-width characters that can split a pattern across visible-identical
# bytes. Stripped before matching so "syste​m:" (U+200B between e and m)
# folds to "system:" and the role-marker regex catches it.
_ZERO_WIDTH_RE = re.compile(r"[​‌‍‎‏﻿]")


# Back-compat shim: the table-of-pairs export used by callers that
# parametrize tests over the scrub list. New code should not rely on this
# being exhaustive — the regex patterns above are the source of truth.
# Each entry here SHOULD produce a match when fed through scrub_for_prompt.
SCRUB_PATTERNS: tuple[tuple[str, str], ...] = _LITERAL_PATTERNS + (
    ("system:", "[sys]"),
    ("System:", "[sys]"),
    ("SYSTEM:", "[sys]"),
    ("assistant:", "[asst]"),
    ("Assistant:", "[asst]"),
    ("ASSISTANT:", "[asst]"),
    ("user:", "[usr]"),
    ("User:", "[usr]"),
    ("USER:", "[usr]"),
    ("ignore previous instructions", _OVERRIDE_REPL),
    ("ignore all previous instructions", _OVERRIDE_REPL),
    ("disregard previous instructions", _OVERRIDE_REPL),
    ("new instructions:", _NEW_INSTR_REPL),
)


def scrub_for_prompt(raw: str) -> str:
    """Scrub user-influenced text before concatenating into the agent
    system prompt. Whitelist-by-replacement, not a silver bullet.

    Pipeline:
      1. NFKC normalize (fullwidth → ASCII, ligatures → letters).
      2. Strip zero-width separators.
      3. Replace literal chat-template tokens (case-sensitive).
      4. Replace role markers via regex (case-insensitive + whitespace).
      5. Replace instruction-override phrases via regex.

    Empty/falsy input is passed through unchanged so a None body_md
    stays None rather than becoming "".
    """
    if not raw:
        return raw

    out = unicodedata.normalize("NFKC", raw)
    out = _ZERO_WIDTH_RE.sub("", out)

    for needle, repl in _LITERAL_PATTERNS:
        if needle in out:
            out = out.replace(needle, repl)

    out = _ROLE_MARKER_RE.sub(_replace_role, out)
    out = _OVERRIDE_RE.sub(_OVERRIDE_REPL, out)
    out = _NEW_INSTR_RE.sub(_NEW_INSTR_REPL, out)
    return out
