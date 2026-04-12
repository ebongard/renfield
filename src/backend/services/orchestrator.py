"""Cross-MCP Query Orchestrator for multi-domain queries.

Detects when a user message needs data from multiple MCP servers (e.g.
Release + Jira + Confluence) and runs sub-agents in parallel with isolated
contexts, then synthesizes results into a combined answer.

Architecture:
  1. Planner LLM determines if query is multi-role
  2. Sub-agents run in parallel (asyncio.gather), each with own tool registry
  3. Synthesizer LLM combines sub-results into coherent answer
  4. Hooks allow plugins to inject pre-computed plans, extend roles, decorate
     results (contacts, provenance, cards), and validate output

Ported from Reva's Teams-only orchestrator to Renfield platform level so both
Teams and WebSocket channels get orchestration support.
"""

from __future__ import annotations

import asyncio
import json as _json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from services.prompt_manager import prompt_manager
from utils.hooks import run_hooks
from utils.llm_client import extract_response_content, get_agent_client

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from services.agent_router import AgentRole, AgentRouter


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SubAgentResult:
    """Result from a single sub-agent execution."""
    role: str
    answer: str
    tool_summaries: list[tuple[str, str]]
    tools_available: list[str]
    plugin_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestrationResult:
    """Combined result from orchestrated multi-role execution."""
    synthesis: str
    sub_results: list[SubAgentResult]
    tool_summaries: list[tuple[str, str]]
    tools_available: list[str]
    plan: list[dict]
    plugin_data: dict[str, Any] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Planner JSON recovery
# ---------------------------------------------------------------------------

