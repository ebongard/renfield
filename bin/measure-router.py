#!/usr/bin/env python3
"""Measures the agent ROUTER against a real model instead of guessing.

The router is one LLM call (`AgentRouter.classify`). A rule that reads
"obviously better" has measured worse in this project before, on the tool
pre-selection (`bin/measure-preselect.py` tells that story). This is the same
bench one layer earlier: which ROLE does a message get.

What it compares
----------------
    BEFORE  no pairing registered -- the prompt from before the peer rule
    AFTER   a pairing, no name allowed into prompts (`peer_rule`)
    NAMED   a pairing, names allowed (`peer_rule_named`, --instances)

Nothing is reconstructed: the prompt is built by the real `AgentRouter` from
the real `config/agent_roles.yaml` and `prompts/router.yaml`, sent with the
real options, and the answer parsed by the real parser.

Where it runs
-------------
Inside the backend image, from an isolated copy (the test procedure in
CLAUDE.md), because it imports the backend:

    docker exec -w /<copy>/src/backend -e PYTHONPATH=/<copy>/src/backend \\
        renfield-backend python /<copy>/bin/measure-router.py \\
        --url http://<ollama>:11434 --model qwen3:8b --reps 3

🛑 `--url` has no default. The router model in production is the INTENT tier
on the in-cluster Ollama; point this at an endpoint that serves the same model
and that you are allowed to load -- a model loaded next to the main model on
the GPU host competes with it for memory.

`--dry-run` prints the three category blocks and calls nothing.

The jobs are INTERLEAVED and shuffled across variants and repetitions: run in
blocks, a server's prefix cache feigns stability.
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
PEER = "@peer"


async def _router(roles_config: dict, *, paired: bool, names: list[str]):
    """A real router over a real manager; only the pairing rows are stand-ins."""
    from services.agent_router import AgentRouter
    from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState
    from services.peer_mcp_registry import sync_peers
    from utils.config import settings

    manager = MCPManager()
    # Every server a role names counts as connected, as on a healthy instance:
    # a role that drops out here would silently change the category list.
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
    settings.federation_peer_name_in_tool_description_enabled = bool(names)
    await sync_peers(manager, db)
    return AgentRouter(roles_config, mcp_manager=manager)


def _hit(expect, role: str, peer_roles: set[str]) -> bool:
    if expect == PEER:
        return role in peer_roles
    return role in (expect if isinstance(expect, list) else [expect])


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="Ollama / OpenAI-compatible endpoint serving the router model")
    ap.add_argument("--model", help="router model (default: the configured intent model)")
    ap.add_argument("--cases", default=str(ROOT / "bin" / "router-cases-peer.json"))
    ap.add_argument("--roles", default=str(ROOT / "config" / "agent_roles.yaml"))
    ap.add_argument("--instances", default="Nordhaus,Suedhaus",
                    help="names the NAMED variant lists; must match the cases")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261115)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dry_run and not args.url:
        ap.error("--url is required (or use --dry-run)")

    from services.agent_router import load_roles_config
    from utils.config import settings
    from utils.llm_client import get_dedicated_client

    roles_config = load_roles_config(args.roles)
    names = [n.strip() for n in args.instances.split(",") if n.strip()]
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    routers = {
        "BEFORE": await _router(roles_config, paired=False, names=[]),
        "AFTER": await _router(roles_config, paired=True, names=[]),
        "NAMED": await _router(roles_config, paired=True, names=names),
    }
    peer_roles = {
        name for name, role in routers["NAMED"].roles.items()
        if role.has_agent_loop and (role.federation or role.mcp_servers is None)
    }

    if args.dry_run:
        for label, router in routers.items():
            print(f"===== {label} (de) =====")
            print(router._build_role_descriptions("de") + router._peer_rule("de"))
        return 0

    # The router takes its own endpoint whenever one is configured, so the
    # `ollama` it is handed only has to exist.
    settings.agent_router_url = args.url
    if args.model:
        settings.agent_router_model = args.model
    ollama = SimpleNamespace(client=get_dedicated_client(args.url))

    jobs = [
        (label, index, rep)
        for label in routers for index in range(len(cases)) for rep in range(args.reps)
    ]
    random.Random(args.seed).shuffle(jobs)
    seen: dict[tuple[str, int], Counter] = defaultdict(Counter)
    for n, (label, index, _rep) in enumerate(jobs, start=1):
        case = cases[index]
        role = await routers[label].classify(case["task"], ollama, lang=case.get("lang", "de"))
        # A fallback is the router failing, not the model choosing `general`.
        seen[(label, index)]["FALLBACK" if role.route_layer == "fallback" else role.name] += 1
        print(f"\r{n}/{len(jobs)}", end="", file=sys.stderr)
    print(file=sys.stderr)

    totals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    print(f"model={settings.agent_router_model or settings.ollama_intent_model} "
          f"reps={args.reps} peer-capable roles={sorted(peer_roles)}\n")
    print(f"{'kind':8} {'expect':24} " + " ".join(f"{label:28}" for label in routers) + " task")
    for index, case in enumerate(cases):
        kind = "peer" if case["expect"] == PEER else "control"
        cells = []
        for label in routers:
            counts = seen[(label, index)]
            hits = sum(c for role, c in counts.items() if _hit(case["expect"], role, peer_roles))
            # The name-free variant cannot know a bare instance name.
            scored = not (label == "AFTER" and case.get("needs_name"))
            if scored:
                totals[(label, kind)][0] += hits
                totals[(label, kind)][1] += args.reps
            got = ",".join(f"{role}x{c}" for role, c in counts.most_common())
            cells.append(f"{hits}/{args.reps}{'' if scored else '*'} {got}"[:28].ljust(28))
        expect = case["expect"] if isinstance(case["expect"], str) else "|".join(case["expect"])
        print(f"{kind:8} {expect[:24]:24} " + " ".join(cells) + f" {case['task']}")
    print("\n* not scored: the name-free rule cannot know a bare instance name")
    for kind in ("peer", "control"):
        print(f"{kind:8} " + "  ".join(
            f"{label} {totals[(label, kind)][0]}/{totals[(label, kind)][1]}" for label in routers))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
