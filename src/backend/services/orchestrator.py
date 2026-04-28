"""
Cross-MCP Query Orchestrator -- Decomposes multi-domain queries into sub-queries.

Detects when a user message spans multiple domains (e.g. "Mach Licht an UND
spiel Musik") and runs domain-specific sub-agents sequentially, then synthesizes
results into a combined answer.

Opt-in via AGENT_ORCHESTRATOR_ENABLED=true.
"""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING

from loguru import logger

from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import extract_response_content, get_agent_client, get_classification_chat_kwargs


# Strip "_Quelle: ..._" / "_Source: ..._" lines that synthesizer LLMs
# sometimes write alongside the actual answer. Transports/plugins that
# attach their own canonical source footer (Reva's transport.py) expect
# the LLM not to duplicate it. The pattern tolerates italic/bold markers,
# both DE and EN variants, and trailing whitespace. Anchored to line
# start; multi-line mode so the regex acts per-line.
_SOURCE_LINE_RE: re.Pattern[str] = re.compile(
    r"(?im)^\s*[_*]*\s*(quelle|source|sources|quellen)\s*[:：][^\n]*[_*]*\s*$",
)


def _strip_source_line(answer: str) -> str:
    """Remove any trailing ``_Quelle: ..._`` / ``_Source: ..._`` line."""
    return _SOURCE_LINE_RE.sub("", answer).rstrip()

if TYPE_CHECKING:
    from services.action_executor import ActionExecutor
    from services.agent_router import AgentRole, AgentRouter
    from services.agent_service import AgentStep
    from services.mcp_client import MCPManager
    from services.ollama_service import OllamaService


# Plugin-data fields that are list-shaped by convention. When multiple
# post_sub_agent handlers contribute to one of these keys, results are
# concatenated rather than overwritten — matching how Reva's contact
# accumulator and provenance trail expect to merge across plugins.
# Other keys still follow last-writer-wins with a collision warning.
_LIST_SHAPED_PLUGIN_DATA_FIELDS: frozenset[str] = frozenset(
    {"contacts", "provenance", "warnings"}
)


def _failed_sub_result(role: str, query: str, error: str | None = None) -> dict:
    """Empty-shape result for a sub-agent that failed before producing output.

    Centralizing this keeps the success-path and failure-path dict shapes
    in sync. A drift would silently break ``post_orchestration`` handlers
    that walk ``sub_results`` and read ``plugin_data`` / ``steps``.

    The optional ``error`` field carries a user-displayable message; when
    present it triggers an ``error`` AgentStep in ``_run_parallel`` /
    ``_run_sequential`` so the user sees that one of their sub-agents
    failed (and the synthesizer's combined answer omits it).
    """
    return {
        "role": role,
        "query": query,
        "answer": "",
        "steps": [],
        "plugin_data": {},
        "error": error,
    }