def _try_parse_planner_json(raw: str) -> dict | None:
    """Parse a planner JSON response, tolerating mid-string truncation.

    The planner LLM occasionally exceeds the ``num_predict`` budget and emits
    a JSON object that breaks mid-string in the last ``steps[]`` entry.  Strict
    parsing rejects the whole document, but the leading steps are usually
    valid and useful.  This helper tries:

    1. Strict parse -- preferred path.
    2. Recovery -- find the last fully closed ``}`` inside ``steps[]``, then
       reconstruct the document with the partial trailing entry dropped.

    Returns the parsed dict, or None if recovery is impossible.
    """
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        pass

    steps_marker = raw.find('"steps"')
    if steps_marker < 0:
        return None
    list_start = raw.find("[", steps_marker)
    if list_start < 0:
        return None

    depth = 0
    last_complete = -1
    in_string = False
    escape = False
    for i in range(list_start + 1, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_complete = i
        elif ch == "]" and depth == 0:
            break

    if last_complete < 0:
        return None

    rebuilt = raw[: last_complete + 1] + "]}"
    try:
        return _json.loads(rebuilt)
    except _json.JSONDecodeError:
        return None


def _strip_llm_source_line(answer: str) -> str:
    """Strip any '_Quelle: ..._' / '_Source: ..._' line the synthesizer may have written.

    The canonical source footer is added later by the transport layer based on
    actual tool_summaries -- having the LLM also write its own (potentially
    incomplete) version causes a visible duplicate.
    """
    return re.sub(
        r"(?im)^\s*[_*]*\s*(quelle|source|sources|quellen)\s*[:：][^\n]*[_*]*\s*$",
        "",
        answer,
    ).rstrip()


# ---------------------------------------------------------------------------
# Recognition rules builder (dynamic, replaces hardcoded patterns)
# ---------------------------------------------------------------------------

def _build_recognition_rules(roles: dict[str, AgentRole], lang: str) -> str:
    """Build recognition rules for the planner prompt from entity patterns and keyword boosts.

    Entity patterns come from ``reference_resolver._compiled`` (loaded at startup
    via the ``load_entity_patterns`` hook).  Keyword boosts come from each role's
    ``keyword_boost`` list in ``agent_roles.yaml``.
    """
    lines: list[str] = []

    # Entity pattern examples (if available)
    try:
        from services.reference_resolver import _compiled
        if _compiled:
            for pattern in _compiled:
                example = getattr(pattern, "example", None) or pattern.pattern
                domain = getattr(pattern, "domain", None)
                if domain:
                    lines.append(f"   - Pattern like {example} -> single-role {domain}")
    except (ImportError, AttributeError):
        pass

    # Keyword boosts from role config
    for name, role in sorted(roles.items()):
        if not role.mcp_servers:
            continue
        keywords = getattr(role, "keyword_boost", None)
        if keywords:
            kw_str = ", ".join(f'"{k}"' for k in keywords[:6])
            lines.append(f"   - Keywords: {kw_str} -> {name}")

    return "\n".join(lines) if lines else "   (no specific recognition rules configured)"


# ---------------------------------------------------------------------------
# Orchestrator role eligibility
# ---------------------------------------------------------------------------

async def get_orchestrator_roles(router: AgentRouter) -> set[str]:
    """Return the set of role names eligible for orchestration.

    Base set: any role with ``mcp_servers`` defined.  Plugins can extend via
    the ``extend_orchestrator_roles`` hook.
    """
    roles = {name for name, r in router.roles.items() if r.mcp_servers}
    hook_results = await run_hooks("extend_orchestrator_roles")
    for hr in hook_results:
        if isinstance(hr, set):
            roles |= hr
    return roles


# ---------------------------------------------------------------------------
# Multi-role detection (Planner)
# ---------------------------------------------------------------------------

async def detect_multi_role(
    message: str,
    primary_role: AgentRole,
    roles: dict[str, AgentRole],
    req_id: str,
    lang: str = "en",
) -> list[dict] | None:
    """Determine if a query needs data from multiple MCP servers.

    Returns None for single-role queries, or a list of
    ``[{"role": "release", "query": "..."}, ...]`` for multi-role queries.
    """
    model = getattr(primary_role, "model", None)
    if not model:
        return None

    role_url = getattr(primary_role, "ollama_url", None)
    client, _resolved_url = get_agent_client(role_url)

    # Build role descriptions (only MCP-backed roles)
    desc_lines = []
    eligible = {name for name, r in roles.items() if r.mcp_servers}
    for name in sorted(eligible):
        role = roles[name]
        desc = role.description.get(lang, role.description.get("en", name))
        desc_lines.append(f"- {name}: {desc}")
    role_descriptions = "\n".join(desc_lines)

    # Build dynamic recognition rules
    recognition_rules = _build_recognition_rules(roles, lang)

    template = prompt_manager.get("orchestrator", "planner", lang=lang)
    if not template or template == "planner":
        logger.warning(f"[{req_id}] Orchestrator: planner prompt not found")
        return None

    prompt = template.format(
        message=message[:300],
        role_descriptions=role_descriptions,
        recognition_rules=recognition_rules,
    )
    logger.debug(f"[{req_id}] Planner prompt:\n{prompt}")

    try:
        response = await asyncio.wait_for(
            client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0, "num_predict": 800, "num_ctx": 4096},
            ),
            timeout=10.0,
        )
        raw = extract_response_content(response)
        logger.info(f"[{req_id}] Planner raw: {raw}")

        # Parse JSON (handle markdown fences and tolerate truncation)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start < 0 or end <= start:
            logger.info(f"[{req_id}] Planner: no JSON found, single-role")
            return None

        parsed = _try_parse_planner_json(raw[start:end])
        if parsed is None:
            logger.warning(f"[{req_id}] Planner: JSON unrecoverable, single-role")
            return None

        if not parsed.get("multi"):
            logger.info(f"[{req_id}] Planner: single-role (explicit)")
            return None

        steps = parsed.get("steps", [])
        if not steps or not isinstance(steps, list):
            logger.warning(f"[{req_id}] Planner: multi=true but no valid steps")
            return None

        # Validate each step: role must exist and have MCP servers
        valid_steps = []
        for step in steps:
            role_name = step.get("role", "")
            query = step.get("query", "")
            if role_name in eligible and role_name in roles and query:
                valid_steps.append({"role": role_name, "query": query})

        if len(valid_steps) < 2:
            logger.info(f"[{req_id}] Planner: fewer than 2 valid steps, single-role")
            return None

        logger.info(
            f"[{req_id}] Planner: multi-role detected -- "
            + ", ".join(f"{s['role']}: {s['query'][:50]}" for s in valid_steps)
        )
        return valid_steps

    except asyncio.TimeoutError:
        logger.warning(f"[{req_id}] Planner: timeout (10s), falling back to single-role")
        return None
    except Exception as e:
        logger.warning(f"[{req_id}] Planner: failed ({e}), falling back to single-role")
        return None


# ---------------------------------------------------------------------------
# Sub-agent runner
# ---------------------------------------------------------------------------

