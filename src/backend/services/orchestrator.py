"""
Cross-MCP Query Orchestrator -- Decomposes multi-domain queries into sub-queries.

Detects when a user message spans multiple domains (e.g. "Mach Licht an UND
spiel Musik") and runs domain-specific sub-agents sequentially, then synthesizes
results into a combined answer.

Opt-in via AGENT_ORCHESTRATOR_ENABLED=true.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from loguru import logger

from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import extract_response_content, get_agent_client, get_classification_chat_kwargs

if TYPE_CHECKING:
    from services.action_executor import ActionExecutor
    from services.agent_router import AgentRole, AgentRouter
    from services.agent_service import AgentStep
    from services.mcp_client import MCPManager
    from services.ollama_service import OllamaService


class QueryOrchestrator:
    """Orchestrates multi-domain queries across specialized agents."""

    def __init__(
        self,
        agent_router: "AgentRouter",
        mcp_manager: "MCPManager",
    ):
        self.router = agent_router
        self.mcp_manager = mcp_manager

    async def detect_multi_domain(
        self,
        message: str,
        ollama: "OllamaService",
        lang: str = "de",
    ) -> list[dict] | None:
        """Detect if a message needs multi-domain handling.

        Returns list of sub-queries [{role: str, query: str}] or None.

        Plugin extension: the ``extend_orchestrator_roles`` hook runs
        before the planner prompt is built. Each handler returns an
        iterable of additional role *names* to include in the planner's
        role list (the role's existing description from ``self.router.roles``
        is used; unknown names are silently ignored). This lets a plugin
        promote a non-``has_agent_loop`` role into the planner's vocabulary
        without modifying the agent_router.
        """
        from utils.hooks import run_hooks

        # Default eligibility: any role with an agent loop. Plugins can
        # extend this set via the extend_orchestrator_roles hook.
        eligible_names: set[str] = {
            role.name for role in self.router.roles.values() if role.has_agent_loop
        }
        try:
            extra_results = await run_hooks(
                "extend_orchestrator_roles",
                roles=self.router.roles,
                lang=lang,
            )
        except Exception as e:
            logger.warning(f"extend_orchestrator_roles hook raised, ignoring: {e}")
            extra_results = []

        for er in extra_results:
            if er is None:
                continue
            try:
                eligible_names.update(name for name in er if name in self.router.roles)
            except TypeError:
                logger.warning(
                    f"extend_orchestrator_roles handler returned non-iterable "
                    f"(type={type(er).__name__}); ignoring"
                )

        # Build role descriptions for the detection prompt
        role_lines = []
        for name in sorted(eligible_names):
            role = self.router.roles.get(name)
            if role is None:
                continue
            desc = role.description.get(lang, role.description.get("de", ""))
            role_lines.append(f"- {role.name}: {desc}")

        role_descriptions = "\n".join(role_lines)
        detect_prompt = prompt_manager.get(
            "agent", "orchestrator_detect_prompt", lang=lang,
            message=message, role_descriptions=role_descriptions,
        )
        if not detect_prompt:
            return None

        # Planner: use the primary role's agent model + URL. Small router
        # models (e.g. llama3.2:3b) cannot reliably emit pure JSON for
        # multi-domain decomposition — they wrap JSON in prose and invent
        # role names. Reva ran qwen3.5:27b on llama-server as its planner
        # in production for months; we mirror that here.
        primary_role = next(
            (r for r in self.router.roles.values() if r.has_agent_loop),
            None,
        )
        planner_model: str | None = None
        planner_url: str | None = None
        if primary_role is not None:
            planner_model = getattr(primary_role, "model", None)
            planner_url = getattr(primary_role, "ollama_url", None)
        planner_model = planner_model or settings.ollama_model
        planner_url = planner_url or settings.agent_ollama_url

        if not planner_model:
            logger.debug("Orchestrator: no planner model configured, skipping detection")
            return None

        try:
            if planner_url:
                client, _ = get_agent_client(fallback_url=planner_url)
            else:
                client = ollama.client

            classification_kwargs = get_classification_chat_kwargs(planner_model)
            # num_predict=800 matches Reva's production planner budget — enough
            # for a 4-sub-agent plan with localized query strings.
            raw_response = await asyncio.wait_for(
                client.chat(
                    model=planner_model,
                    messages=[{"role": "user", "content": detect_prompt}],
                    options={"temperature": 0, "num_predict": 800, "num_ctx": 4096},
                    **classification_kwargs,
                ),
                timeout=settings.agent_router_timeout,
            )
            response_text = (extract_response_content(raw_response) or "").strip()

            # Accept the explicit "null" sentinel (single-domain signal).
            if response_text.lower() in ("null", "none", ""):
                return None

            # Extract the JSON array from a potentially-prose response.
            # Even large models occasionally wrap the array in explanation.
            start = response_text.find("[")
            end = response_text.rfind("]") + 1
            if start < 0 or end <= start:
                logger.info(f"Orchestrator: no JSON array in response, single-role. Raw: {response_text[:200]}")
                return None

            try:
                sub_queries = json.loads(response_text[start:end])
            except json.JSONDecodeError as e:
                logger.info(f"Orchestrator: JSON parse failed ({e}), single-role. Raw: {response_text[start:end][:200]}")
                return None

            if not isinstance(sub_queries, list) or len(sub_queries) < 2:
                return None

            # Validate each sub-query has role + query and the role exists.
            valid = []
            for sq in sub_queries:
                if isinstance(sq, dict) and sq.get("role") and sq.get("query"):
                    if sq["role"] in self.router.roles:
                        valid.append(sq)

            if len(valid) < 2:
                logger.info(
                    f"Orchestrator: parsed {len(sub_queries)} entries but only "
                    f"{len(valid)} had valid roles, single-role"
                )
                return None

            logger.info(
                f"Orchestrator detected {len(valid)} domains: "
                f"{[sq['role'] for sq in valid]}"
            )
            return valid

        except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
            logger.warning(f"Orchestrator detection failed: {e}")
            return None

    async def run_orchestrated(
        self,
        sub_queries: list[dict],
        message: str,
        ollama: "OllamaService",
        executor: "ActionExecutor",
        lang: str = "de",
        **agent_kwargs,
    ) -> AsyncGenerator["AgentStep", None]:
        """Run sub-agents and synthesize results.

        When agent_orchestrator_parallel is True, sub-agents run in parallel
        with isolated contexts. Otherwise falls back to sequential execution.

        Fires ``post_orchestration`` after synthesis. If any handler
        returns a dict containing a ``card`` key, an additional AgentStep with
        step_type="card" is yielded so the WebSocket layer can forward it to
        the client. First well-shaped card wins.

        ``pre_orchestration`` is *not* fired here — it fires upstream in the
        caller (chat_handler / Teams transport) before sub_queries are
        determined, so plugins can inject a pre-computed plan. By the time
        we reach this method, the plan is already final. Firing again here
        would create double-firing semantics that handlers would have to
        guard against.

        Yields AgentStep objects for real-time feedback.
        """
        from services.agent_service import AgentStep
        from utils.hooks import run_hooks

        sub_results: list[dict] = []
        final_answer: str | None = None

        if settings.agent_orchestrator_parallel:
            inner = self._run_parallel(
                sub_queries, message, ollama, executor, lang,
                sub_results_out=sub_results, **agent_kwargs,
            )
        else:
            inner = self._run_sequential(
                sub_queries, message, ollama, executor, lang,
                sub_results_out=sub_results, **agent_kwargs,
            )

        async for step in inner:
            if step.step_type == "final_answer":
                final_answer = step.content
            yield step

        try:
            hook_results = await run_hooks(
                "post_orchestration",
                message=message,
                sub_results=sub_results,
                final_answer=final_answer,
                lang=lang,
            )
        except Exception as e:
            logger.warning(f"post_orchestration hook raised, ignoring: {e}")
            hook_results = []

        for hr in hook_results:
            if isinstance(hr, dict) and hr.get("card"):
                yield AgentStep(
                    step_number=100,
                    step_type="card",
                    content="",
                    data={"card": hr["card"]},
                )
                break

    async def _run_sub_agent(
        self,
        sq: dict,
        ollama: "OllamaService",
        executor: "ActionExecutor",
        lang: str,
        **agent_kwargs,
    ) -> dict:
        """Run a single sub-agent to completion with isolated context.

        Fires ``pre_sub_agent`` before the agent loop (after the per-task
        tool registry is built — handlers may mutate it, e.g. to pre-select
        a narrower tool list) and ``post_sub_agent`` after the loop
        completes. Each handler's return-dict is merged into the result's
        ``plugin_data`` field so callers downstream (``post_orchestration``)
        see a single accumulated dict per sub-agent.

        Returns dict with role, query, answer, steps, and plugin_data.
        """
        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry
        from utils.hooks import run_hooks

        role_name = sq["role"]
        query = sq["query"]
        role = self.router.roles.get(role_name)

        if not role or not role.has_agent_loop:
            logger.warning(f"Orchestrator: skipping invalid role '{role_name}'")
            return {"role": role_name, "query": query, "answer": "", "steps": [], "plugin_data": {}}

        logger.info(f"Orchestrator: launching sub-agent [{role_name}]: {query[:60]}")

        # Each sub-agent gets its own tool registry (isolated context)
        tool_registry = AgentToolRegistry(
            mcp_manager=self.mcp_manager,
            server_filter=role.mcp_servers,
            internal_filter=role.internal_tools,
        )

        # Wait for plugin-registered tools to be ready before the agent
        # loop reads the registry. ``register_tools`` hooks attach tools
        # asynchronously via ``_hook_task``; without this await the agent
        # may run before plugin tools are registered, causing intermittent
        # "tool not found" failures on the first orchestrated call after
        # startup. The single-agent path in chat_handler also lacks this
        # await — fixing it here is part of the orchestrator uplift.
        hook_task = getattr(tool_registry, "_hook_task", None)
        if hook_task is not None:
            try:
                await hook_task
            except Exception as e:
                logger.warning(f"tool_registry._hook_task raised, continuing: {e}")

        agent = AgentService(tool_registry, role=role)

        # Fire pre_sub_agent — plugins receive the registry and may mutate
        # it (e.g. tool pre-selection, contact accumulator init). Hook
        # exceptions never break the sub-agent.
        try:
            await run_hooks(
                "pre_sub_agent",
                step=sq,
                role=role_name,
                tool_registry=tool_registry,
                lang=lang,
            )
        except Exception as e:
            logger.warning(f"pre_sub_agent hook raised, continuing: {e}")

        steps = []
        final_answer = None
        async for step in agent.run(
            message=query,
            ollama=ollama,
            executor=executor,
            lang=lang,
            **agent_kwargs,
        ):
            # Tag step with sub-agent role for frontend grouping. Some MCP
            # tools return list-shaped step.data (e.g. JQL search results) —
            # only inject the marker when data is dict-shaped or unset, never
            # convert a list into a dict (would lose the result payload).
            if step.data is None:
                step.data = {"sub_agent_role": role_name}
            elif isinstance(step.data, dict):
                step.data["sub_agent_role"] = role_name
            # list/scalar data stays as-is; sub_agent_role is then unavailable
            # for frontend grouping on that step but the data itself survives.
            steps.append(step)
            if step.step_type == "final_answer":
                final_answer = step.content

        result = {
            "role": role_name,
            "query": query,
            "answer": final_answer or "",
            "steps": steps,
            "plugin_data": {},
        }

        # Fire post_sub_agent — plugins receive the completed result and
        # may attach side-channel data (drained contact accumulators,
        # provenance entries, telemetry). Each handler's return-dict is
        # merged into result["plugin_data"]; later handlers' keys win.
        try:
            hook_results = await run_hooks(
                "post_sub_agent",
                step=sq,
                role=role_name,
                result=result,
                lang=lang,
            )
        except Exception as e:
            logger.warning(f"post_sub_agent hook raised, continuing: {e}")
            hook_results = []

        for hr in hook_results:
            if isinstance(hr, dict):
                result["plugin_data"].update(hr)

        logger.info(f"Orchestrator: sub-agent [{role_name}] completed ({len(steps)} steps)")
        return result

    async def _run_parallel(
        self,
        sub_queries: list[dict],
        message: str,
        ollama: "OllamaService",
        executor: "ActionExecutor",
        lang: str = "de",
        sub_results_out: list[dict] | None = None,
        **agent_kwargs,
    ) -> AsyncGenerator["AgentStep", None]:
        """Run all sub-agents in parallel, then synthesize.

        `sub_results_out` (when provided) is appended to as sub-agents complete,
        giving the caller access to the structured per-role answers needed for
        the post_orchestration hook.
        """
        from services.agent_service import AgentStep

        logger.info(f"⚡ Orchestrator: parallel execution of {len(sub_queries)} sub-agents")

        # Launch all sub-agents in parallel (isolated contexts)
        tasks = [
            self._run_sub_agent(sq, ollama, executor, lang, **agent_kwargs)
            for sq in sub_queries
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Yield steps grouped by sub-agent + collect results for synthesis.
        # IMPORTANT: per-sub-agent `final_answer` steps are suppressed
        # here — they are intermediate artifacts, not the combined
        # response. The user sees exactly one final answer: either the
        # synthesizer's output (for 2+ successful sub-agents) or the
        # single surviving sub-agent's answer (1 sub-agent, or fallback).
        # Without this filter, the web chat would render 1 + N answers
        # (each with its own greeting), which duplicates content and
        # confuses the user.
        sub_results: list[dict] = sub_results_out if sub_results_out is not None else []
        for sq, result in zip(sub_queries, raw_results):
            if isinstance(result, Exception):
                logger.error(f"Orchestrator: sub-agent [{sq['role']}] failed: {result}")
                yield AgentStep(
                    step_number=0,
                    step_type="error",
                    content=f"Sub-Agent [{sq['role']}] fehlgeschlagen: {result}",
                )
                sub_results.append({"role": sq["role"], "query": sq["query"], "answer": ""})
                continue

            for step in result["steps"]:
                if step.step_type == "final_answer":
                    continue  # see note above
                yield step
            sub_results.append(result)

        async for step in self._emit_combined_answer(message, sub_results, ollama, lang):
            yield step

    async def _run_sequential(
        self,
        sub_queries: list[dict],
        message: str,
        ollama: "OllamaService",
        executor: "ActionExecutor",
        lang: str = "de",
        sub_results_out: list[dict] | None = None,
        **agent_kwargs,
    ) -> AsyncGenerator["AgentStep", None]:
        """Run sub-agents sequentially (original behavior).

        `sub_results_out` (when provided) is appended to as each sub-agent
        completes, giving the caller access to the structured per-role answers
        for the post_orchestration hook.
        """
        from services.agent_service import AgentService, AgentStep
        from services.agent_tools import AgentToolRegistry

        sub_results: list[dict] = sub_results_out if sub_results_out is not None else []

        for i, sq in enumerate(sub_queries):
            role_name = sq["role"]
            query = sq["query"]
            role = self.router.roles.get(role_name)

            if not role or not role.has_agent_loop:
                logger.warning(f"Orchestrator: skipping invalid role '{role_name}'")
                continue

            logger.info(f"Orchestrator: running sub-agent {i+1}/{len(sub_queries)} [{role_name}]: {query[:60]}")

            tool_registry = AgentToolRegistry(
                mcp_manager=self.mcp_manager,
                server_filter=role.mcp_servers,
                internal_filter=role.internal_tools,
            )
            agent = AgentService(tool_registry, role=role)

            # Same suppression rule as _run_parallel — only the
            # combined answer should be surfaced to the user.
            final_answer = None
            async for step in agent.run(
                message=query,
                ollama=ollama,
                executor=executor,
                lang=lang,
                **agent_kwargs,
            ):
                if step.step_type == "final_answer":
                    final_answer = step.content
                    continue
                yield step

            sub_results.append({
                "role": role_name,
                "query": query,
                "answer": final_answer or "",
            })

        async for step in self._emit_combined_answer(message, sub_results, ollama, lang):
            yield step

    async def _emit_combined_answer(
        self,
        message: str,
        sub_results: list[dict],
        ollama: "OllamaService",
        lang: str,
    ) -> "AsyncGenerator[AgentStep, None]":
        """Yield a single combined ``final_answer`` for the orchestrated turn.

        Logic:
        1. Synthesize via LLM when ≥2 sub-agents returned a non-empty
           answer — the combined deck needs narrative glue.
        2. Fall back to the first non-empty sub-agent answer when only
           one succeeded.
        3. When *every* sub-agent failed (``non_empty`` is empty), emit
           a visible error message so the user sees feedback and the
           downstream chat_handler persists the turn. Returning silently
           here would leave ``full_response=""``, which gates both the
           WebSocket final bubble AND DB persistence — losing the whole
           turn including the user's message.
        """
        from services.agent_service import AgentStep

        non_empty = [r for r in sub_results if r.get("answer")]

        if len(non_empty) >= 2:
            synthesized = await self._synthesize(message, sub_results, ollama, lang)
            if synthesized:
                yield AgentStep(
                    step_number=99,
                    step_type="final_answer",
                    content=synthesized,
                )
                return
            # Synthesizer returned nothing — fall through to fallback.

        if non_empty:
            yield AgentStep(
                step_number=99,
                step_type="final_answer",
                content=non_empty[0]["answer"],
            )
            return

        # Every sub-agent failed. Surface a localized error so the user
        # isn't left staring at an empty reply.
        failed_roles = [r.get("role", "?") for r in sub_results]
        if lang.startswith("de"):
            msg = (
                "Keine der angefragten Integrationen hat eine Antwort "
                f"geliefert (betroffen: {', '.join(failed_roles)}). "
                "Bitte versuche es in einem Moment erneut."
            )
        else:
            msg = (
                "None of the requested integrations returned an answer "
                f"(affected: {', '.join(failed_roles)}). "
                "Please try again in a moment."
            )
        yield AgentStep(
            step_number=99,
            step_type="final_answer",
            content=msg,
        )

    async def _synthesize(
        self,
        message: str,
        sub_results: list[dict],
        ollama: "OllamaService",
        lang: str,
    ) -> str | None:
        """Combine sub-results into a unified answer via LLM."""
        results_text = "\n".join(
            f"- [{r['role']}] {r['query']}: {r['answer']}"
            for r in sub_results
        )

        synthesize_prompt = prompt_manager.get(
            "agent", "orchestrator_synthesize_prompt", lang=lang,
            message=message, sub_results=results_text,
        )
        if not synthesize_prompt:
            # Fallback: concatenate
            return "\n\n".join(r["answer"] for r in sub_results if r["answer"])

        try:
            router_model = settings.agent_router_model or settings.ollama_intent_model or settings.ollama_model
            classification_kwargs = get_classification_chat_kwargs(router_model)

            raw_response = await asyncio.wait_for(
                ollama.client.chat(
                    model=router_model,
                    messages=[{"role": "user", "content": synthesize_prompt}],
                    options={"temperature": 0.3, "num_predict": 500},
                    **classification_kwargs,
                ),
                timeout=settings.orchestrator_synthesis_timeout,
            )
            return extract_response_content(raw_response) or None

        except Exception as e:
            logger.warning(f"Orchestrator synthesis failed: {e}")
            # Fallback: concatenate
            return "\n\n".join(r["answer"] for r in sub_results if r["answer"])
