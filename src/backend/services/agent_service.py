"""
Agent Service — ReAct Agent Loop for multi-step tool chaining.

Implements a Reason + Act loop where the LLM iteratively:
1. Decides which tool to call (or produces a final answer)
2. Receives the tool result
3. Decides the next step

Uses structured JSON protocol for robustness with local models.
"""
import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from loguru import logger

from services.agent_tools import AgentToolRegistry
from services.prompt_manager import prompt_manager
from utils.circuit_breaker import agent_circuit_breaker
from utils.config import settings
from utils.llm_client import get_agent_client
from utils.token_counter import token_counter

if TYPE_CHECKING:
    from services.action_executor import ActionExecutor
    from services.agent_router import AgentRole
    from services.ollama_service import OllamaService


def _compress_history_message(content: str, max_chars: int = 500) -> str:
    """Compress a conversation history message for the agent prompt.

    Action result blocks can be very large (full document lists, base64 data).
    This extracts the essential summary and truncates the natural-language response.
    """
    if not content:
        return ""

    # Split action result prefix from the natural-language response
    # Format: "[Aktionsergebnis — ...: \n<data>]\n\n<response>"
    action_prefix = ""
    response_text = content
    if content.startswith("[Aktionsergebnis"):
        # Find the closing bracket
        bracket_end = content.find("]\n\n")
        if bracket_end > 0:
            action_block = content[1:bracket_end]  # strip [ and ]
            response_text = content[bracket_end + 3:]

            # Extract intent and first few result items from the action block
            # Typical format: "Aktionsergebnis — ...: \nmcp.paperless.search_documents → 75 Ergebnisse:\n  - id=2299, title=..."
            lines = action_block.split("\n")
            # Keep header + up to 3 result items
            compressed_lines = []
            item_count = 0
            for line in lines:
                if line.strip().startswith("- "):
                    item_count += 1
                    if item_count <= 3:
                        compressed_lines.append(line)
                    elif item_count == 4:
                        compressed_lines.append("  ... (weitere Ergebnisse gekürzt)")
                else:
                    compressed_lines.append(line)
            action_prefix = "[" + "\n".join(compressed_lines) + "] "

    # Truncate the natural-language response
    remaining = max(100, max_chars - len(action_prefix))
    if len(response_text) > remaining:
        response_text = response_text[:remaining] + "..."

    return action_prefix + response_text


@dataclass
class AgentStep:
    """Represents one step in the Agent Loop."""
    step_number: int
    step_type: str  # "thinking" | "tool_call" | "tool_result" | "final_answer" | "error"
    content: str = ""
    tool: str | None = None
    parameters: dict | None = None
    reason: str | None = None
    success: bool | None = None
    data: dict | None = None