class QueryOrchestrator:
    """Orchestrates multi-domain queries across specialized agents."""

    def __init__(
        self,
        agent_router: "AgentRouter",
        mcp_manager: "MCPManager",
    ):
        self.router = agent_router
        self.mcp_manager = mcp_manager
        # Cache of orchestrator-eligible role names. Populated lazily on
        # first detect_multi_domain call so plugins that register
        # extend_orchestrator_roles handlers AFTER this constructor runs
        # (typical: Renfield's lifecycle does plugin registration before
        # the first user request) are still picked up. Subsequent calls
        # use the cached value, avoiding per-request hook fires.
        self._eligible_roles_cache: set[str] | None = None

    async def _resolve_eligible_roles(self, lang: str) -> set[str]:
        """Build the planner's eligible-role set, cached after first call.

        Default: any role with ``has_agent_loop=True``. Plugins can extend
        the set via the ``extend_orchestrator_roles`` hook (returning an
        iterable of role names; unknown names are silently dropped). The
        result is cached on the instance so plugin hooks fire only once
        per orchestrator lifetime, not per request.
        """
        if self._eligible_roles_cache is not None:
            return self._eligible_roles_cache

        eligible: set[str] = {
            role.name for role in self.router.roles.values() if role.has_agent_loop
        }

        # run_hooks never raises (utils/hooks.py contract).
        from utils.hooks import run_hooks
        extra_results = await run_hooks(
            "extend_orchestrator_roles",
            roles=self.router.roles,
            lang=lang,
        )
        for er in extra_results:
            if er is None:
                continue
            try:
                eligible.update(name for name in er if name in self.router.roles)
            except TypeError:
                logger.warning(
                    f"extend_orchestrator_roles handler returned non-iterable "
                    f"(type={type(er).__name__}); ignoring"
                )

        self._eligible_roles_cache = eligible
        return eligible

    async def detect_multi_domain(
        self,
        message: str,
        ollama: "OllamaService",
        lang: str = "de",
    ) -> list[dict] | None:
        """Detect if a message needs multi-domain handling.

        Returns list of sub-queries [{role: str, query: str}] or None.

        The planner's role vocabulary comes from ``_resolve_eligible_roles``
        (cached after first call). Plugins extend it via the
        ``extend_orchestrator_roles`` hook.
        """
        eligible_names = await self._resolve_eligible_roles(lang)

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

        except asyncio.TimeoutError:
            logger.warning(
                f"Orchestrator detection timed out after "
                f"{settings.agent_router_timeout}s — falling back to single-role"
            )
            return None
        except (ConnectionError, json.JSONDecodeError) as e:
            # Network/parse failures: log and fall back. JSON decode is
            # belt-and-braces — the response_text path above already handles
            # malformed JSON via the `start/end` slice + try/except.
            logger.warning(f"Orchestrator detection: {type(e).__name__}: {e}")
            return None

    async def run_orchestrated(
        self,
        sub_queries: list[dict],
        message: str,
        ollama: "OllamaService",
        executor: "ActionExecutor",
        lang: str = "de",
        typing_callback: "Callable[[], Awaitable[None]] | None" = None,
        **agent_kwargs,
    ) -> AsyncGenerator["AgentStep", None]:
        """Run sub-agents and synthesize results.

        ``pre_orchestration`` fires upstream in the caller (chat_handler
        / Teams transport) before sub_queries are determined, so plugins
        can inject a pre-computed plan. Firing again here would create
        double-firing semantics that handlers would have to guard against.

        ``typing_callback`` (optional, design Resolved-Q2) is invoked once
        before sub-agents launch so transports can emit a generic typing
        indicator before the planner-and-fan-out work begins. Teams
        passes ``context.send_activity(typing)``; web passes a websocket
        send. Failure to invoke is logged but doesn't break the run.

        Yields AgentStep objects for real-time feedback. After synthesis,
        ``post_orchestration`` fires; the first handler returning a dict
        with a ``card`` key contributes an extra ``card`` step.
        """
        from services.agent_service import AgentStep
        from utils.hooks import run_hooks

        if typing_callback is not None:
            try:
                await typing_callback()
            except Exception as e:
                logger.warning(f"typing_callback raised, ignoring: {e}")

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

        # run_hooks never raises — direct call.
        hook_results = await run_hooks(
            "post_orchestration",
            message=message,
            sub_results=sub_results,
            final_answer=final_answer,
            lang=lang,
        )
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

        Acts as the **exception sink** for both parallel and sequential
        modes: every code path returns a dict with the canonical shape
        (see :func:`_failed_sub_result`). Crashes are caught, logged, and
        surfaced as the result's ``error`` field. Both modes can therefore
        treat sub-agent failures uniformly without their own try/except.

        Hook lifecycle:
        - ``pre_sub_agent`` fires once after ``tool_registry`` is built
          (handlers may mutate it, e.g. for tool pre-selection).
        - ``post_sub_agent`` fires unconditionally if ``pre_sub_agent``
          fired — even if ``agent.run`` raises mid-stream — so plugin
          accumulators (contacts, provenance) always get drained.
        - Plugin contributions to ``result["plugin_data"]`` from
          ``post_sub_agent`` follow per-key merge semantics: keys in
          :data:`_LIST_SHAPED_PLUGIN_DATA_FIELDS` are concatenated;
          non-list keys follow last-writer-wins with a warning on
          collision.
        """
        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry
        from utils.hooks import run_hooks

        # Validate sub-query shape — buggy plugin-supplied plans (from
        # pre_orchestration) and detect_multi_domain bugs both flow here.
        if not isinstance(sq, dict):
            logger.error(f"Orchestrator: malformed sub-query (not a dict): {sq!r}")
            return _failed_sub_result(
                "?", "",
                error=f"Malformed sub-query: not a dict (got {type(sq).__name__})",
            )
        role_name = sq.get("role")
        query = sq.get("query")
        if not isinstance(role_name, str) or not isinstance(query, str):
            logger.error(f"Orchestrator: malformed sub-query (missing role/query): {sq!r}")
            return _failed_sub_result(
                role_name if isinstance(role_name, str) else "?",
                query if isinstance(query, str) else "",
                error="Malformed sub-query: missing role or query",
            )

        role = self.router.roles.get(role_name)
        if not role or not role.has_agent_loop:
            logger.warning(f"Orchestrator: skipping invalid role '{role_name}'")
            return _failed_sub_result(role_name, query)

        logger.info(f"Orchestrator: launching sub-agent [{role_name}]: {query[:60]}")

        try:
            try:
                tool_registry = await AgentToolRegistry.create(
                    mcp_manager=self.mcp_manager,
                    server_filter=role.mcp_servers,
                    internal_filter=role.internal_tools,
                )
            except Exception as e:
                logger.opt(exception=True).error(
                    f"Tool registry init failed for [{role_name}]: "
                    f"{type(e).__name__}: {e}"
                )
                msg = (
                    f"Tools für Rolle '{role_name}' konnten nicht geladen werden."
                    if lang.startswith("de") else
                    f"Tools for role '{role_name}' failed to load."
                )
                return _failed_sub_result(role_name, query, error=msg)

            agent = AgentService(tool_registry, role=role)

            # run_hooks never raises (utils/hooks.py contract) — direct call.
            await run_hooks(
                "pre_sub_agent",
                step=sq,
                role=role_name,
                tool_registry=tool_registry,
                lang=lang,
            )

            steps: list = []
            final_answer: str | None = None
            try:
                async for step in agent.run(
                    message=query,
                    ollama=ollama,
                    executor=executor,
                    lang=lang,
                    **agent_kwargs,
                ):
                    # Tag step with sub-agent role for frontend grouping.
                    # Only inject when data is dict-shaped or unset — list
                    # data (JQL results) and scalars must stay as-is so
                    # downstream callers reading raw payloads don't break.
                    # Tradeoff: list-shaped steps lose the role marker
                    # (frontend grouping degrades for those), but the
                    # data payload is preserved.
                    if step.data is None:
                        step.data = {"sub_agent_role": role_name}
                    elif isinstance(step.data, dict):
                        step.data["sub_agent_role"] = role_name
                    steps.append(step)
                    if step.step_type == "final_answer":
                        final_answer = step.content
                agent_run_error: str | None = None
            except Exception as e:
                logger.opt(exception=True).error(
                    f"Sub-agent [{role_name}] agent.run crashed: {e}"
                )
                agent_run_error = str(e)
                final_answer = None

            result: dict = {
                "role": role_name,
                "query": query,
                "answer": final_answer or "",
                "steps": steps,
                "plugin_data": {},
                "error": agent_run_error,
            }

            # post_sub_agent: fire even on agent.run crash so plugins can
            # drain accumulators that pre_sub_agent populated.
            hook_results = await run_hooks(
                "post_sub_agent",
                step=sq,
                role=role_name,
                result=result,
                lang=lang,
            )
            for hr in hook_results:
                if not isinstance(hr, dict):
                    continue
                for k, v in hr.items():
                    if k in _LIST_SHAPED_PLUGIN_DATA_FIELDS and isinstance(v, list):
                        result["plugin_data"].setdefault(k, []).extend(v)
                    elif k in result["plugin_data"]:
                        logger.warning(
                            f"post_sub_agent: key '{k}' contributed by multiple "
                            f"handlers (last writer wins) — registration order "
                            f"determines outcome"
                        )
                        result["plugin_data"][k] = v
                    else:
                        result["plugin_data"][k] = v

        except Exception as e:
            logger.opt(exception=True).error(
                f"Sub-agent [{role_name}] crashed in setup/teardown: {e}"
            )
            return _failed_sub_result(role_name, query, error=str(e))

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

        ``sub_results_out`` (when provided) is appended to as sub-agents
        complete. ``_run_sub_agent`` is the exception sink — gather should
        only see dicts, but ``return_exceptions=True`` is kept as
        belt-and-braces against future regressions.

        On ``CancelledError`` (e.g. WebSocket client disconnects), pending
        sub-agents are cancelled to free LLM/MCP resources rather than
        running to completion with a discarded result.
        """
        from services.agent_service import AgentStep

        logger.info(f"⚡ Orchestrator: parallel execution of {len(sub_queries)} sub-agents")

        tasks = [
            asyncio.create_task(
                self._run_sub_agent(sq, ollama, executor, lang, **agent_kwargs)
            )
            for sq in sub_queries
        ]
        try:
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Wait briefly for cancellation to propagate, but don't block
            # the cancellation chain on uncooperative sub-agents.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # Per-sub-agent final_answer steps are suppressed — only the
        # synthesizer's combined answer (or the single-survivor fallback)
        # reaches the user. Without this, the UI would render 1 + N
        # answers each with their own greeting.
        sub_results: list[dict] = sub_results_out if sub_results_out is not None else []
        for sq, result in zip(sub_queries, raw_results):
            if isinstance(result, BaseException):
                # Defensive: _run_sub_agent shouldn't raise. If it ever does,
                # fall back to a canonical-shape failure record so post_orchestration
                # handlers don't trip over a missing plugin_data/steps key.
                logger.error(
                    f"Orchestrator: sub-agent [{sq.get('role','?')}] raised "
                    f"unexpectedly (sink contract violated): {result!r}"
                )
                result = _failed_sub_result(
                    sq.get("role") if isinstance(sq, dict) else "?",
                    sq.get("query") if isinstance(sq, dict) else "",
                    error=str(result),
                )

            if result.get("error"):
                yield AgentStep(
                    step_number=0,
                    step_type="error",
                    content=f"Sub-Agent [{result['role']}] fehlgeschlagen: {result['error']}",
                )

            for step in result["steps"]:
                if step.step_type == "final_answer":
                    continue
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
        """Run sub-agents sequentially.

        Delegates to ``_run_sub_agent`` per query so hooks fire
        identically to parallel mode. Exception isolation is also
        identical: ``_run_sub_agent`` is the sink and returns a
        canonical-shape failure record (with ``error`` set) instead of
        propagating, so a single failed sub-agent does not abort the
        sequential run.
        """
        from services.agent_service import AgentStep

        sub_results: list[dict] = sub_results_out if sub_results_out is not None else []

        for i, sq in enumerate(sub_queries):
            role_preview = sq.get("role", "?") if isinstance(sq, dict) else "?"
            query_preview = sq.get("query", "") if isinstance(sq, dict) else ""
            logger.info(
                f"Orchestrator: running sub-agent {i+1}/{len(sub_queries)} "
                f"[{role_preview}]: {query_preview[:60]}"
            )
            result = await self._run_sub_agent(sq, ollama, executor, lang, **agent_kwargs)

            if result.get("error"):
                yield AgentStep(
                    step_number=0,
                    step_type="error",
                    content=f"Sub-Agent [{result['role']}] fehlgeschlagen: {result['error']}",
                )

            # Suppress per-sub-agent final_answer — only the synthesizer's
            # combined answer reaches the user.
            for step in result["steps"]:
                if step.step_type == "final_answer":
                    continue
                yield step

            sub_results.append(result)

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
        """Combine sub-results into a unified answer via LLM.

        Plugin extension points:

        - ``build_synthesis_context`` — fires before the prompt is built.
          Plugins return a text block to append to ``collected_data`` so
          the synthesizer can reference plugin-specific context (e.g.
          Reva's contacts engine emits a ``<contacts>...</contacts>``
          block for the synthesizer to mention contact persons in prose).
          First non-None result wins.

        - ``synthesis_prompt_override`` — fires after ``collected_data``
          is finalized. Plugins return a fully-templated synth prompt
          to replace Renfield's default. First non-None result wins.

        - Source-line stripping is applied unconditionally to the synth
          output: ``_Quelle: ..._`` / ``_Source: ..._`` lines are removed
          (DE+EN, italic markers tolerated). Transports/plugins that
          attach their own canonical source footer expect the LLM not to
          duplicate it; this is generic enough to be a default.
        """
        from utils.hooks import run_hooks

        results_text = "\n".join(
            f"- [{r['role']}] {r['query']}: {r['answer']}"
            for r in sub_results
        )

        # Hook: plugins may append plugin-specific context to the
        # collected sub-results before the synthesizer sees them.
        context_results = await run_hooks(
            "build_synthesis_context",
            message=message,
            sub_results=sub_results,
            lang=lang,
        )
        for cr in context_results:
            if isinstance(cr, str) and cr:
                results_text = f"{results_text}\n\n{cr}"
                break

        # Hook: plugins may override the synthesizer prompt entirely.
        synthesize_prompt: str | None = None
        override_results = await run_hooks(
            "synthesis_prompt_override",
            message=message,
            collected_data=results_text,
            lang=lang,
        )
        for op in override_results:
            if isinstance(op, str) and op:
                synthesize_prompt = op
                break

        if synthesize_prompt is None:
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
            answer = extract_response_content(raw_response) or None
            return _strip_source_line(answer) if answer else None

        except Exception as e:
            logger.warning(f"Orchestrator synthesis failed: {e}")
            # Fallback: concatenate
            return "\n\n".join(r["answer"] for r in sub_results if r["answer"])