async def _run_sub_agent(
    step: dict,
    mcp_manager: Any,
    roles: dict[str, AgentRole],
    lang: str,
    req_id: str,
    history: list[dict] | None,
    memory_context: str,
    context_vars_text: str,
) -> SubAgentResult:
    """Run a single sub-agent to completion with isolated context.

    Each sub-agent runs inside its own asyncio Task, which copies the
    parent's contextvars context.  Hooks ``pre_sub_agent`` and
    ``post_sub_agent`` allow plugins to start/drain per-sub-agent collectors
    (contacts, provenance) and mutate the tool registry (preselect_tools).
    """
    from services.action_executor import ActionExecutor
    from services.agent_service import AgentService
    from services.agent_tools import AgentToolRegistry
    from services.ollama_service import OllamaService

    role_name = step["role"]
    sub_query = step["query"]
    role = roles.get(role_name)

    if not role:
        logger.warning(f"[{req_id}] Orchestrator: unknown role '{role_name}', skipping")
        return SubAgentResult(
            role=role_name, answer="", tool_summaries=[], tools_available=[],
        )

    logger.info(f"[{req_id}] Orchestrator: launching sub-agent [{role_name}]: {sub_query[:60]}")

    # Resolve role-specific prompt via prompt_key (e.g. "reva" -> prompts/reva.yaml)
    sub_prompt = prompt_manager.get(role.prompt_key, f"{role_name}_system_prompt", lang=lang)
    if not sub_prompt or sub_prompt == f"{role_name}_system_prompt":
        sub_prompt = prompt_manager.get(role.prompt_key, "system_prompt", lang=lang)
    if not sub_prompt or sub_prompt == "system_prompt":
        sub_prompt = prompt_manager.get("agent", "agent_prompt", lang=lang)

    # Each sub-agent gets its own tool registry (isolated context)
    tool_registry = AgentToolRegistry(
        mcp_manager=mcp_manager,
        server_filter=role.mcp_servers if role.mcp_servers else [role_name],
        internal_filter=["internal.knowledge_search"],
    )
    # Wait for plugin-registered tools (e.g. Reva local tools via register_tools hook)
    hook_task = getattr(tool_registry, "_hook_task", None)
    if hook_task:
        await hook_task

    # pre_sub_agent hook: plugins start collectors, mutate tool_registry (preselect)
    await run_hooks("pre_sub_agent", step=step, role=role_name, tool_registry=tool_registry)

    tools_available = list(tool_registry._tools.keys())
    tool_summaries: list[tuple[str, str]] = []
    ollama = OllamaService()
    agent = AgentService(tool_registry, role=role)
    executor = ActionExecutor(mcp_manager=mcp_manager)

    sub_answer = ""
    async for agent_step in agent.run(
        message=sub_query,
        ollama=ollama,
        executor=executor,
        lang=lang,
        personality_context=sub_prompt,
        conversation_history=history or None,
        context_vars_text=context_vars_text,
        memory_context=memory_context,
    ):
        if agent_step.step_type == "tool_call":
            logger.info(f"[{req_id}] Orchestrator[{role_name}]: tool_call {agent_step.tool}")
        elif agent_step.step_type == "tool_result":
            if agent_step.success:
                data_str = agent_step.content or ""
                if agent_step.data and isinstance(agent_step.data, list):
                    texts = [
                        item.get("text", "")
                        for item in agent_step.data
                        if isinstance(item, dict) and item.get("text")
                    ]
                    if texts:
                        data_str = texts[0]
                tool_summaries.append((agent_step.tool or "", data_str))
        elif agent_step.step_type == "final_answer":
            sub_answer = agent_step.content or ""
            logger.info(f"[{req_id}] Orchestrator[{role_name}]: final_answer ({len(sub_answer)} chars)")

    result = SubAgentResult(
        role=role_name,
        answer=sub_answer,
        tool_summaries=tool_summaries,
        tools_available=tools_available,
    )

    # post_sub_agent hook: plugins drain collectors, return plugin data
    hook_results = await run_hooks("post_sub_agent", step=step, role=role_name, result=result)
    for hr in hook_results:
        if isinstance(hr, dict):
            result.plugin_data.update(hr)

    return result


# ---------------------------------------------------------------------------
# Orchestrated execution (main entry point)
# ---------------------------------------------------------------------------