@dataclass
class AgentContext:
    """Accumulated context for the Agent Loop."""
    original_message: str
    steps: list[AgentStep] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)

    # Maximum number of steps to include in the prompt (sliding window)
    # With 32k context window, we can keep all steps from a typical agent run
    MAX_HISTORY_STEPS: int = settings.agent_history_limit

    # Blob store for large binary data (base64) passed between tool steps.
    # Keys are "$blob:stepN_fieldname", values are the actual base64 strings.
    blob_store: dict[str, str] = field(default_factory=dict)

    # Metadata for blobs (filename, mime_type) keyed by step number.
    # E.g. {2: {"filename": "invoice.pdf", "mime_type": "application/pdf"}}
    blob_meta: dict[int, dict[str, str]] = field(default_factory=dict)

    # Token tracking
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def track_tokens(self, prompt: str, response: str = "") -> None:
        """Track token usage for monitoring and budget management."""
        self.total_input_tokens += token_counter.count(prompt)
        if response:
            self.total_output_tokens += token_counter.count(response)

    def get_token_usage(self) -> dict:
        """Get total token usage statistics."""
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
        }

    def build_history_prompt(self, lang: str = "de") -> str:
        """
        Build prompt section from accumulated step history.

        Uses a sliding window to limit context size and prevent token overflow.
        Only the last MAX_HISTORY_STEPS steps are included in the prompt.

        Args:
            lang: Language for the history prompt (de/en)
        """
        if not self.steps:
            return ""

        # Sliding window: only include last N steps
        recent_steps = self.steps[-self.MAX_HISTORY_STEPS:]

        # Language-specific labels
        if lang == "en":
            header = "PREVIOUS STEPS:"
            step_label = "Step"
            tool_called = "Tool '{tool}' called"
            result_label = "Result:"
            error_label = "Error:"
            no_result = "No result"
        else:
            header = "BISHERIGE SCHRITTE:"
            step_label = "Schritt"
            tool_called = "Tool '{tool}' aufgerufen"
            result_label = "Ergebnis:"
            error_label = "Fehler:"
            no_result = "Kein Ergebnis"

        lines = [header]
        for step in recent_steps:
            if step.step_type == "tool_call":
                tool_text = tool_called.format(tool=step.tool)
                lines.append(
                    f"  {step_label} {step.step_number}: {tool_text}"
                    f" mit {json.dumps(step.parameters, ensure_ascii=False)}"
                )
            elif step.step_type == "tool_result":
                # With 32k context window, tool results can be much more detailed
                content = step.content[:8000] if step.content else no_result
                lines.append(f"  {result_label} {content}")
            elif step.step_type == "error":
                lines.append(f"  {error_label} {step.content[:1500]}")

        return "\n".join(lines)

    def detect_infinite_loop(self, min_repetitions: int = 3) -> bool:
        """
        Detect if the agent is stuck in an infinite loop.

        Checks if the last N tool calls are identical (same tool + same parameters).

        Args:
            min_repetitions: Minimum number of identical calls to trigger detection

        Returns:
            True if infinite loop detected, False otherwise
        """
        # Get recent tool calls
        tool_calls = [
            (s.tool, json.dumps(s.parameters or {}, sort_keys=True))
            for s in self.steps
            if s.step_type == "tool_call"
        ]

        if len(tool_calls) < min_repetitions:
            return False

        # Check if last N calls are identical
        recent = tool_calls[-min_repetitions:]
        return len(set(recent)) == 1


def _truncate(text: str, max_length: int = settings.agent_response_truncation) -> str:
    """Truncate text to max_length with ellipsis."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


# Fields that contain large binary data (base64-encoded)
_BLOB_FIELDS = {"content_base64"}


def _extract_blobs(data: any, step_num, blob_store: dict[str, str], blob_meta: dict[int, dict[str, str]] | None = None) -> any:
    """
    Extract large binary fields from tool result data, store them in the
    blob store, and replace with $blob:stepN_field references.

    Handles nested JSON strings from MCP tool results (e.g.
    [{"type": "text", "text": '{"content_base64": "..."}'}]).

    Returns a modified copy of the data with blobs replaced by references.
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if key in _BLOB_FIELDS and isinstance(value, str) and len(value) > 500:
                ref = f"$blob:step{step_num}_{key}"
                blob_store[ref] = value
                size_kb = len(value) * 3 // 4 // 1024  # approx decoded size
                result[key] = ref
                result[f"_{key}_size"] = f"{size_kb}KB"
                logger.debug(f"🗄️ Stored blob {ref} ({size_kb}KB)")
                # Store metadata (filename, mime_type) for auto-attach
                if blob_meta is not None:
                    # Extract integer step from "2" or "2_0" (list index suffix)
                    if isinstance(step_num, int):
                        step_key = step_num
                    else:
                        base = str(step_num).split("_")[0]
                        step_key = int(base) if base.isdigit() else None
                    if step_key is not None:
                        meta = blob_meta.setdefault(step_key, {})
                        if "filename" in data:
                            meta["filename"] = data["filename"]
                        if "mime_type" in data:
                            meta["mime_type"] = data["mime_type"]
                        meta["blob_ref"] = ref
            elif key == "text" and isinstance(value, str) and "content_base64" in value:
                # MCP raw_data format: {"type": "text", "text": "<json_string>"}
                # Parse the nested JSON, extract blobs, re-serialize
                try:
                    parsed = json.loads(value)
                    cleaned = _extract_blobs(parsed, step_num, blob_store, blob_meta)
                    result[key] = json.dumps(cleaned, ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    result[key] = value
            elif isinstance(value, (dict, list)):
                result[key] = _extract_blobs(value, step_num, blob_store, blob_meta)
            else:
                result[key] = value
        return result
    elif isinstance(data, list):
        return [_extract_blobs(item, f"{step_num}_{i}", blob_store, blob_meta) for i, item in enumerate(data)]
    return data


def _resolve_blobs(params: any, blob_store: dict[str, str]) -> any:
    """
    Resolve $blob:stepN_field references in tool call parameters back to
    actual data from the blob store.
    """
    if isinstance(params, dict):
        return {k: _resolve_blobs(v, blob_store) for k, v in params.items()}
    elif isinstance(params, list):
        return [_resolve_blobs(item, blob_store) for item in params]
    elif isinstance(params, str) and params.startswith("$blob:") and params in blob_store:
        logger.debug(f"🗄️ Resolved blob reference {params}")
        return blob_store[params]
    return params


def _parse_agent_json(raw: str) -> dict | None:
    """
    Robustly parse JSON from LLM output.

    Handles:
    - Clean JSON (including deeply nested structures)
    - Markdown code blocks (```json ... ```)
    - JSON embedded in text
    - Common formatting issues
    """
    raw = raw.strip()
    if not raw:
        return None

    # Method 1: Direct parse (handles clean JSON of any nesting depth)
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Method 2: Markdown code block
    if "```" in raw:
        match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(1))
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

    # Method 3: Find balanced JSON object using brace counting
    start = raw.find('{')
    if start >= 0:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(raw)):
            c = raw[i]
            if escape:
                escape = False
                continue
            if c == '\\' and in_string:
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = raw[start:i + 1]
                    try:
                        result = json.loads(candidate)
                        if isinstance(result, dict):
                            return result
                    except json.JSONDecodeError:
                        break

    # Method 4: Trim after last closing brace (last resort)
    if '}' in raw:
        trimmed = raw[:raw.rfind('}') + 1]
        # Find the first opening brace
        first_brace = trimmed.find('{')
        if first_brace >= 0:
            try:
                result = json.loads(trimmed[first_brace:])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

    return None


