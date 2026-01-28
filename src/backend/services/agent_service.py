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
from dataclasses import dataclass, field
from typing import AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from loguru import logger

from utils.config import settings
from services.agent_tools import AgentToolRegistry

if TYPE_CHECKING:
    from services.ollama_service import OllamaService
    from services.action_executor import ActionExecutor


@dataclass
class AgentStep:
    """Represents one step in the Agent Loop."""
    step_number: int
    step_type: str  # "thinking" | "tool_call" | "tool_result" | "final_answer" | "error"
    content: str = ""
    tool: Optional[str] = None
    parameters: Optional[Dict] = None
    reason: Optional[str] = None
    success: Optional[bool] = None
    data: Optional[Dict] = None


@dataclass
class AgentContext:
    """Accumulated context for the Agent Loop."""
    original_message: str
    steps: List[AgentStep] = field(default_factory=list)
    tool_results: List[Dict] = field(default_factory=list)

    def build_history_prompt(self) -> str:
        """Build prompt section from accumulated step history."""
        if not self.steps:
            return ""

        lines = ["BISHERIGE SCHRITTE:"]
        for step in self.steps:
            if step.step_type == "tool_call":
                lines.append(
                    f"  Schritt {step.step_number}: Tool '{step.tool}' aufgerufen"
                    f" mit {json.dumps(step.parameters, ensure_ascii=False)}"
                )
            elif step.step_type == "tool_result":
                # Truncate tool results to keep context manageable
                content = step.content[:300] if step.content else "Kein Ergebnis"
                lines.append(f"  Ergebnis: {content}")
            elif step.step_type == "error":
                lines.append(f"  Fehler: {step.content[:200]}")

        return "\n".join(lines)


def _truncate(text: str, max_length: int = 300) -> str:
    """Truncate text to max_length with ellipsis."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def _parse_agent_json(raw: str) -> Optional[Dict]:
    """
    Robustly parse JSON from LLM output.

    Handles:
    - Clean JSON
    - Markdown code blocks (```json ... ```)
    - JSON embedded in text
    - Common formatting issues
    """
    raw = raw.strip()

    # Method 1: Markdown code block
    if "```" in raw:
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if match:
            raw = match.group(1)

    # Method 2: Extract first JSON object
    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
    if json_match:
        raw = json_match.group(0)

    # Method 3: Trim after last closing brace
    if '}' in raw:
        raw = raw[:raw.rfind('}') + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
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
        max_steps: Optional[int] = None,
        step_timeout: Optional[float] = None,
        total_timeout: Optional[float] = None,
    ):
        self.tool_registry = tool_registry
        self.max_steps = max_steps or settings.agent_max_steps
        self.step_timeout = step_timeout or settings.agent_step_timeout
        self.total_timeout = total_timeout or settings.agent_total_timeout

    def _build_agent_prompt(
        self,
        message: str,
        context: AgentContext,
        conversation_history: Optional[List[Dict]] = None,
    ) -> str:
        """Build the prompt for the Agent LLM call."""
        tools_prompt = self.tool_registry.build_tools_prompt()
        history_prompt = context.build_history_prompt()

        conv_context = ""
        if conversation_history:
            recent = conversation_history[-4:]
            lines = []
            for msg in recent:
                role = "Nutzer" if msg.get("role") == "user" else "Assistent"
                lines.append(f"  {role}: {msg.get('content', '')[:200]}")
            conv_context = f"\nKONVERSATIONS-KONTEXT:\n" + "\n".join(lines)

        # Determine step directive based on whether we have history
        if context.steps:
            step_directive = "Was ist der nächste Schritt?"
        else:
            step_directive = "Beginne mit dem ERSTEN Schritt:"

        return f"""Du bist ein Agent, der komplexe Aufgaben Schritt für Schritt löst.

AUFGABE: "{message}"
{conv_context}

{tools_prompt}

{history_prompt}

ANTWORT-FORMAT: Antworte mit GENAU EINEM JSON-Objekt.

Wenn du ein Tool aufrufen willst:
{{"action": "<tool_name>", "parameters": {{...}}, "reason": "Warum dieses Tool"}}

Wenn du die finale Antwort geben willst:
{{"action": "final_answer", "answer": "Deine Antwort an den Nutzer", "reason": "Warum fertig"}}

REGELN:
- Nutze NUR Tools aus der Liste oben
- Rufe pro Antwort GENAU EIN Tool auf
- Nutze die Ergebnisse vorheriger Schritte für Entscheidungen
- Gib final_answer wenn alle Informationen gesammelt sind
- Die finale Antwort muss natürlich und auf Deutsch sein
- Antworte NUR mit JSON, kein anderer Text

