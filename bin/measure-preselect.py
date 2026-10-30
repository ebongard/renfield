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
AUFRUF = {"temperature": 0, "max_tokens": 512,
          "chat_template_kwargs": {"enable_thinking": False}}

# The sentence that kept tipping over in the paper/search branch.
# `expect: "treffer"` means `--tool` MUST be chosen; "kein-treffer" means it
# must NOT be.
STANDARDFAELLE = [
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


def signatur(prompt: str) -> str:
    """Like `agent_service.preselect_input_signature` -- the FINISHED prompt."""
    return hashlib.sha1(prompt.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def aufgabe_finden(prompt: str) -> str:
    treffer = re.search(r'^Aufgabe: "(.*)"$', prompt, re.M) or \
              re.search(r'^Task: "(.*)"$', prompt, re.M)
    if not treffer:
        sys.exit("Keine `Aufgabe:`/`Task:`-Zeile gefunden — --original-task angeben.")
    return treffer.group(1)


def aufgabe_einsetzen(prompt: str, alt: str, neu: str) -> str:
    """Swaps the task -- exclusively in the `Aufgabe:`/`Task:` line.

    🛑 A bare `prompt.replace(alt, neu)` has two silent failures, and both
    look like a result:
    - if `alt` is absent from one variant, EVERY case measures the same prompt,
      and the output is full of clean 5/5 or 0/5;
    - if `alt` happens to appear in a tool description too, the list gets
      changed along with it and something other than the intent is measured.
    Hence line-exact -- and a failure aborts instead of keeping quiet.
    """
    zeilen = prompt.split("\n")
    getroffen = 0
    for i, zeile in enumerate(zeilen):
        if zeile.startswith(("Aufgabe: ", "Task: ")) and alt in zeile:
            zeilen[i] = zeile.replace(alt, neu)
            getroffen += 1
    if getroffen != 1:
        raise ValueError(
            f"Aufgabe {alt!r} steht {getroffen}x in einer `Aufgabe:`/`Task:`-Zeile "
            "— erwartet genau einmal. Falsche Datei oder falsches --original-task?")
    return "\n".join(zeilen)


def werkzeugnamen(prompt: str) -> list:
    """The offered tool names, taken from the prompt -- for the `enum`.

    🛑 `re.split(r"(?m)^- ")` and NOT `splitlines()`: an entry runs across
    line breaks, because the hint texts contain some. This cut produced a false
    finding twice on 2026-10-27.
    """
    namen = []
    for stueck in re.split(r"(?m)^- ", prompt):
        kopf = stueck.split(":", 1)[0].strip()
        if kopf.startswith(("mcp.", "internal.")):
            namen.append(kopf)
    return namen


def frage(url: str, modell: str, prompt: str, zeit: float, typisiert: bool = False) -> list:
    nutzlast = {"model": modell, "messages": [{"role": "user", "content": prompt}],
                **AUFRUF}
    if typisiert:
        # A root array, so the PROMPT stays unchanged: that way the two
        # measurements differ in exactly one variable.
        nutzlast["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "preselect", "strict": True, "schema": {
                # `maxItems` is mandatory: `uniqueItems` is NOT enforced, so
                # otherwise the model repeats a valid name until the token
                # budget is used up and the answer comes back truncated. Has to
                # match `_build_preselect_schema`.
                "type": "array", "maxItems": 8, "uniqueItems": True,
                "items": {"type": "string", "enum": werkzeugnamen(prompt)},
            }},
        }
    anfrage = urllib.request.Request(
        url, data=json.dumps(nutzlast).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(anfrage, timeout=zeit) as antwort:
        text = json.loads(antwort.read())["choices"][0]["message"]["content"]
    werkzeuge = json.loads(text.strip())
    if not isinstance(werkzeuge, list):
        raise ValueError(f"keine Liste: {text[:80]!r}")
    return werkzeuge


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

    fassungen: dict[str, str] = {}
    for eintrag in a.variant:
        if "=" not in eintrag:
            sys.exit(f"--variant braucht LABEL=PFAD, bekam {eintrag!r}")
        label, pfad = eintrag.split("=", 1)
        with open(pfad) as datei:
            fassungen[label] = datei.read()

    if a.cases:
        with open(a.cases) as datei:
            faelle = json.load(datei)
    else:
        faelle = STANDARDFAELLE
    erlaubt = {"treffer", "kein-treffer"}
    for nr, fall in enumerate(faelle, 1):
        if fall.get("expect") not in erlaubt:
            # 🛑 Without this check a typo counts silently as "kein-treffer"
            # and inverts the case's verdict.
            sys.exit(f"Fall {nr} ({fall.get('task')!r}): expect="
                     f"{fall.get('expect')!r}, erlaubt sind {sorted(erlaubt)}.")
    original = a.original_task or aufgabe_finden(next(iter(fassungen.values())))

    # 🛑 Every version has to carry the task BEFORE 100+ calls run.
    for label, text in fassungen.items():
        try:
            aufgabe_einsetzen(text, original, original)
        except ValueError as e:
            sys.exit(f"Fassung {label!r}: {e}")

    print(f"  Aufgabe in den Dateien: {original!r}")
    for label, text in fassungen.items():
        sig = signatur(text)
        marke = ""
        if a.verify_signature:
            # 🛑 Only the UNCHANGED version can carry the signature from the
            # log; a variant is by definition a different prompt. Hence a
            # warning, not an abort -- unless it matches NOWHERE, in which case
            # the wrong file was measured.
            marke = "  <= deckt sich mit dem Protokoll" if sig == a.verify_signature else ""
        print(f"    {label:12s} {len(text):6d} Zeichen, Signatur {sig}{marke}")
    if a.verify_signature and not any(
            signatur(t) == a.verify_signature for t in fassungen.values()):
        sys.exit(f"\n  ABBRUCH: keine Datei hat die Signatur {a.verify_signature}. "
                 "Die Bank misst dann nicht den Produktionsprompt.")
    print()

    auftraege = [(label, f["task"], f["expect"], i)
                 for label in fassungen for f in faelle for i in range(a.reps)]
    random.Random(a.seed).shuffle(auftraege)

    treffer: dict = defaultdict(int)
    laeufe: dict = defaultdict(int)
    fehler = 0
    for n, (label, aufgabe, soll, _) in enumerate(auftraege, 1):
        prompt = aufgabe_einsetzen(fassungen[label], original, aufgabe)
        try:
            werkzeuge = frage(a.url, a.model, prompt, a.timeout, a.typed)
        except Exception as e:
            fehler += 1
            print(f"  FEHLER {label}/{aufgabe!r}: {e}", file=sys.stderr)
            continue
        getroffen = any(a.tool in str(w) for w in werkzeuge)
        laeufe[(label, aufgabe)] += 1
        if getroffen == (soll == "treffer"):
            treffer[(label, aufgabe)] += 1
        if n % max(1, len(auftraege) // 4) == 0:
            print(f"  … {n}/{len(auftraege)}", flush=True)

    labels = list(fassungen)
    kopf = "  " + "Aufgabe".ljust(38) + "soll".ljust(7) + \
           "".join(lbl.rjust(9) for lbl in labels)
    print("\n" + kopf)
    for f in faelle:
        zeile = f"  {f['task']:38s}{'JA' if f['expect'] == 'treffer' else 'NEIN':7s}"
        for lbl in labels:
            zeile += f"{treffer[(lbl, f['task'])]}/{laeufe[(lbl, f['task'])]}".rjust(9)
        print(zeile)
    print()
    for lbl in labels:
        richtig = sum(v for (x, _), v in treffer.items() if x == lbl)
        gesamt = sum(v for (x, _), v in laeufe.items() if x == lbl)
        quote = 100 * richtig / max(gesamt, 1)
        print(f"  {lbl}: {richtig}/{gesamt} richtig ({quote:.0f} %)")
    if fehler:
        print(f"\n  🛑 {fehler} Aufruf(e) fehlgeschlagen — die Quoten sind unvollstaendig.")
    return 1 if fehler else 0


if __name__ == "__main__":
    raise SystemExit(main())