class AgentService:
    """
    ReAct Agent Loop — iteratively calls tools until a final answer is produced.

    Usage:
        async for step in agent.run(message, ollama, executor, ...):
            # step is an AgentStep with step_type and content
            await websocket.send_json(step_to_ws_message(step))
    """

    def __init__(
        self,
        tool_registry: AgentToolRegistry,
        max_steps: int | None = None,
        step_timeout: float | None = None,
        total_timeout: float | None = None,
        role: Optional["AgentRole"] = None,
    ):
        self.tool_registry = tool_registry
        self.role = role
        # Role overrides take priority, then explicit params, then settings
        self.max_steps = max_steps or (role.max_steps if role else None) or settings.agent_max_steps
        self.step_timeout = step_timeout or settings.agent_step_timeout
        self.total_timeout = total_timeout or settings.agent_total_timeout
        self._prompt_key = (role.prompt_key if role else None) or "agent_prompt"

    async def _build_agent_prompt(
        self,
        message: str,
        context: AgentContext,
        conversation_history: list[dict] | None = None,
        room_context: dict | None = None,
        lang: str = "de",
        memory_context: str = "",
    ) -> str:
        """Build the prompt for the Agent LLM call."""
        tools_prompt = self.tool_registry.build_tools_prompt()
        history_prompt = context.build_history_prompt(lang=lang)

        # Build room context string for the prompt
        room_context_str = ""
        if room_context and room_context.get("room_name"):
            room_context_str = prompt_manager.get(
                "agent", "room_context_template", lang=lang,
                room_name=room_context["room_name"]
            )

        # With 32k context, include conversation history for follow-up references
        # like "Schick die gleiche Rechnung nochmal" or "Und wie ist es morgen?"
        conv_context = ""
        if conversation_history:
            recent = conversation_history[-settings.agent_conv_context_messages:]
            history_lines = []
            for msg in recent:
                role = "User" if msg.get("role") == "user" else "Assistant"
                content = _compress_history_message(msg.get("content", ""))
                history_lines.append(f"  {role}: {content}")
            conv_context = prompt_manager.get(
                "agent", "conv_context_template", lang=lang,
                history_lines="\n".join(history_lines)
            )

        # Determine step directive based on whether we have history
        if context.steps:
            step_directive = prompt_manager.get("agent", "step_directive_next", lang=lang)
        else:
            step_directive = prompt_manager.get("agent", "step_directive_first", lang=lang)

        # Load tool corrections from semantic feedback
        tool_corrections = ""
        try:
            from services.database import AsyncSessionLocal
            from services.intent_feedback_service import IntentFeedbackService
            async with AsyncSessionLocal() as feedback_db:
                service = IntentFeedbackService(feedback_db)
                similar = await service.find_similar_corrections(
                    message, feedback_type="agent_tool"
                )
                if similar:
                    tool_corrections = service.format_agent_corrections(similar, lang=lang)
                    logger.info(f"📝 {len(similar)} tool correction(s) injected into agent prompt")
        except Exception as e:
            logger.warning(f"⚠️ Agent tool correction lookup failed: {e}")

        # Build prompt from externalized template (role-specific or default)
        prompt = prompt_manager.get(
            "agent", self._prompt_key, lang=lang,
            message=message,
            room_context=room_context_str,
            conv_context=conv_context,
            memory_context=memory_context,
            tools_prompt=tools_prompt,
            tool_corrections=tool_corrections,
            history_prompt=history_prompt,
            step_directive=step_directive
        )

        # If role-specific prompt not found, fall back to default agent_prompt
        if not prompt and self._prompt_key != "agent_prompt":
            logger.debug(f"Prompt key '{self._prompt_key}' not found, falling back to 'agent_prompt'")
            prompt = prompt_manager.get(
                "agent", "agent_prompt", lang=lang,
                message=message,
                room_context=room_context_str,
                conv_context=conv_context,
                memory_context=memory_context,
                tools_prompt=tools_prompt,
                tool_corrections=tool_corrections,
                history_prompt=history_prompt,
                step_directive=step_directive
            )

        return prompt

    async def run(
        self,
        message: str,
        ollama: "OllamaService",
        executor: "ActionExecutor",
        conversation_history: list[dict] | None = None,
        room_context: dict | None = None,
        lang: str | None = None,
        memory_context: str = "",
    ) -> AsyncGenerator[AgentStep, None]:
        """
        Run the Agent Loop. Yields AgentStep objects for real-time feedback.

        Args:
            message: The user's original message
            ollama: OllamaService instance for LLM calls
            executor: ActionExecutor for executing tool calls
            conversation_history: Optional conversation history
            room_context: Optional room context for HA entity resolution
            lang: Language for prompts and responses (de/en). None = default_lang
            memory_context: Formatted memory section for the agent prompt
        """
        # Use ollama's default language if not specified
        lang = lang or ollama.default_lang

        context = AgentContext(original_message=message)
        start_time = time.monotonic()

        # Per-role model/URL override > global agent settings > default
        role_model = self.role.model if self.role else None
        role_url = self.role.ollama_url if self.role else None
        agent_model = role_model or settings.agent_model or settings.ollama_model

        # Use separate Ollama instance for agent if configured
        agent_client, resolved_url = get_agent_client(role_url, settings.agent_ollama_url)
        role_label = self.role.name if self.role else "default"
        logger.info(f"🤖 Agent [{role_label}] using Ollama: {resolved_url} / {agent_model}")

        # With 32k context, all tools fit in the prompt (~5000 tokens for 109 tools).
        # The LLM selects the right tool itself — eliminates keyword-filtering errors.
        total_tools = len(self.tool_registry.get_tool_names())

        # Step 0: Thinking
        yield AgentStep(
            step_number=0,
            step_type="thinking",
            content=prompt_manager.get("agent", "thinking_message", lang=lang),
        )

        # Get LLM options from config
        llm_options = prompt_manager.get_config("agent", "llm_options") or {
            "temperature": 0.1, "top_p": 0.2, "num_predict": 2048, "num_ctx": 32768
        }
        llm_options_retry = prompt_manager.get_config("agent", "llm_options_retry") or {
            "temperature": 0.3, "top_p": 0.4, "num_predict": 2048, "num_ctx": 32768
        }
        json_system_message = prompt_manager.get("agent", "json_system_message", lang=lang)

        for step_num in range(1, self.max_steps + 1):
            # Check total timeout
            elapsed = time.monotonic() - start_time
            if elapsed > self.total_timeout:
                logger.warning(f"⏰ Agent total timeout after {elapsed:.1f}s at step {step_num}")
                summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model, lang=lang, agent_client=agent_client)
                yield summary_step
                return

            # Build prompt with all available tools (32k context fits all tools)
            prompt = await self._build_agent_prompt(message, context, conversation_history, room_context=room_context, lang=lang, memory_context=memory_context)
            logger.info(f"🤖 Agent step {step_num} prompt ({len(prompt)} chars, {total_tools} tools)")

            # Check circuit breaker before LLM call
            if not await agent_circuit_breaker.allow_request():
                logger.warning(f"🔴 Agent circuit breaker OPEN — skipping LLM call at step {step_num}")
                yield AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=prompt_manager.get("agent", "error_circuit_open", lang=lang),
                )
                summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model, lang=lang, agent_client=agent_client)
                yield summary_step
                return

            # Call LLM with per-step timeout
            try:
                raw_response = await asyncio.wait_for(
                    agent_client.chat(
                        model=agent_model,
                        messages=[
                            {"role": "system", "content": json_system_message},
                            {"role": "user", "content": prompt},
                        ],
                        options=llm_options,
                    ),
                    timeout=self.step_timeout,
                )
                response_text = raw_response.message.content or ""
                await agent_circuit_breaker.record_success()

                # Track token usage
                context.track_tokens(prompt, response_text)
                logger.info(f"🤖 Agent step {step_num} LLM response ({len(response_text)} chars): {response_text[:500]}")
            except TimeoutError:
                await agent_circuit_breaker.record_failure()
                logger.warning(f"⏰ Agent step {step_num} timed out after {self.step_timeout}s")
                yield AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=prompt_manager.get("agent", "error_timeout", lang=lang, step=step_num),
                )
                summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model, lang=lang, agent_client=agent_client)
                yield summary_step
                return
            except Exception as e:
                await agent_circuit_breaker.record_failure()
                logger.error(f"❌ Agent LLM call failed at step {step_num}: {e}")
                yield AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=prompt_manager.get("agent", "error_llm_failed", lang=lang, error=str(e)),
                )
                yield self._build_fallback_answer(context, step_num, str(e), lang=lang)
                return

            # Parse JSON response
            parsed = _parse_agent_json(response_text)

            # Treat JSON without "action" field as malformed (LLM output just parameters)
            if parsed and "action" not in parsed:
                logger.info(f"🔄 Agent step {step_num}: JSON missing 'action' field, treating as empty for retry")
                parsed = None
                response_text = ""

            # Retry once on empty response with a nudge prompt
            if not parsed and not response_text.strip():
                logger.info(f"🔄 Agent step {step_num}: Empty LLM response, retrying with nudge...")
                retry_nudge = prompt_manager.get("agent", "retry_nudge", lang=lang)
                nudge = prompt + retry_nudge
                try:
                    retry_response = await asyncio.wait_for(
                        agent_client.chat(
                            model=agent_model,
                            messages=[
                                {"role": "system", "content": json_system_message},
                                {"role": "user", "content": nudge},
                            ],
                            options=llm_options_retry,
                        ),
                        timeout=self.step_timeout,
                    )
                    response_text = retry_response.message.content or ""
                    context.track_tokens(nudge, response_text)
                    logger.info(f"🔄 Agent step {step_num} retry ({len(response_text)} chars): {response_text[:200]}")
                    parsed = _parse_agent_json(response_text)
                except Exception as e:
                    logger.warning(f"🔄 Agent step {step_num} retry failed: {e}")

            # Try to recover truncated send_email calls (LLM pasted base64 content)
            if not parsed and "send_email" in response_text and context.blob_store:
                parsed = _recover_send_email(response_text, context)

            if not parsed:
                logger.warning(f"⚠️ Agent step {step_num}: JSON parse failed (len={len(response_text)}): {response_text[:500]}")
                # If we have collected tool results, summarize them via LLM
                has_results = any(s.step_type == "tool_result" and s.success for s in context.steps)
                if has_results:
                    logger.info(f"📝 Agent step {step_num}: Summarizing collected results via LLM...")
                    summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model, lang=lang, agent_client=agent_client)
                    yield summary_step
                elif response_text.strip():
                    # No tool results but LLM gave some text — use it as answer
                    # Guard: don't leak truncated/malformed JSON as user-facing text
                    raw = response_text.strip()
                    if raw.startswith("{") or raw.startswith("["):
                        logger.warning(f"⚠️ Agent step {step_num}: Suppressing malformed JSON from response")
                        yield AgentStep(
                            step_number=step_num,
                            step_type="final_answer",
                            content=prompt_manager.get("agent", "error_incomplete", lang=lang),
                            reason="Malformed JSON suppressed",
                        )
                    else:
                        reason_text = "JSON parsing failed, using raw response" if lang == "en" else "JSON-Parsing fehlgeschlagen, Rohantwort verwendet"
                        yield AgentStep(
                            step_number=step_num,
                            step_type="final_answer",
                            content=raw,
                            reason=reason_text,
                        )
                else:
                    reason_text = "Empty LLM response" if lang == "en" else "Leere LLM-Antwort"
                    yield AgentStep(
                        step_number=step_num,
                        step_type="final_answer",
                        content=prompt_manager.get("agent", "error_incomplete", lang=lang),
                        reason=reason_text,
                    )
                return

            action = parsed.get("action", "")

            # Handle final_answer
            if action == "final_answer":
                answer = parsed.get("answer", "")
                yield AgentStep(
                    step_number=step_num,
                    step_type="final_answer",
                    content=answer,
                    reason=parsed.get("reason", ""),
                )
                return

            # Validate tool name
            if not self.tool_registry.is_valid_tool(action):
                logger.warning(f"⚠️ Agent step {step_num}: Invalid tool '{action}'")
                error_content = f"Unknown tool: {action}" if lang == "en" else f"Unbekanntes Tool: {action}"
                error_step = AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=error_content,
                    tool=action,
                )
                context.steps.append(error_step)
                yield error_step
                # Continue loop — LLM will see the error in history
                continue

            parameters = parsed.get("parameters", {})
            reason = parsed.get("reason", "")

            # Auto-attach downloaded documents to email calls
            if "send_email" in action and context.blob_store:
                parameters = _auto_attach_blobs(parameters, context)

            # Resolve any $blob: references from previous tool results
            resolved_parameters = _resolve_blobs(parameters, context.blob_store)

            # Yield tool_call step (with display-safe params — no base64 content)
            display_params = {
                k: (f"[{len(v)} chars]" if isinstance(v, str) and len(v) > 200 else v)
                for k, v in parameters.items()
            } if parameters else parameters
            # Truncate attachment content in display
            if "attachments" in (display_params or {}):
                display_params["attachments"] = [
                    {k: (f"[{len(v)} chars]" if k == "content_base64" and isinstance(v, str) else v)
                     for k, v in att.items()}
                    for att in display_params.get("attachments", [])
                    if isinstance(att, dict)
                ]
            tool_call_step = AgentStep(
                step_number=step_num,
                step_type="tool_call",
                tool=action,
                parameters=display_params,
                reason=reason,
            )
            context.steps.append(tool_call_step)
            yield tool_call_step

            # Execute the tool (with resolved blob data)
            try:
                intent_data = {
                    "intent": action,
                    "parameters": resolved_parameters,
                    "confidence": 1.0,
                }
                result = await executor.execute(intent_data)
            except Exception as e:
                logger.error(f"❌ Agent tool execution failed: {action} — {e}")
                error_msg = f"Tool error: {e!s}" if lang == "en" else f"Tool-Fehler: {e!s}"
                result = {
                    "success": False,
                    "message": error_msg,
                    "action_taken": False,
                }

            logger.info(f"🤖 Agent step {step_num} tool result: success={result.get('success')}, has_data={result.get('data') is not None}, message_len={len(result.get('message', ''))}")

            # Extract large binary blobs before building LLM summary
            result_data = result.get("data")
            blob_count_before = len(context.blob_store)
            if result_data:
                result_data_for_llm = _extract_blobs(result_data, step_num, context.blob_store, context.blob_meta)
            else:
                result_data_for_llm = result_data
            blob_count_after = len(context.blob_store)
            if blob_count_after > blob_count_before:
                logger.info(f"🗄️ Step {step_num}: Extracted {blob_count_after - blob_count_before} blob(s) from data")

            # Also extract blobs from the message text (may contain JSON with content_base64)
            raw_message = result.get("message", "")
            if "content_base64" in raw_message:
                logger.info(f"🗄️ Step {step_num}: Found content_base64 in message ({len(raw_message)} chars)")
                try:
                    parsed_msg = json.loads(raw_message)
                    cleaned_msg = _extract_blobs(parsed_msg, step_num, context.blob_store, context.blob_meta)
                    result_message = json.dumps(cleaned_msg, ensure_ascii=False)
                    blob_count_after = len(context.blob_store)  # update after message extraction
                except (json.JSONDecodeError, TypeError):
                    result_message = raw_message
            else:
                result_message = raw_message

            # Build result summary for the LLM history prompt.
            # For download results with blobs: show clean metadata only (no blob refs).
            # The LLM doesn't need to know about blob mechanics — auto-attach handles it.
            no_result = "No result" if lang == "en" else "Kein Ergebnis"
            result_message = result_message or no_result

            if blob_count_after > blob_count_before:
                # This step produced blobs — show clean metadata summary
                meta = context.blob_meta.get(step_num, {})
                fname = meta.get("filename", "document.pdf")
                mtype = meta.get("mime_type", "unknown")
                attach_note = "Will be auto-attached to email." if lang == "en" else "Wird automatisch an E-Mail angehängt."
                result_summary = f"Document downloaded: {fname} ({mtype}). {attach_note}"
                logger.info(f"📎 Step {step_num} summary: {result_summary}")
            elif result_data_for_llm:
                data_label = "Data" if lang == "en" else "Daten"
                data_str = json.dumps(result_data_for_llm, ensure_ascii=False)
                result_summary = _truncate(f"{result_message} | {data_label}: {data_str}", max_length=4000)
            else:
                result_summary = _truncate(result_message, max_length=4000)

            # Yield tool_result step
            tool_result_step = AgentStep(
                step_number=step_num,
                step_type="tool_result",
                content=result_summary,
                tool=action,
                success=result.get("success", False),
                data=result.get("data"),
            )
            context.steps.append(tool_result_step)
            context.tool_results.append(result)
            yield tool_result_step

            # Check for infinite loop (same tool called repeatedly)
            if context.detect_infinite_loop(min_repetitions=3):
                logger.warning(f"🔄 Agent infinite loop detected at step {step_num} (tool: {action})")
                yield AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=prompt_manager.get("agent", "error_loop_detected", lang=lang, tool=action),
                    tool=action,
                )
                summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model, lang=lang, agent_client=agent_client)
                yield summary_step
                return

        # Max steps reached — summarize collected results via LLM
        logger.warning(f"⚠️ Agent reached max steps ({self.max_steps})")
        summary_step = await self._build_summary_answer(context, self.max_steps, message, ollama, agent_model, lang=lang, agent_client=agent_client)
        yield summary_step

    async def _build_summary_answer(
        self,
        context: AgentContext,
        step_num: int,
        original_message: str,
        ollama: "OllamaService",
        agent_model: str,
        lang: str = "de",
        agent_client=None,
    ) -> AgentStep:
        """
        Summarize collected tool results into a natural-language answer via LLM.

        Falls back to a static message if the LLM call fails.
        """
        collected = []
        for step in context.steps:
            if step.step_type == "tool_result" and step.success:
                collected.append(step.content)

        if not collected:
            reason_text = "No results collected" if lang == "en" else "Keine Ergebnisse gesammelt"
            return AgentStep(
                step_number=step_num,
                step_type="final_answer",
                content=prompt_manager.get("agent", "error_incomplete", lang=lang),
                reason=reason_text,
            )

        # Build a summary prompt from externalized template
        results_text = "\n".join(f"- {c}" for c in collected)
        summary_prompt_text = prompt_manager.get(
            "agent", "summary_prompt", lang=lang,
            message=original_message,
            results=results_text
        )

        # Get LLM options for summary
        llm_options_summary = prompt_manager.get_config("agent", "llm_options_summary") or {
            "temperature": 0.3, "num_predict": 1500, "num_ctx": 32768
        }
        summary_system = prompt_manager.get("agent", "summary_system_message", lang=lang)

        client = agent_client or ollama.client
        try:
            raw_response = await asyncio.wait_for(
                client.chat(
                    model=agent_model,
                    messages=[
                        {"role": "system", "content": summary_system},
                        {"role": "user", "content": summary_prompt_text},
                    ],
                    options=llm_options_summary,
                ),
                timeout=self.step_timeout,
            )
            summary = (raw_response.message.content or "").strip()

            # Track tokens for summary call
            context.track_tokens(summary_prompt_text, summary)

            if summary:
                logger.info(f"✅ Agent summary generated ({len(summary)} chars)")
                reason_text = "LLM summary of collected results" if lang == "en" else "LLM-Zusammenfassung der gesammelten Ergebnisse"
                return AgentStep(
                    step_number=step_num,
                    step_type="final_answer",
                    content=summary,
                    reason=reason_text,
                )
        except Exception as e:
            logger.warning(f"⚠️ Agent summary LLM call failed: {e}")

        # Fallback: static message (should rarely happen)
        reason_text = "Summary failed" if lang == "en" else "Zusammenfassung fehlgeschlagen"
        return AgentStep(
            step_number=step_num,
            step_type="final_answer",
            content=prompt_manager.get("agent", "error_summary_failed", lang=lang),
            reason=reason_text,
        )

    def _build_fallback_answer(self, context: AgentContext, step_num: int, error: str, lang: str = "de") -> AgentStep:
        """Build a fallback answer when an error occurs."""
        if lang == "en":
            content = f"Sorry, an error occurred while processing: {error}"
            reason = "Error fallback"
        else:
            content = f"Entschuldigung, bei der Bearbeitung ist ein Fehler aufgetreten: {error}"
            reason = "Fehler-Fallback"
        return AgentStep(
            step_number=step_num,
            step_type="final_answer",
            content=content,
            reason=reason,
        )


