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

        try:
            router_url = settings.agent_router_url or settings.agent_ollama_url
            router_model = settings.agent_router_model or settings.ollama_intent_model or settings.ollama_model

            if router_url:
                client, _ = get_agent_client(fallback_url=router_url)
            else:
                client = ollama.client

            classification_kwargs = get_classification_chat_kwargs(router_model)
            raw_response = await asyncio.wait_for(
                client.chat(
                    model=router_model,
                    messages=[{"role": "user", "content": detect_prompt}],
                    options={"temperature": 0, "num_predict": 256, "num_ctx": 4096},
                    **classification_kwargs,
                ),
                timeout=settings.agent_router_timeout,
            )
            response_text = extract_response_content(raw_response) or ""

            # Parse response — either JSON array or "null"
            response_text = response_text.strip()
            if response_text.lower() in ("null", "none", ""):
                return None

            sub_queries = json.loads(response_text)
            if not isinstance(sub_queries, list) or len(sub_queries) < 2:
                return None

            # Validate each sub-query has role and query
            valid = []
            for sq in sub_queries:
                if isinstance(sq, dict) and sq.get("role") and sq.get("query"):
                    # Verify role exists
                    if sq["role"] in self.router.roles:
                        valid.append(sq)

            if len(valid) < 2:
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
        """Run sub-agents sequentially, then synthesize results.

        Yields AgentStep objects for real-time feedback.
        """
        from services.agent_service import AgentService, AgentStep
        from services.agent_tools import AgentToolRegistry

        sub_results: list[dict] = []

        for i, sq in enumerate(sub_queries):
            role_name = sq["role"]
            query = sq["query"]
            role = self.router.roles.get(role_name)

            if not role or not role.has_agent_loop:
                logger.warning(f"Orchestrator: skipping invalid role '{role_name}'")
                continue

            logger.info(f"Orchestrator: running sub-agent {i+1}/{len(sub_queries)} [{role_name}]: {query[:60]}")

            # Create filtered tool registry for this domain
            tool_registry = AgentToolRegistry(
                mcp_manager=self.mcp_manager,
                server_filter=role.mcp_servers,
                internal_filter=role.internal_tools,
            )
            agent = AgentService(tool_registry, role=role)

            # Run sub-agent and collect results
            final_answer = None
            async for step in agent.run(
                message=query,
                ollama=ollama,
                executor=executor,
                lang=lang,
                **agent_kwargs,
            ):
                # Forward intermediate steps to the caller
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
