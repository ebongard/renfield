#!/usr/bin/env python3
"""Measures the agent ROUTER against a real model instead of guessing.

The router is one LLM call (`AgentRouter.classify`). A rule that reads
"obviously better" has measured worse in this project before, on the tool
pre-selection (`bin/measure-preselect.py` tells that story). This is the same
bench one layer earlier: which ROLE does a message get.

What it compares
----------------
    BEFORE  no pairing registered -- the prompt from before the peer rule
    AFTER   a pairing, no name allowed into the router prompt (`peer_rule`)
    NAMED   a pairing, names allowed (`peer_rule_named`, --instances;
            FEDERATION_PEER_NAME_IN_ROUTER_PROMPT_ENABLED)
    CLASH   like NAMED, plus an instance that is called like a PERSON
            (--clash). Played only for the cases marked `"clash": true` and
            not scored: it shows where "frag <Name>, ob ..." goes when the
            name is both. Nothing in the rule can tell the two apart.

🛑 What it does NOT measure
---------------------------
Only the LLM classify layer. In production a turn passes the role hint, the
entity-id route, the continuity score and the SEMANTIC fast path BEFORE this
call -- each of them can decide the turn without the model ever seeing the
rule -- and the cross-MCP planner, the tool pre-selection and the agent
AFTER it. A 100 % here is not "the question reaches the peer"; it is "when
the router model is asked, it names a role that can ask".

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
    # The routing switch -- the tool-description switch is a different one
    # and does not open the router prompt.
    settings.federation_peer_name_in_router_prompt_enabled = bool(names)
    await sync_peers(manager, db)
    return AgentRouter(roles_config, mcp_manager=manager)


def _hit(case: dict, role: str, peer_roles: set[str], rule_role: str | None) -> bool:
    """`expect`: one of these roles (`@peer` = any role that can ask a peer).
    `expect_not_rule`: anything but the role the peer rule names -- for a
    message that asks a PERSON, where several roles are acceptable and only
    being pulled to the far instance is wrong."""
    if case.get("expect_not_rule"):
        return role not in (rule_role, "FALLBACK")
    expect = case["expect"]
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
    ap.add_argument("--clash", default="Kim",
                    help="a person's name the CLASH variant adds as an instance name")
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
        "CLASH": await _router(roles_config, paired=True, names=[*names, args.clash]),
    }
    rule_role = routers["NAMED"].federation_role_name()
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
        if label != "CLASH" or cases[index].get("clash")
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
          f"reps={args.reps} peer-capable roles={sorted(peer_roles)} rule names={rule_role}")
    print("LLM CLASSIFY LAYER ONLY -- not end to end: production runs the semantic "
          "fast path (and role hint, entity id, continuity) before it, the planner, "
          "the tool pre-selection and the agent after it.\n")
    print(f"{'kind':8} {'expect':24} " + " ".join(f"{label:28}" for label in routers) + " task")
    for index, case in enumerate(cases):
        kind = "peer" if case.get("expect") == PEER else "control"
        cells = []
        for label in routers:
            counts = seen[(label, index)]
            if not counts:
                cells.append("-".ljust(28))
                continue
            hits = sum(c for role, c in counts.items() if _hit(case, role, peer_roles, rule_role))
            # The name-free variant cannot know a bare instance name, and
            # CLASH has no right answer.
            scored = label != "CLASH" and not (label == "AFTER" and case.get("needs_name"))
            if scored:
                totals[(label, kind)][0] += hits
                totals[(label, kind)][1] += args.reps
            got = ",".join(f"{role}x{c}" for role, c in counts.most_common())
            cells.append(f"{hits}/{args.reps}{'' if scored else '*'} {got}"[:28].ljust(28))
        expect = (
            f"not {rule_role} (rule)" if case.get("expect_not_rule")
            else case["expect"] if isinstance(case["expect"], str)
            else "|".join(case["expect"])
        )
        print(f"{kind:8} {expect[:24]:24} " + " ".join(cells) + f" {case['task']}")
    print("\n* not scored: the name-free rule cannot know a bare instance name; "
          "CLASH (an instance named like a person) has no right answer")
    for kind in ("peer", "control"):
        print(f"{kind:8} " + "  ".join(
            f"{label} {totals[(label, kind)][0]}/{totals[(label, kind)][1]}"
            for label in routers if label != "CLASH"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
