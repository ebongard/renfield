"""
SkillExtractor — LLM-driven extraction of procedural skills from an
agent-turn trace.

Triggered from the agent loop's post-turn background task. Input is the
``AgentContext.steps`` list at end-of-turn; output is either a
``SkillDraft`` ready to persist, or ``None`` if the turn didn't produce
something worth remembering.

Heuristics applied BEFORE the LLM is called (cheap filters, save tokens):

  - need ≥ ``settings.skill_extract_min_tool_calls`` SUCCESSFUL tool calls
  - need a non-empty final_answer (otherwise the turn errored out)
  - skip if the turn was a single-shot search (no actuation, no chaining)
  - skip if all tool calls were the same tool (no procedure, just retries)

Only if all of those pass do we send the trace to the LLM and ask:
"Is there a generalizable procedure here? If yes, give us {title,
trigger_examples, body_md, tool_sequence}; if no, return null."

The extractor does NOT write to the DB — it returns a draft, and the
caller (the post-turn background task in agent_service) decides whether
to persist via SkillService.create_auto_extracted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from utils.config import settings
from utils.llm_client import get_default_client


@dataclass
class SkillDraft:
    """LLM-emitted skill ready to persist."""
    title: str
    body_md: str
    trigger_examples: list[str]
    tool_sequence: list[str]


# Length caps for fields that ride into the system prompt. Mirrors the
# Pydantic limits in api/routes/skills.py so a draft that bypasses the
# route boundary (post-turn auto-extract path) still respects the same
# prompt-bloat ceiling. Tool-name pattern matches the live tool registry
# namespace (`mcp.<server>.<tool>` or `internal.<tool>`) so a poisoned
# extractor response containing role-marker text in a fake tool name is
# dropped at parse time rather than persisted and later scrubbed.
_DRAFT_TITLE_MAX_CHARS = 255
_DRAFT_BODY_MD_MAX_CHARS = 8000
_DRAFT_TRIGGER_MAX_CHARS = 200
_DRAFT_TRIGGER_CAP = 5
_DRAFT_TOOL_CAP = 10
_TOOL_NAME_RE = re.compile(r"^(mcp\.[a-z0-9_]+|internal)\.[a-z0-9_]+$")

# Per-step caps for the LLM-facing trace string. Keep this reviewable as
# a single budget — the prompt that gets sent to the extractor is bounded
# by the SUM of these caps × #steps. If we ever bump num_predict on the
# extractor LLM call, this is the table to revisit.
_TRACE_CAPS = {
    "params": 200,
    "tool_result": 120,
    "error": 120,
    "final_answer": 200,
}


_SYSTEM_DE = """Du bist ein Skill-Extractor fuer einen lernenden Agenten.
Eingabe: die User-Anfrage und die Tool-Trace einer erfolgreichen Agent-Antwort.
Aufgabe: pruefe ob die Antwort einer wiederverwendbaren PROZEDUR folgt (eine
verallgemeinerbare Schritt-fuer-Schritt-Loesung fuer eine KLASSE aehnlicher
Anfragen, nicht nur diese eine Anfrage).

Wenn JA: emittiere ein JSON-Objekt mit den Feldern:
  title:              Kurzer Name der Prozedur (max 80 Zeichen, z.B. "Album auf DLNA-Renderer abspielen")
  trigger_examples:   Array von 2-4 typischen Nutzer-Formulierungen, die diese Prozedur ausloesen sollten
  tool_sequence:      Array der Tool-Namen in der Reihenfolge der Ausfuehrung
  body_md:            Markdown-Schritt-fuer-Schritt-Anleitung (3-8 Zeilen, jeder Schritt mit "- ")

Wenn NEIN (zu spezifisch, einzelner Tool-Call, oder keine generalisierbare Struktur):
emittiere genau das Token: null

Antworte AUSSCHLIESSLICH mit dem JSON-Objekt oder dem null-Token, kein
weiterer Text."""

_SYSTEM_EN = """You are a skill extractor for a learning agent.
Input: the user's request and the tool trace of a successful agent answer.
Task: decide whether the answer follows a reusable PROCEDURE (a generalizable
step-by-step solution for a CLASS of similar requests, not just this one).

If YES: emit a JSON object with these fields:
  title:              Short procedure name (max 80 chars, e.g. "Play album on DLNA renderer")
  trigger_examples:   Array of 2-4 typical user phrasings that should trigger this procedure
  tool_sequence:      Array of tool names in execution order
  body_md:            Markdown step-by-step (3-8 lines, each starting with "- ")

If NO (too specific, single tool call, or no generalizable structure): emit
exactly the token: null

