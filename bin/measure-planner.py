#!/usr/bin/env python3
"""Measures the cross-MCP PLANNER against a real model instead of guessing.

The planner (`QueryOrchestrator.detect_multi_domain`) decides whether a turn
is split into sub-queries for several roles. Since the router sends a
question addressed to a paired instance to the federation role, such a turn
reaches the planner -- and a planner that splits "frag <Instanz>, welche
Termine anstehen und wer zuhause ist" by topic hands the presence half to a
role that answers from THIS house. The planner prompt carries a rule against
that. This bench says whether the model follows it, and whether the rule
stops the planner from splitting what it should split.

What it compares
----------------
    BEFORE  no pairing registered -- the planner prompt from before the rule
    AFTER   a pairing, no name in the routing prompts (`orchestrator_peer_rule`)
    NAMED   a pairing, names allowed (`orchestrator_peer_rule_named`,
            --instances; FEDERATION_PEER_NAME_IN_ROUTER_PROMPT_ENABLED)

What counts
-----------
    peer    a multi-topic question to an instance. Right: NOT spread over
            roles that cannot ask -- no plan at all, or every sub-task for
            the federation role.
    split   an ordinary request for two domains. Right: still a plan with at
            least two roles, the expected ones among them.
    single  one domain. Right: no plan.

Nothing is reconstructed: the real `detect_multi_domain` builds the prompt
from `prompts/agent.yaml` and `config/agent_roles.yaml`, sends it with its
real options (typed plan schema on an OpenAI-compatible endpoint, as in
production) and parses the answer with its real parser.

🛑 `--url` and `--model` have NO default
---------------------------------------
The planner runs on the agent tier -- in production the MAIN model. Point
this ONLY at an endpoint its owner approved for it. A URL containing `/v1`
is used the way production uses llama-server (OpenAI-compatible, typed
plan); anything else as an Ollama endpoint. Every fallback is switched off:
a bench that silently measured the fallback model would be worse than none.

    docker exec -w /<copy>/src/backend -e PYTHONPATH=/<copy>/src/backend \\
        renfield-backend python /<copy>/bin/measure-planner.py \\
        --url http://<approved-endpoint>/v1 --model <model> --reps 3

`--dry-run` prints the three planner prompts and calls nothing.

🛑 What it does NOT measure: the planner call alone. Whether the turn got to
the planner (router), and what the sub-agents then do, is not in it.

A failed call is reported as ERROR, never as "no plan": the planner swallows
timeouts and connection errors and returns None, which would read as a hit.

The jobs are INTERLEAVED and shuffled across variants and repetitions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
NO_PLAN = "none"
ERROR = "ERROR"


class _Recorder:
    """Stands between the planner and its client: keeps the prompt, and knows
    whether an answer really came back. With no client it answers "one
    domain" itself -- the dry run."""

    def __init__(self, client=None):
        self.client = client
        self.prompt = ""
        self.answered = False

    async def chat(self, **kwargs):
        self.answered = False
        self.prompt = kwargs["messages"][0]["content"]
        if self.client is None:
            response = SimpleNamespace(message=SimpleNamespace(content="null"))
        else:
            response = await self.client.chat(**kwargs)
        self.answered = True
        return response


async def _planner(roles_config: dict, *, paired: bool, names: list[str], model: str | None):
    """A real planner over a real router and manager; only the pairing rows
    are stand-ins."""
    from services.agent_router import AgentRouter
    from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState
    from services.orchestrator import QueryOrchestrator
    from services.peer_mcp_registry import sync_peers
    from utils.config import settings

    manager = MCPManager()
    # Every server a role names counts as connected, as on a healthy instance.
    for role in roles_config.get("roles", {}).values():
        for server in (role.get("mcp_servers") or []):
            state = MCPServerState(config=MCPServerConfig(name=server))
            state.connected = True
            manager._servers[server] = state
    rows = []
    if paired:
        rows = [
            SimpleNamespace(id=i, remote_pubkey=f"{i:02x}" * 32,
                            remote_display_name="alex", remote_instance_name=name)
            for i, name in enumerate(names or [None], start=1)
        ]
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    settings.federation_peer_name_in_router_prompt_enabled = bool(names)
    await sync_peers(manager, db)
    router = AgentRouter(roles_config, mcp_manager=manager)
    # The planner takes model and URL from the FIRST role with an agent loop.
    # The bench decides both, so no per-role override may win.
    for role in router.roles.values():
        role.model = model
        role.ollama_url = None
    return QueryOrchestrator(router, manager)


