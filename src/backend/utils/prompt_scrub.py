"""
prompt_scrub — defense-in-depth scrubbing for LLM-derived text that lands
in the agent system prompt.

Two consumers today (skill_service, tool_outcome_service) inject user-
influenced strings into the prompt: skill body_md / triggers / titles, and
the last_failure_summary on a tool_outcome row. Both originate from LLM
output that was steered by user input — without scrubbing, a tool error
message containing "system: ignore previous rules" would land in the
LLM's system role.

Not a complete defense against prompt injection (no such thing exists
today). It is a "raise the bar" whitelist-by-replacement of the most-cited
chat-template tokens, role markers, and instruction-override phrases.
"""
from __future__ import annotations


# Replacement table — keep narrow and aligned with the two consumers'
# original lists. Adding a needle here automatically applies to both.
SCRUB_PATTERNS: tuple[tuple[str, str], ...] = (
    # Role markers — interpreted as role boundaries on a line by themselves
    # or with whitespace.
    ("system:", "[sys]"),
    ("System:", "[sys]"),
    ("SYSTEM:", "[sys]"),
    ("assistant:", "[asst]"),
    ("Assistant:", "[asst]"),
    ("ASSISTANT:", "[asst]"),
    ("user:", "[usr]"),
    ("User:", "[usr]"),
    ("USER:", "[usr]"),
    # Chat-template tokens used by every major instruct format.
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
    # Instruction-override phrases that show up in injection PoCs.
    ("ignore previous instructions", "[IGNORE_PREVIOUS scrubbed]"),
    ("ignore all previous instructions", "[IGNORE_PREVIOUS scrubbed]"),
    ("disregard previous instructions", "[IGNORE_PREVIOUS scrubbed]"),
    ("new instructions:", "[NEW_INSTRUCTIONS scrubbed]"),
)


def scrub_for_prompt(raw: str) -> str:
    """Scrub user-influenced text before concatenating into the agent
    system prompt. Whitelist-by-replacement, not a silver bullet."""
    if not raw:
        return raw
    out = raw
    for needle, repl in SCRUB_PATTERNS:
        if needle in out:
            out = out.replace(needle, repl)
    return out