async def run_orchestrated(
    plan: list[dict],
    original_message: str,
    mcp_manager: Any,
    roles: dict[str, AgentRole],
    lang: str,
    req_id: str,
    history: list[dict] | None,
    memory_context: str,
    context_vars_text: str,
    user_name: str,
    user_id: int | None,
    typing_callback: Callable[[], Awaitable[None]] | None = None,
) -> OrchestrationResult:
    """Execute a multi-role query plan: run sub-agents in parallel, then synthesize.

    Args:
        plan: List of ``{"role": "...", "query": "..."}`` dicts from planner.
        original_message: The user's original message (resolved references).
        mcp_manager: MCP server manager for tool registry creation.
        roles: Dict of role_name -> AgentRole from the router.
        lang: Language code for prompts.
        req_id: Request correlation ID for logging.
        history: Conversation history for sub-agents.
        memory_context: Formatted memory section for sub-agent prompts.
        context_vars_text: Pre-built context variables text.
        user_name: Display name of the current user.
        user_id: Authenticated user ID.
        typing_callback: Optional async function called before parallel execution.

    Returns:
        OrchestrationResult with synthesis, sub-results, and plugin data.
    """
    # Typing indicator before parallel launch
    if typing_callback:
        try:
            await typing_callback()
        except Exception:
            pass  # Non-critical

    # Launch all sub-agents in parallel
    logger.info(f"[{req_id}] Orchestrator: parallel execution of {len(plan)} sub-agents")
    tasks = [
        _run_sub_agent(
            step=step,
            mcp_manager=mcp_manager,
            roles=roles,
            lang=lang,
            req_id=req_id,
            history=history,
            memory_context=memory_context,
            context_vars_text=context_vars_text,
        )
        for step in plan
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect results, handle errors gracefully
    sub_results: list[SubAgentResult] = []
    all_tool_summaries: list[tuple[str, str]] = []
    all_tools_available: list[str] = []
    errors: list[dict] = []
    all_plugin_data: dict[str, Any] = {}

    for i, result in enumerate(raw_results):
        if isinstance(result, Exception):
            role_name = plan[i]["role"]
            logger.warning(f"[{req_id}] Orchestrator: sub-agent [{role_name}] failed: {result}")
            errors.append({"role": role_name, "reason": str(result)})
            continue
        sub_results.append(result)
        all_tool_summaries.extend(result.tool_summaries)
        all_tools_available.extend(result.tools_available)
        # Merge plugin data from sub-agents
        for key, value in result.plugin_data.items():
            if key in all_plugin_data and isinstance(all_plugin_data[key], list) and isinstance(value, list):
                all_plugin_data[key].extend(value)
            else:
                all_plugin_data.setdefault(key, value)

    # --- Synthesize results ---
    if not sub_results:
        return OrchestrationResult(
            synthesis="Sorry, I could not process that request.",
            sub_results=[], tool_summaries=all_tool_summaries,
            tools_available=all_tools_available, plan=plan,
            plugin_data=all_plugin_data, errors=errors,
        )

    if len(sub_results) == 1:
        orch_result = OrchestrationResult(
            synthesis=sub_results[0].answer or "No result.",
            sub_results=sub_results, tool_summaries=all_tool_summaries,
            tools_available=all_tools_available, plan=plan,
            plugin_data=all_plugin_data, errors=errors,
        )
        await run_hooks("post_orchestration", result=orch_result, plan=plan, message=original_message)
        return orch_result

    # Build collected data block for synthesizer
    collected_parts = []
    for sr in sub_results:
        role = roles.get(sr.role)
        label = role.description.get(lang, sr.role) if role else sr.role
        collected_parts.append(f"[{label}]:\n{sr.answer}")
    collected_data = "\n\n".join(collected_parts)

    # Use the primary role's model for synthesis
    first_role = roles.get(plan[0]["role"])
    synth_model = getattr(first_role, "model", None) if first_role else None
    synth_url = getattr(first_role, "ollama_url", None) if first_role else None

    if not synth_model:
        # No model available for synthesis, concatenate
        fallback = "\n\n".join(
            f"**{roles[sr.role].description.get(lang, sr.role) if roles.get(sr.role) else sr.role}:**\n{sr.answer}"
            for sr in sub_results
        )
        orch_result = OrchestrationResult(
            synthesis=fallback, sub_results=sub_results,
            tool_summaries=all_tool_summaries, tools_available=all_tools_available,
            plan=plan, plugin_data=all_plugin_data, errors=errors,
        )
        await run_hooks("post_orchestration", result=orch_result, plan=plan, message=original_message)
        return orch_result

    synth_template = prompt_manager.get("orchestrator", "synthesizer", lang=lang)
    synth_prompt = synth_template.format(message=original_message, collected_data=collected_data)

    try:
        client, _url = get_agent_client(synth_url)
        response = await asyncio.wait_for(
            client.chat(
                model=synth_model,
                messages=[{"role": "user", "content": synth_prompt}],
                options={"temperature": 0.3, "num_predict": 1024},
            ),
            timeout=60.0,
        )
        final_answer = extract_response_content(response)
        logger.info(f"[{req_id}] Orchestrator: synthesis complete ({len(final_answer)} chars)")
        final_answer = _strip_llm_source_line(final_answer)
    except Exception as e:
        logger.warning(f"[{req_id}] Orchestrator synthesis failed ({e}), concatenating results")
        final_answer = "\n\n".join(
            f"**{roles[sr.role].description.get(lang, sr.role) if roles.get(sr.role) else sr.role}:**\n{sr.answer}"
            for sr in sub_results
        )

    orch_result = OrchestrationResult(
        synthesis=final_answer,
        sub_results=sub_results,
        tool_summaries=all_tool_summaries,
        tools_available=all_tools_available,
        plan=plan,
        plugin_data=all_plugin_data,
        errors=errors,
    )

    # post_orchestration hook: plugins decorate (contacts, provenance, cards)
    await run_hooks("post_orchestration", result=orch_result, plan=plan, message=original_message)

    logger.info(
        f"[{req_id}] Done (orchestrated): roles={[s.role for s in sub_results]}, "
        f"tools={len(all_tool_summaries)}"
    )
    return orch_result