def _hit(case: dict, plan: str, rule_role: str | None) -> bool:
    if plan == ERROR:
        return False
    roles = set() if plan == NO_PLAN else set(plan.split("+"))
    if case["kind"] == "peer":
        return roles <= {rule_role}
    if case["kind"] == "split":
        return len(roles) >= 2 and set(case.get("roles", [])) <= roles
    return plan == NO_PLAN


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="APPROVED endpoint serving the planner model (no default)")
    ap.add_argument("--model", help="planner model name (no default)")
    ap.add_argument("--cases", default=str(ROOT / "bin" / "planner-cases-peer.json"))
    ap.add_argument("--roles", default=str(ROOT / "config" / "agent_roles.yaml"))
    ap.add_argument("--instances", default="Nordhaus,Suedhaus",
                    help="names the NAMED variant lists; must match the cases")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261115)
    ap.add_argument("--timeout", type=float,
                    help="seconds per call (default: the configured router timeout)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dry_run and not (args.url and args.model):
        ap.error("--url and --model are both required (or use --dry-run)")

    import services.orchestrator as orchestrator_module
    from services.agent_router import load_roles_config
    from utils import llm_client
    from utils.config import settings

    # One endpoint, no way around it.
    settings.ollama_fallback_url = ""
    settings.llm_openai_fallback_enabled = False
    openai_style = bool(args.url) and "/v1" in args.url
    settings.llm_openai_base_url = args.url if openai_style else None
    if openai_style:
        settings.llm_openai_model = args.model
        settings.llm_openai_for_agent = True
    # Truthy in both modes: without a planner URL the planner would use the
    # `ollama` object it is handed instead of resolving its own client.
    settings.agent_ollama_url = args.url or "http://dry-run.invalid"
    if args.timeout:
        settings.agent_router_timeout = args.timeout
    llm_client.clear_client_cache()

    real_get_agent_client = orchestrator_module.get_agent_client
    recorder = _Recorder()

    def _recorded_client(*a, **kw):
        if args.dry_run:
            return recorder, "dry-run"
        client, url = real_get_agent_client(*a, **kw)
        recorder.client = client
        return recorder, url

    orchestrator_module.get_agent_client = _recorded_client

    roles_config = load_roles_config(args.roles)
    names = [n.strip() for n in args.instances.split(",") if n.strip()]
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    model = args.model or "dry-run"
    planners = {
        "BEFORE": await _planner(roles_config, paired=False, names=[], model=model),
        "AFTER": await _planner(roles_config, paired=True, names=[], model=model),
        "NAMED": await _planner(roles_config, paired=True, names=names, model=model),
    }
    rule_role = planners["NAMED"].router.federation_role_name()
    ollama = SimpleNamespace(client=recorder)

    if args.dry_run:
        for label, planner in planners.items():
            await planner.detect_multi_domain(cases[0]["task"], ollama, lang="de")
            print(f"===== {label} (de) =====")
            print(recorder.prompt)
        return 0

    jobs = [
        (label, index, rep)
        for label in planners for index in range(len(cases)) for rep in range(args.reps)
    ]
    random.Random(args.seed).shuffle(jobs)
    seen: dict[tuple[str, int], Counter] = defaultdict(Counter)
    for n, (label, index, _rep) in enumerate(jobs, start=1):
        case = cases[index]
        recorder.answered = False
        plan = await planners[label].detect_multi_domain(
            case["task"], ollama, lang=case.get("lang", "de"))
        if not recorder.answered:
            outcome = ERROR
        elif not plan:
            outcome = NO_PLAN
        else:
            outcome = "+".join(sq["role"] for sq in plan)
        seen[(label, index)][outcome] += 1
        print(f"\r{n}/{len(jobs)}", end="", file=sys.stderr)
    print(file=sys.stderr)

    totals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    print(f"model={args.model} url-style={'openai' if openai_style else 'ollama'} "
          f"reps={args.reps} federation role={rule_role}")
    print("PLANNER CALL ONLY -- not end to end: the router decides before it "
          "whether a turn gets here, the sub-agents act after it.\n")
    print(f"{'kind':7} " + " ".join(f"{label:34}" for label in planners) + " task")
    for index, case in enumerate(cases):
        cells = []
        for label in planners:
            counts = seen[(label, index)]
            hits = sum(c for plan, c in counts.items() if _hit(case, plan, rule_role))
            # The name-free rule cannot know a bare instance name.
            scored = not (label == "AFTER" and case.get("needs_name"))
            if scored:
                totals[(label, case["kind"])][0] += hits
                totals[(label, case["kind"])][1] += args.reps
            got = ",".join(f"{plan}x{c}" for plan, c in counts.most_common())
            cells.append(f"{hits}/{args.reps}{'' if scored else '*'} {got}"[:34].ljust(34))
        print(f"{case['kind']:7} " + " ".join(cells) + f" {case['task']}")
    print("\n* not scored: the name-free rule cannot know a bare instance name")
    for kind in ("peer", "split", "single"):
        print(f"{kind:7} " + "  ".join(
            f"{label} {totals[(label, kind)][0]}/{totals[(label, kind)][1]}"
            for label in planners))
    errors = sum(c[ERROR] for c in seen.values())
    if errors:
        print(f"\n🛑 {errors} call(s) did not come back -- those rows are not a measurement")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