Respond with ONLY the JSON object or the null token, nothing else."""


class SkillExtractor:
    """Decide whether an agent turn produced a reusable procedure."""

    def __init__(self):
        self._client = None

    async def _get_client(self):
        if self._client is None:
            self._client = get_default_client()
        return self._client

    # ----------------------------------------------------- cheap pre-filter
    @staticmethod
    def _is_extractable(steps: list, *, min_tool_calls: int) -> tuple[bool, str]:
        """Return (extractable, reason). Reason is for log + tests."""
        tool_calls = [s for s in steps if s.step_type == "tool_call"]
        tool_results = [s for s in steps if s.step_type == "tool_result"]
        final = next(
            (s for s in steps if s.step_type == "final_answer"),
            None,
        )

        if final is None or not (final.content or "").strip():
            return False, "no_final_answer"

        successful_results = [r for r in tool_results if r.success]
        if len(successful_results) < min_tool_calls:
            return False, f"too_few_successful_tools ({len(successful_results)}<{min_tool_calls})"

        # All same tool? Not a procedure, just retries.
        tools_used = {c.tool for c in tool_calls if c.tool}
        if len(tools_used) < 2:
            return False, "single_tool_only"

        return True, "ok"

    # --------------------------------------------------------------- main
    async def extract(
        self,
        *,
        user_message: str,
        steps: list,
        lang: str = "de",
    ) -> SkillDraft | None:
        """Return a SkillDraft if the turn produced a reusable procedure."""
        min_calls = settings.skill_extract_min_tool_calls
        ok, reason = self._is_extractable(steps, min_tool_calls=min_calls)
        if not ok:
            logger.debug(f"🧠 Skill extraction skipped: {reason}")
            return None

        trace = self._build_trace(steps)
        system = _SYSTEM_EN if lang == "en" else _SYSTEM_DE
        prompt = (
            f"User-Anfrage:\n{user_message}\n\nTool-Trace:\n{trace}"
            if lang != "en"
            else f"User request:\n{user_message}\n\nTool trace:\n{trace}"
        )

        model = settings.skill_extract_model or settings.ollama_chat_model
        try:
            client = await self._get_client()
            response = await client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                # Skill extraction is offline-style: no streaming, no tool use,
                # short response. Keep token budget tight.
                options={"temperature": 0.2, "num_predict": 600},
            )
        except Exception as e:
            logger.warning(f"⚠️ Skill extractor LLM call failed: {e}")
            return None

        content = self._extract_message_content(response)
        if not content:
            return None

        return self._parse_response(content)

    # ---------------------------------------------------------- internals
    @staticmethod
    def _build_trace(steps: list) -> str:
        """Compact rendering of the turn trace for the LLM prompt."""
        lines: list[str] = []
        for s in steps:
            if s.step_type == "tool_call":
                params = s.parameters or {}
                # Truncate to keep prompt small — the LLM doesn't need full params.
                params_str = json.dumps(params, ensure_ascii=False)[:_TRACE_CAPS["params"]]
                lines.append(f"- CALL {s.tool}({params_str})")
            elif s.step_type == "tool_result":
                outcome = "ok" if s.success else "fail"
                content = (s.content or "")[:_TRACE_CAPS["tool_result"]].replace("\n", " ")
                lines.append(f"  -> {outcome}: {content}")
            elif s.step_type == "final_answer":
                lines.append(f"- ANSWER: {(s.content or '')[:_TRACE_CAPS['final_answer']]}")
            elif s.step_type == "error":
                lines.append(f"- ERROR: {(s.content or '')[:_TRACE_CAPS['error']]}")
        return "\n".join(lines)

    @staticmethod
    def _extract_message_content(response: Any) -> str | None:
        """Defensive: ollama and our fallback wrapper expose the message
        body slightly differently. Try the common shapes."""
        if response is None:
            return None
        msg = getattr(response, "message", None)
        if msg is not None:
            content = getattr(msg, "content", None) or (
                msg.get("content") if isinstance(msg, dict) else None
            )
            if content:
                return content.strip()
        if isinstance(response, dict):
            m = response.get("message")
            if isinstance(m, dict):
                return (m.get("content") or "").strip()
            if "content" in response:
                return (response["content"] or "").strip()
        return None

    # Code-fence pattern: ```optional-label\n<body>\n``` (DOTALL on body).
    # Anchored at both ends with optional trailing whitespace. Critically,
    # this is a non-greedy capture of the BODY — a character-class
    # ``strip('`')`` would eat backticks inside the body too (which
    # ruins responses where body_md cites a tool with backticks like
    # ``"call `mcp.ha.turn_on`"``).
    _FENCE_RE = re.compile(
        r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$",
        re.DOTALL,
    )

    @staticmethod
    def _parse_response(content: str) -> SkillDraft | None:
        """Tolerant JSON-or-null parser. Accepts code fences."""
        text = content.strip()
        m = SkillExtractor._FENCE_RE.match(text)
        if m is not None:
            text = m.group(1).strip()
        if text.lower() in ("null", "none", ""):
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ Skill extractor JSON parse failed: {e}; got: {text[:120]}")
            return None

        if obj is None or not isinstance(obj, dict):
            return None

        title = (obj.get("title") or "").strip()
        body_md = (obj.get("body_md") or "").strip()
        triggers = obj.get("trigger_examples") or []
        tools = obj.get("tool_sequence") or []

        if not (title and body_md and isinstance(triggers, list) and isinstance(tools, list)):
            logger.debug(f"🧠 Skill extractor: incomplete draft, skipping: {obj}")
            return None

        # Coerce strings; cap counts AND individual string lengths so a
        # misbehaving LLM emitting a multi-KB "trigger" doesn't bloat the
        # row + future prompt-build. Drop tool names that don't match the
        # live tool registry namespace pattern — that's where a poisoned
        # extractor response (LLM steered into emitting a "tool name"
        # carrying role-marker text) gets caught before persistence.
        triggers = [
            str(t).strip()[:_DRAFT_TRIGGER_MAX_CHARS]
            for t in triggers if t
        ][:_DRAFT_TRIGGER_CAP]
        tools = [
            str(t).strip()
            for t in tools if t
        ][:_DRAFT_TOOL_CAP]
        tools = [t for t in tools if _TOOL_NAME_RE.match(t)]
        if not triggers or not tools:
            return None

        return SkillDraft(
            title=title[:_DRAFT_TITLE_MAX_CHARS],
            body_md=body_md[:_DRAFT_BODY_MD_MAX_CHARS],
            trigger_examples=triggers,
            tool_sequence=tools,
        )
