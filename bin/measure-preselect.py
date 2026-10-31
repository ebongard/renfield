#!/usr/bin/env python3
"""Measures the tool pre-selection against the REAL prompt instead of guessing.

Why this exists
---------------
On 2026-10-27 "scanne nach x-idra sucht Dokumente" cost eight rounds of
measurement. Four review rounds before that had worked on the hint text without
ever measuring whether the change had any effect. Two versions that looked
"obviously better" measured worse (70 % and 74 % against 89 %); a third made
the reported case worse, from 3/3 to 0/3. Without a bench any of them could
have shipped.

🛑 The measurement runs against the REAL prompt, not against a reconstruction.
On 2026-10-26 I took a reconstruction in the pod for reality -- it differed.

How to get at the real prompt
-----------------------------
1. `AGENT_PRESELECT_PROMPT_DUMP: "true"` in `k8s/configmap.yaml`, commit,
   `kubectl apply`, roll the backend. (Dark, because the prompt carries the
   user's message in full -- switch it back off immediately afterwards.)
2. Make one turn, then fetch the line:
       kubectl -n renfield logs <backend-pod> --since=10m \
         | grep "Tool pre-selection PROMPT"
3. Write the `{!r}` part into a file with `ast.literal_eval`.
4. `--verify-signature` checks that the file is the prompt from the log: the
   signature has to match the `(in=...)` of the log line. Only then is the
   measurement a measurement.

Variants
--------
A variant is a complete prompt file. It is built outside this script, so the
script does not have to learn a patch language:

    python3 - <<'PY'
    p = open("dump.txt").read()
    anker = "Startet einen NEUEN Scan: Papier aus dem Einzug einlesen."
    open("variante.txt","w").write(p.replace(anker, anker + " " + NEUER_TEXT, 1))
    PY

🛑 What was measured is what ships -- byte for byte. A variant that was spliced
into the middle of a sentence must not travel into the YAML as clean German
without measuring the clean version again.

Example
-------
    bin/measure-preselect.py \
        --variant IST=dump.txt --variant NEU=variante.txt \
        --reps 3 --verify-signature 7ebd68f5

The jobs are INTERLEAVED and worked through in shuffled order. Run in blocks
one after another, llama-server's KV cache benefits and feigns stability -- a
block of 15 delivered 15 identical answers that way, while the same case
fluctuated when spread across the bench.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import urllib.request
from collections import defaultdict

# Has to match `_DEFAULT_LLM_OPTIONS_TOOL_PRESELECT` in
# services/agent_service.py and `get_classification_chat_kwargs` -- otherwise
# the bench measures something other than production. `num_ctx` is missing on
# purpose: the OpenAI path does not map it (see `_options_to_openai`).
REQUEST_OPTIONS = {"temperature": 0, "max_tokens": 512,
          "chat_template_kwargs": {"enable_thinking": False}}

# The sentence that kept tipping over in the paper/search branch.
# `expect: "treffer"` means `--tool` MUST be chosen; "kein-treffer" means it
# must NOT be.
DEFAULT_CASES = [
    # A target after "scanne nach <X>" -- no object in between
    {"task": "scanne nach x-idra", "expect": "treffer"},
    {"task": "scanne nach X-IDRA", "expect": "treffer"},
    {"task": "scanne nach xidra", "expect": "treffer"},
    {"task": "scanne nach household", "expect": "treffer"},
    {"task": "scanne nach Haushalt", "expect": "treffer"},
    {"task": "scanne nach foo-bar", "expect": "treffer"},
    {"task": "scanne das Dokument", "expect": "treffer"},
    {"task": "scanne die Seiten", "expect": "treffer"},
    {"task": "einscannen nach x-idra", "expect": "treffer"},
    # Paper as the object: unread paper has no searchable content
    {"task": "scanne die Seiten nach x-idra", "expect": "treffer"},
    {"task": "scanne die Seiten nach Haushalt", "expect": "treffer"},
    # Negative controls: explicit search verbs
    {"task": "suche nach x-idra", "expect": "kein-treffer"},
    {"task": "finde Dokumente von x-idra", "expect": "kein-treffer"},
    {"task": "zeig mir die Rechnungen von x-idra", "expect": "kein-treffer"},
    {"task": "welche Dokumente gibt es zu x-idra", "expect": "kein-treffer"},
    # Negative controls: an object with readable text between the verb and "nach"
    {"task": "scanne das Dokument nach x-idra", "expect": "kein-treffer"},
    {"task": "scanne x-idra nach Rechnungen", "expect": "kein-treffer"},
    {"task": "scanne die Rechnung nach dem Betrag", "expect": "kein-treffer"},
    {"task": "durchsuche das Dokument nach x-idra", "expect": "kein-treffer"},
]


def signature(prompt: str) -> str:
    """Like `agent_service.preselect_input_signature` -- the FINISHED prompt."""
    return hashlib.sha1(prompt.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def find_task(prompt: str) -> str:
    treffer = re.search(r'^Aufgabe: "(.*)"$', prompt, re.M) or \
              re.search(r'^Task: "(.*)"$', prompt, re.M)
    if not treffer:
        sys.exit("No `Aufgabe:`/`Task:` line found — pass --original-task.")
    return treffer.group(1)


def replace_task(prompt: str, old: str, new: str) -> str:
    """Swaps the task -- exclusively in the `Aufgabe:`/`Task:` line.

    🛑 A bare `prompt.replace(old, new)` has two silent failures, and both
    look like a result:
    - if `old` is absent from one variant, EVERY case measures the same prompt,
      and the output is full of clean 5/5 or 0/5;
    - if `old` happens to appear in a tool description too, the list gets
      changed along with it and something other than the intent is measured.
    Hence line-exact -- and a failure aborts instead of keeping quiet.
    """
    lines = prompt.split("\n")
    matched = 0
    for i, line in enumerate(lines):
        if line.startswith(("Aufgabe: ", "Task: ")) and old in line:
            lines[i] = line.replace(old, new)
            matched += 1
    if matched != 1:
        raise ValueError(
            f"Aufgabe {old!r} steht {matched}x in einer `Aufgabe:`/`Task:`-Zeile "
            "— erwartet genau einmal. Falsche Datei oder falsches --original-task?")
    return "\n".join(lines)


def tool_names(prompt: str) -> list:
    """The offered tool names, taken from the prompt -- for the `enum`.

    🛑 `re.split(r"(?m)^- ")` and NOT `splitlines()`: an entry runs across
    line breaks, because the hint texts contain some. This cut produced a false
    finding twice on 2026-10-27.
    """
    names = []
    for chunk in re.split(r"(?m)^- ", prompt):
        head = chunk.split(":", 1)[0].strip()
        if head.startswith(("mcp.", "internal.")):
            names.append(head)
    return names


def ask(url: str, model_name: str, prompt: str, timeout_s: float, use_schema: bool = False) -> list:
    payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}],
                **REQUEST_OPTIONS}
    if use_schema:
        # A root array, so the PROMPT stays unchanged: that way the two
        # measurements differ in exactly one variable.
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "preselect", "strict": True, "schema": {
                # `maxItems` is mandatory: `uniqueItems` is NOT enforced, so
                # otherwise the model repeats a valid name until the token
                # budget is used up and the answer comes back truncated. Has to
                # match `_build_preselect_schema`.
                "type": "array", "maxItems": 8, "uniqueItems": True,
                "items": {"type": "string", "enum": tool_names(prompt)},
            }},
        }
    http_request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(http_request, timeout=timeout_s) as response:
        text = json.loads(response.read())["choices"][0]["message"]["content"]
    tools = json.loads(text.strip())
    if not isinstance(tools, list):
        raise ValueError(f"keine Liste: {text[:80]!r}")
    return tools


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", action="append", required=True, metavar="LABEL=PFAD",
                   help="Prompt-Datei je Fassung; mehrfach angebbar")
    # 🛑 `bin/preselect-cases-peer.json` lives in the repo so the federation
    # measurement is repeatable. The default cases below cover the
    # paper/search branch; the peer cases cover "frag Xs renfield" /
    # "was liegt in <Instanz>" / "in allen renfields" together with the
    # negative controls on which the decision against a global limit was made
    # on 2026-10-29:
    #   --cases bin/preselect-cases-peer.json --tool query_brain
    p.add_argument("--cases", metavar="PFAD",
                   help="JSON-Liste [{task, expect}]; ohne Angabe die Standardfaelle")
    p.add_argument("--tool", default="scan_document",
                   help="Teilzeichenkette, die als Treffer zaehlt (Vorgabe: scan_document)")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--url", default="http://cuda.local:8081/v1/chat/completions")
    p.add_argument("--model", default="qwen3.6")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--original-task", help="Aufgabe, die in den Dateien steht")
    p.add_argument("--typed", action="store_true",
                   help="Antwort an ein JSON-Schema binden (Wurzel-Array, enum "
                        "ueber die angebotenen Namen) — Prompt bleibt identisch")
    p.add_argument("--verify-signature", metavar="HEX8",
                   help="bricht ab, wenn eine Datei nicht diese Signatur hat")
    a = p.parse_args()

    versions: dict[str, str] = {}
    for entry in a.variant:
        if "=" not in entry:
            sys.exit(f"--variant needs LABEL=PATH, got {entry!r}")
        label, path = entry.split("=", 1)
        with open(path) as fh:
            versions[label] = fh.read()

    if a.cases:
        with open(a.cases) as fh:
            case_list = json.load(fh)
    else:
        case_list = DEFAULT_CASES
    allowed = {"treffer", "kein-treffer"}
    for case_no, case in enumerate(case_list, 1):
        if case.get("expect") not in allowed:
            # 🛑 Without this check a typo counts silently as "kein-treffer"
            # and inverts the case's verdict.
            sys.exit(f"Case {case_no} ({case.get('task')!r}): expect="
                     f"{case.get('expect')!r}, allowed are {sorted(allowed)}.")
    original = a.original_task or find_task(next(iter(versions.values())))

    # 🛑 Every version has to carry the task BEFORE 100+ calls run.
    for label, text in versions.items():
        try:
            replace_task(text, original, original)
        except ValueError as e:
            sys.exit(f"Version {label!r}: {e}")

    print(f"  Task in the files: {original!r}")
    for label, text in versions.items():
        sig = signature(text)
        marker = ""
        if a.verify_signature:
            # 🛑 Only the UNCHANGED version can carry the signature from the
            # log; a variant is by definition a different prompt. Hence a
            # warning, not an abort -- unless it matches NOWHERE, in which case
            # the wrong file was measured.
            marker = "  <= matches the log" if sig == a.verify_signature else ""
        print(f"    {label:12s} {len(text):6d} chars, signature {sig}{marker}")
    if a.verify_signature and not any(
            signature(t) == a.verify_signature for t in versions.values()):
        sys.exit(f"\n  ABORT: no file has the signature {a.verify_signature}. "
                 "The bench would then not measure the production prompt.")
    print()

    jobs = [(label, f["task"], f["expect"], i)
                 for label in versions for f in case_list for i in range(a.reps)]
    random.Random(a.seed).shuffle(jobs)

    treffer: dict = defaultdict(int)
    runs: dict = defaultdict(int)
    errors = 0
    for n, (label, task, expected, _) in enumerate(jobs, 1):
        prompt = replace_task(versions[label], original, task)
        try:
            tools = ask(a.url, a.model, prompt, a.timeout, a.typed)
        except Exception as e:
            errors += 1
            print(f"  ERROR {label}/{task!r}: {e}", file=sys.stderr)
            continue
        matched = any(a.tool in str(w) for w in tools)
        runs[(label, task)] += 1
        if matched == (expected == "treffer"):
            treffer[(label, task)] += 1
        if n % max(1, len(jobs) // 4) == 0:
            print(f"  … {n}/{len(jobs)}", flush=True)

    labels = list(versions)
    head = "  " + "Task".ljust(38) + "expect".ljust(7) + \
           "".join(lbl.rjust(9) for lbl in labels)
    print("\n" + head)
    for f in case_list:
        line = f"  {f['task']:38s}{'YES' if f['expect'] == 'treffer' else 'NO':7s}"
        for lbl in labels:
            line += f"{treffer[(lbl, f['task'])]}/{runs[(lbl, f['task'])]}".rjust(9)
        print(line)
    print()
    for lbl in labels:
        correct = sum(v for (x, _), v in treffer.items() if x == lbl)
        total = sum(v for (x, _), v in runs.items() if x == lbl)
        rate = 100 * correct / max(total, 1)
        print(f"  {lbl}: {correct}/{total} correct ({rate:.0f} %)")
    if errors:
        print(f"\n  🛑 {errors} call(s) failed — the rates are incomplete.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