def _auto_attach_blobs(parameters: dict, context: AgentContext) -> dict:
    """
    Auto-attach downloaded documents to email parameters.

    When the LLM calls send_email, it often fails to properly reference
    blob data (3B models can't follow $blob: reference instructions reliably).
    This function automatically builds the attachments array from previously
    downloaded documents stored in the blob store.
    """
    if not context.blob_meta:
        return parameters

    # Build attachments from blob store metadata
    auto_attachments = []
    for step_key, meta in sorted(context.blob_meta.items()):
        blob_ref = meta.get("blob_ref")
        if not blob_ref or blob_ref not in context.blob_store:
            continue
        auto_attachments.append({
            "filename": meta.get("filename", f"document_{step_key}.pdf"),
            "mime_type": meta.get("mime_type", "application/pdf"),
            "content_base64": context.blob_store[blob_ref],
        })

    if not auto_attachments:
        return parameters

    # Replace whatever attachments the LLM tried to construct
    parameters = dict(parameters)  # don't mutate original
    parameters["attachments"] = auto_attachments
    logger.info(f"📎 Auto-attached {len(auto_attachments)} document(s) to email")
    return parameters


def _recover_send_email(response_text: str, context: AgentContext) -> dict | None:
    """
    Recover a truncated send_email JSON from LLM output.

    When the LLM tries to paste actual base64 content into send_email,
    the output gets truncated by num_predict. This function detects the
    pattern and reconstructs the call using blob store data.
    """
    # Check if this looks like a truncated send_email attempt
    if "send_email" not in response_text:
        return None

    # Try to extract the basic fields before the attachments array
    try:
        # Find the action and basic parameters
        action_match = re.search(r'"action"\s*:\s*"([^"]*send_email[^"]*)"', response_text)
        if not action_match:
            return None

        action = action_match.group(1)

        # Extract simple string fields
        account_match = re.search(r'"account"\s*:\s*"([^"]*)"', response_text)
        to_match = re.search(r'"to"\s*:\s*"([^"]*)"', response_text)
        subject_match = re.search(r'"subject"\s*:\s*"([^"]*)"', response_text)
        body_match = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', response_text)

        if not (to_match and subject_match):
            return None

        parameters = {
            "to": to_match.group(1),
            "subject": subject_match.group(1),
            "body": body_match.group(1) if body_match else "",
        }
        if account_match:
            parameters["account"] = account_match.group(1)

        # Attachments will be auto-attached from blob store
        reason_match = re.search(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', response_text)

        recovered = {
            "action": action,
            "parameters": parameters,
            "reason": reason_match.group(1) if reason_match else "Recovered from truncated output",
        }
        logger.info(f"🔧 Recovered truncated send_email call: to={parameters['to']}, subject={parameters['subject']}")
        return recovered

    except Exception as e:
        logger.debug(f"🔧 send_email recovery failed: {e}")
        return None


def step_to_ws_message(step: AgentStep) -> dict:
    """Convert an AgentStep to a WebSocket message dict."""
    if step.step_type == "thinking":
        return {
            "type": "agent_thinking",
            "step": step.step_number,
            "content": step.content,
        }
    elif step.step_type == "tool_call":
        return {
            "type": "agent_tool_call",
            "step": step.step_number,
            "tool": step.tool,
            "parameters": step.parameters or {},
            "reason": step.reason or "",
        }
    elif step.step_type == "tool_result":
        return {
            "type": "agent_tool_result",
            "step": step.step_number,
            "tool": step.tool,
            "success": step.success,
            "message": step.content,
            "data": step.data,
        }
    elif step.step_type == "final_answer":
        return {
            "type": "stream",
            "content": step.content,
        }
    elif step.step_type == "error":
        return {
            "type": "agent_tool_result",
            "step": step.step_number,
            "tool": step.tool,
            "success": False,
            "message": step.content,
        }
    else:
        return {
            "type": "agent_thinking",
            "step": step.step_number,
            "content": step.content,
        }
