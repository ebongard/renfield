#!/usr/bin/env python3
"""Subsume recall-loss eval — Structured Memory Phase 3-subsume watch.

Measures the loss surface of MEMORY_SUBSUME_TO_KG by running BOTH extractors
(memory v1 + conversation KG) on the SAME conversation and, per case, deciding:

    subsumed := the memory extractor produced any item with
                (category == "fact" AND subject != null)   -> the flat store
                would be SKIPPED under MEMORY_SUBSUME_TO_KG
    captured := the KG extractor produced a relation whose SUBJECT entity name
                matches (case-insensitively) one of the subsumed subjects
    LOSS     := subsumed AND NOT captured

A LOSS case is a fact the assistant would silently forget once subsume is on.

Two layers (mirrors run_kg_extraction_eval.py):
  - ``classify_case(memory_items, kg_entities, kg_relations)`` — a PURE function
    (no LLM, no DB) computing (subsumed, captured, lost) from already-extracted
    structures. Unit-tested in tests/eval/test_subsume_recall_loss_runner.py.
  - ``run_case`` — runs the two real extraction prompts + LLM + parse, then
    classify_case. Needs Ollama, so it runs on-demand (not in the unit suite).

NOT a CI test. Run in a backend pod against the prod models:
    kubectl -n renfield exec deploy/backend -c backend -- \\
      python bin/run_subsume_recall_loss_eval.py /tests/eval/subsume_recall_loss_eval.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
if _BACKEND.exists() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# In the prod container the backend modules live at the image WORKDIR (/app),
# not under src/backend — make that importable too.
for _cand in ("/app", str(Path.cwd())):
    if (Path(_cand) / "utils" / "config.py").exists() and _cand not in sys.path:
        sys.path.insert(0, _cand)


def _norm(s) -> str:
    return str(s or "").strip().lower()


def classify_case(
    memory_items: list[dict],
    kg_entities: list[dict],
    kg_relations: list[dict],
) -> dict:
    """Compute the subsume/capture/loss verdict for one case. Pure.

    Mirrors the production gate exactly:
        subsume iff item.category == "fact" and item.subject (truthy).
    A subsumed subject is "captured" iff some KG relation names it as the
    subject (the only place a subsumed fact about that person could survive).
    """
    subsumed_subjects = [
        _norm(it.get("subject"))
        for it in memory_items
        if _norm(it.get("category")) == "fact" and _norm(it.get("subject"))
    ]
    subsumed = bool(subsumed_subjects)

    rel_subjects = {_norm(r.get("subject")) for r in kg_relations}
    # captured iff EVERY subsumed subject has at least one KG relation as subject
    captured_subjects = [s for s in subsumed_subjects if s in rel_subjects]
    lost_subjects = [s for s in subsumed_subjects if s not in rel_subjects]
    captured = subsumed and not lost_subjects

    return {
        "subsumed": subsumed,
        "captured": captured,
        "lost": subsumed and not captured,
        "subsumed_subjects": subsumed_subjects,
        "lost_subjects": lost_subjects,
        "kg_relation_count": len(kg_relations),
        "kg_entity_count": len(kg_entities),
    }


def load_cases(fixture: Path) -> list[dict]:
    import yaml
    data = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    return data.get("cases", [])


async def _extract_memory(case: dict) -> list[dict]:
    from services.conversation_memory_service import ConversationMemoryService
    from services.prompt_manager import prompt_manager
    from utils.config import settings
    from utils.llm_client import extract_response_content, get_classification_chat_kwargs, get_default_client

    svc = ConversationMemoryService(None)  # parse helper only
    lang = case.get("lang", "de")
    prompt = prompt_manager.get(
        "memory", "extraction_prompt", lang=lang,
        user_message=case["user"], assistant_response=case.get("assistant", "Ok."),
    )
    system = prompt_manager.get("memory", "extraction_system", lang=lang)
    model = settings.memory_extraction_model or settings.ollama_model
    client = get_default_client()
    resp = await client.chat(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        options=prompt_manager.get_config("memory", "llm_options") or {},
        **get_classification_chat_kwargs(model),
    )
    return svc._parse_extraction_response(extract_response_content(resp)) or []


async def _extract_kg(case: dict) -> tuple[list[dict], list[dict]]:
    from services.knowledge_graph_service import KnowledgeGraphService
    from services.prompt_manager import prompt_manager
    from utils.config import settings
    from utils.llm_client import extract_response_content, get_classification_chat_kwargs, get_default_client

    svc = KnowledgeGraphService(None)  # parse helper only
    lang = case.get("lang", "de")
    speaker = case.get("speaker")
    prompt = prompt_manager.get(
        "knowledge_graph", "extraction_prompt", lang=lang,
        user_message=case["user"], assistant_response=case.get("assistant", "Ok."),
        speaker_clause=svc._build_speaker_clause(speaker, lang),
    )
    system = prompt_manager.get("knowledge_graph", "extraction_system", lang=lang)
    model = settings.kg_extraction_model or settings.ollama_model
    client = get_default_client()
    resp = await client.chat(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        options=prompt_manager.get_config("knowledge_graph", "llm_options") or {},
        **get_classification_chat_kwargs(model),
    )
    parsed = svc._parse_extraction_response(extract_response_content(resp)) or {}
    return parsed.get("entities", []), parsed.get("relations", [])


async def run_case(case: dict) -> dict:
    mem = await _extract_memory(case)
    ents, rels = await _extract_kg(case)
    return classify_case(mem, ents, rels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "fixture", nargs="?",
        default=str(Path(__file__).resolve().parents[1] / "tests/eval/subsume_recall_loss_eval.yaml"),
    )
    parser.add_argument("--case", default=None, help="run only the case with this id")
    args = parser.parse_args()

    import asyncio

    fixture = Path(args.fixture)
    if not fixture.exists():
        sys.exit(f"fixture not found: {fixture}")
    cases = load_cases(fixture)
    if args.case:
        cases = [c for c in cases if c.get("id") == args.case] or sys.exit(f"no case {args.case!r}")

    n_subsumed = n_captured = n_lost = n_unexpected = 0
    print(f"{'CASE':28s} {'SUBSUMED':9s} {'CAPTURED':9s} {'LOST':6s} {'EXPECT':8s}")
    print("-" * 78)
    for case in cases:
        v = asyncio.run(run_case(case))
        n_subsumed += int(v["subsumed"])
        n_captured += int(v["captured"])
        n_lost += int(v["lost"])
        loss_expected = bool(case.get("expect", {}).get("loss_expected", False))
        # An UNEXPECTED result = a control case that lost, or an expected-loss
        # case that was actually captured (good news, but worth flagging).
        flag = ""
        if v["lost"] and not loss_expected:
            flag = "  <-- UNEXPECTED LOSS (control case lost!)"
            n_unexpected += 1
        elif not v["lost"] and v["subsumed"] and loss_expected:
            flag = "  (recovered: KG captured it after all)"
        print(
            f"{case['id']:28s} {str(v['subsumed']):9s} {str(v['captured']):9s} "
            f"{str(v['lost']):6s} {str(loss_expected):8s}{flag}"
        )
        if v["lost_subjects"]:
            print(f"{'':28s}  lost subjects: {v['lost_subjects']} "
                  f"(kg rels={v['kg_relation_count']})")

    print("-" * 78)
    total = len(cases)
    print(f"cases={total}  subsumed={n_subsumed}  captured={n_captured}  LOST={n_lost}")
    if n_subsumed:
        print(f"capture rate (of subsumed): {n_captured}/{n_subsumed} "
              f"= {100*n_captured/n_subsumed:.0f}%")
    print(f"unexpected losses (control cases): {n_unexpected}")
    # Exit non-zero only on an UNEXPECTED loss — the eval documents the surface,
    # it isn't a pass/fail gate on the (expected) danger-zone losses.
    return 1 if n_unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