{step_directive}"""

    async def run(
        self,
        message: str,
        ollama: "OllamaService",
        executor: "ActionExecutor",
        conversation_history: Optional[List[Dict]] = None,
        room_context: Optional[Dict] = None,
    ) -> AsyncGenerator[AgentStep, None]:
        """
        Run the Agent Loop. Yields AgentStep objects for real-time feedback.

        Args:
            message: The user's original message
            ollama: OllamaService instance for LLM calls
            executor: ActionExecutor for executing tool calls
            conversation_history: Optional conversation history
            room_context: Optional room context for HA entity resolution
        """
        context = AgentContext(original_message=message)
        start_time = time.monotonic()
        agent_model = settings.agent_model or settings.ollama_model

        # Step 0: Thinking
        yield AgentStep(
            step_number=0,
            step_type="thinking",
            content="Analysiere Anfrage und plane Schritte...",
        )

        for step_num in range(1, self.max_steps + 1):
            # Check total timeout
            elapsed = time.monotonic() - start_time
            if elapsed > self.total_timeout:
                logger.warning(f"⏰ Agent total timeout after {elapsed:.1f}s at step {step_num}")
                summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model)
                yield summary_step
                return

            # Build prompt with accumulated context
            prompt = self._build_agent_prompt(message, context, conversation_history)
            logger.debug(f"🤖 Agent step {step_num} prompt ({len(prompt)} chars)")

            # Call LLM with per-step timeout
            try:
                raw_response = await asyncio.wait_for(
                    ollama.client.chat(
                        model=agent_model,
                        messages=[
                            {"role": "system", "content": "Antworte nur mit JSON."},
                            {"role": "user", "content": prompt},
                        ],
                        options={
                            "temperature": 0.1,
                            "top_p": 0.2,
                            "num_predict": 500,
                        },
                    ),
                    timeout=self.step_timeout,
                )
                response_text = raw_response.message.content or ""
                logger.debug(f"🤖 Agent step {step_num} response_len={len(response_text)}")
            except asyncio.TimeoutError:
                logger.warning(f"⏰ Agent step {step_num} timed out after {self.step_timeout}s")
                yield AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=f"Schritt {step_num} hat zu lange gedauert (Timeout).",
                )
                summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model)
                yield summary_step
                return
            except Exception as e:
                logger.error(f"❌ Agent LLM call failed at step {step_num}: {e}")
                yield AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=f"LLM-Fehler: {str(e)}",
                )
                yield self._build_fallback_answer(context, step_num, str(e))
                return

            # Parse JSON response
            parsed = _parse_agent_json(response_text)

            # Retry once on empty response with a nudge prompt
            if not parsed and not response_text.strip():
                logger.info(f"🔄 Agent step {step_num}: Empty LLM response, retrying with nudge...")
                nudge = prompt + "\n\nDu MUSST jetzt mit einem JSON-Objekt antworten. Was ist der nächste Schritt?"
                try:
                    retry_response = await asyncio.wait_for(
                        ollama.client.chat(
                            model=agent_model,
                            messages=[
                                {"role": "system", "content": "Antworte nur mit JSON."},
                                {"role": "user", "content": nudge},
                            ],
                            options={
                                "temperature": 0.3,
                                "top_p": 0.4,
                                "num_predict": 500,
                            },
                        ),
                        timeout=self.step_timeout,
                    )
                    response_text = retry_response.message.content or ""
                    logger.debug(f"🔄 Agent step {step_num} retry: {len(response_text)} chars")
                    parsed = _parse_agent_json(response_text)
                except Exception as e:
                    logger.warning(f"🔄 Agent step {step_num} retry failed: {e}")

            if not parsed:
                logger.warning(f"⚠️ Agent step {step_num}: JSON parse failed (len={len(response_text)})")
                # If we have collected tool results, summarize them via LLM
                has_results = any(s.step_type == "tool_result" and s.success for s in context.steps)
                if has_results:
                    logger.info(f"📝 Agent step {step_num}: Summarizing collected results via LLM...")
                    summary_step = await self._build_summary_answer(context, step_num, message, ollama, agent_model)
                    yield summary_step
                elif response_text.strip():
                    # No tool results but LLM gave some text — use it as answer
                    yield AgentStep(
                        step_number=step_num,
                        step_type="final_answer",
                        content=response_text.strip(),
                        reason="JSON-Parsing fehlgeschlagen, Rohantwort verwendet",
                    )
                else:
                    yield AgentStep(
                        step_number=step_num,
                        step_type="final_answer",
                        content="Entschuldigung, ich konnte die Anfrage nicht vollständig bearbeiten.",
                        reason="Leere LLM-Antwort",
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
                error_step = AgentStep(
                    step_number=step_num,
                    step_type="error",
                    content=f"Unbekanntes Tool: {action}",
                    tool=action,
                )
                context.steps.append(error_step)
                yield error_step
                # Continue loop — LLM will see the error in history
                continue

            parameters = parsed.get("parameters", {})
            reason = parsed.get("reason", "")

            # Yield tool_call step
            tool_call_step = AgentStep(
                step_number=step_num,
                step_type="tool_call",
                tool=action,
                parameters=parameters,
                reason=reason,
            )
            context.steps.append(tool_call_step)
            yield tool_call_step

            # Execute the tool
            try:
                intent_data = {
                    "intent": action,
                    "parameters": parameters,
                    "confidence": 1.0,
                }
                result = await executor.execute(intent_data)
            except Exception as e:
                logger.error(f"❌ Agent tool execution failed: {action} — {e}")
                result = {
                    "success": False,
                    "message": f"Tool-Fehler: {str(e)}",
                    "action_taken": False,
                }

            logger.debug(f"🤖 Agent step {step_num} tool result: success={result.get('success')}")
            # Build result summary including actual data for the LLM
            result_message = result.get("message", "Kein Ergebnis")
            result_data = result.get("data")
            if result_data:
                # Include key data so the LLM can reason about values
                data_str = json.dumps(result_data, ensure_ascii=False)
                result_summary = _truncate(f"{result_message} | Daten: {data_str}", max_length=500)
            else:
                result_summary = _truncate(result_message)

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

        # Max steps reached — summarize collected results via LLM
        logger.warning(f"⚠️ Agent reached max steps ({self.max_steps})")
        summary_step = await self._build_summary_answer(context, self.max_steps, message, ollama, agent_model)
        yield summary_step

    async def _build_summary_answer(
        self,
        context: AgentContext,
        step_num: int,
        original_message: str,
        ollama: "OllamaService",
        agent_model: str,
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
            return AgentStep(
                step_number=step_num,
                step_type="final_answer",
                content="Entschuldigung, ich konnte die Anfrage nicht vollständig bearbeiten.",
                reason="Keine Ergebnisse gesammelt",
            )

        # Build a summary prompt for the LLM
        results_text = "\n".join(f"- {c}" for c in collected)
        summary_prompt = (
            f"Der Nutzer hat gefragt: \"{original_message}\"\n\n"
            f"Folgende Tool-Ergebnisse wurden gesammelt:\n{results_text}\n\n"
            f"Fasse die Ergebnisse in einer natürlichen, hilfreichen Antwort auf Deutsch zusammen. "
            f"Nenne konkrete Zahlen, Namen und Links. Antworte direkt ohne Einleitung wie 'Hier ist'."
        )

        try:
            raw_response = await asyncio.wait_for(
                ollama.client.chat(
                    model=agent_model,
                    messages=[
                        {"role": "system", "content": "Du bist ein hilfreicher Assistent. Antworte natürlich auf Deutsch."},
                        {"role": "user", "content": summary_prompt},
                    ],
                    options={
                        "temperature": 0.3,
                        "num_predict": 800,
                    },
                ),
                timeout=self.step_timeout,
            )
            summary = (raw_response.message.content or "").strip()
            if summary:
                logger.info(f"✅ Agent summary generated ({len(summary)} chars)")
                return AgentStep(
                    step_number=step_num,
                    step_type="final_answer",
                    content=summary,
                    reason="LLM-Zusammenfassung der gesammelten Ergebnisse",
                )
        except Exception as e:
            logger.warning(f"⚠️ Agent summary LLM call failed: {e}")

        # Fallback: static message (should rarely happen)
        return AgentStep(
            step_number=step_num,
            step_type="final_answer",
            content="Entschuldigung, ich konnte die Ergebnisse nicht zusammenfassen.",
            reason="Zusammenfassung fehlgeschlagen",
        )

    def _build_fallback_answer(self, context: AgentContext, step_num: int, error: str) -> AgentStep:
        """Build a fallback answer when an error occurs."""
        return AgentStep(
            step_number=step_num,
            step_type="final_answer",
            content=f"Entschuldigung, bei der Bearbeitung ist ein Fehler aufgetreten: {error}",
            reason="Fehler-Fallback",
        )


def step_to_ws_message(step: AgentStep) -> Dict:
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
