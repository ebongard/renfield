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
        """
        # Build role descriptions for the detection prompt
        role_lines = []
        for role in self.router.roles.values():
            if not role.has_agent_loop:
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

        Fires `pre_orchestration` before sub-agents launch and
        `post_orchestration` after synthesis. If any post_orchestration handler
        returns a dict containing a `card` key, an additional AgentStep with
        step_type="card" is yielded so the WebSocket layer can forward it to
        the client. First well-shaped card wins.

        Yields AgentStep objects for real-time feedback.
        """
        from services.agent_service import AgentStep
        from utils.hooks import run_hooks

        plan = {"sub_queries": list(sub_queries), "message": message, "lang": lang}
        try:
            await run_hooks("pre_orchestration", message=message, plan=plan, lang=lang)
        except Exception as e:
            # Hook failures must never break orchestration.
            logger.warning(f"pre_orchestration hook raised, ignoring: {e}")

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

        Returns dict with role, query, answer, and collected steps.
        """
        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry

        role_name = sq["role"]
        query = sq["query"]
        role = self.router.roles.get(role_name)

        if not role or not role.has_agent_loop:
            logger.warning(f"Orchestrator: skipping invalid role '{role_name}'")
            return {"role": role_name, "query": query, "answer": "", "steps": []}

        logger.info(f"Orchestrator: launching sub-agent [{role_name}]: {query[:60]}")

        # Each sub-agent gets its own tool registry (isolated context)
        tool_registry = AgentToolRegistry(
            mcp_manager=self.mcp_manager,
            server_filter=role.mcp_servers,
            internal_filter=role.internal_tools,
        )
        agent = AgentService(tool_registry, role=role)

        steps = []
        final_answer = None
        async for step in agent.run(
            message=query,
            ollama=ollama,
            executor=executor,
            lang=lang,
            **agent_kwargs,
        ):
            # Tag step with sub-agent role for frontend grouping
            step.data = step.data or {}
            step.data["sub_agent_role"] = role_name
            steps.append(step)
            if step.step_type == "final_answer":
                final_answer = step.content

        logger.info(f"Orchestrator: sub-agent [{role_name}] completed ({len(steps)} steps)")
        return {"role": role_name, "query": query, "answer": final_answer or "", "steps": steps}

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

        # Yield steps grouped by sub-agent + collect results for synthesis
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
                yield step
            sub_results.append(result)

        # Synthesize combined answer
        if len([r for r in sub_results if r.get("answer")]) >= 2:
            synthesized = await self._synthesize(message, sub_results, ollama, lang)
            if synthesized:
                yield AgentStep(
                    step_number=99,
                    step_type="final_answer",
                    content=synthesized,
                )

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

            final_answer = None
            async for step in agent.run(
                message=query,
                ollama=ollama,
                executor=executor,
                lang=lang,
                **agent_kwargs,
            ):
                yield step
                if step.step_type == "final_answer":
                    final_answer = step.content

            sub_results.append({
                "role": role_name,
                "query": query,
                "answer": final_answer or "",
            })

        # Synthesize combined answer
        if len(sub_results) >= 2:
            synthesized = await self._synthesize(message, sub_results, ollama, lang)
            if synthesized:
                yield AgentStep(
                    step_number=99,
                    step_type="final_answer",
                    content=synthesized,
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
                timeout=30.0,
            )
            return extract_response_content(raw_response) or None

        except Exception as e:
            logger.warning(f"Orchestrator synthesis failed: {e}")
            # Fallback: concatenate
            return "\n\n".join(r["answer"] for r in sub_results if r["answer"])
