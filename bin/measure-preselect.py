#!/usr/bin/env python3
"""Misst die Werkzeug-Vorauswahl gegen den ECHTEN Prompt, statt sie zu raten.

Warum es das gibt
-----------------
Am 2026-10-27 kostete „scanne nach x-idra sucht Dokumente" acht Messrunden. Vier
Review-Runden davor hatten am Hinweistext gearbeitet, ohne je zu messen, ob die
Aenderung etwas bewirkt. Zwei Fassungen, die „offensichtlich besser" aussahen,
massen schlechter (70 % und 74 % gegen 89 %); eine dritte verschlechterte den
gemeldeten Fall von 3/3 auf 0/3. Ohne Bank haette jede davon ausgeliefert werden
koennen.

🛑 Gemessen wird gegen den ECHTEN Prompt, nicht gegen einen Nachbau. Einen
Nachbau im Pod hielt ich am 2026-10-26 fuer die Wirklichkeit — er wich ab.

So kommt man an den echten Prompt
---------------------------------
1. `AGENT_PRESELECT_PROMPT_DUMP: "true"` in `k8s/configmap.yaml`, committen,
   `kubectl apply`, Backend neu rollen. (Dunkel, weil der Prompt die Nachricht
   des Nutzers ungekuerzt traegt — danach sofort wieder aus.)
2. Einen Zug machen, dann die Zeile holen:
       kubectl -n renfield logs <backend-pod> --since=10m \
         | grep "Tool pre-selection PROMPT"
3. Den `{!r}`-Teil mit `ast.literal_eval` in eine Datei schreiben.
4. `--verify-signature` prueft, dass die Datei der Prompt aus dem Protokoll ist:
   die Signatur muss mit dem `(in=…)` der Protokollzeile uebereinstimmen. Erst
   dann ist die Messung eine Messung.

Varianten
---------
Eine Variante ist eine vollstaendige Prompt-Datei. Gebaut wird sie ausserhalb
dieses Skripts, damit es keine Patch-Sprache lernen muss:

    python3 - <<'PY'
    p = open("dump.txt").read()
    anker = "Startet einen NEUEN Scan: Papier aus dem Einzug einlesen."
    open("variante.txt","w").write(p.replace(anker, anker + " " + NEUER_TEXT, 1))
    PY

🛑 Was gemessen wurde, wird ausgeliefert — byteweise. Eine Variante, die mitten
in einem Satz eingefuegt wurde, darf nicht als sauberes Deutsch in die YAML
wandern, ohne die saubere Fassung erneut zu messen.

Beispiel
--------
    bin/measure-preselect.py \
        --variant IST=dump.txt --variant NEU=variante.txt \
        --reps 3 --verify-signature 7ebd68f5

Die Auftraege werden VERSCHRAENKT und gemischt abgearbeitet. Blockweise
hintereinander profitiert llama-servers KV-Zwischenspeicher und taeuscht
Stabilitaet vor — ein 15er-Block lieferte so 15 identische Antworten, waehrend
derselbe Fall ueber die Bank verteilt schwankte.
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

# Muss zu `_DEFAULT_LLM_OPTIONS_TOOL_PRESELECT` in services/agent_service.py und
# zu `get_classification_chat_kwargs` passen — sonst misst die Bank etwas
# anderes als die Produktion. `num_ctx` fehlt absichtlich: der OpenAI-Pfad
# bildet es nicht ab (s. `_options_to_openai`).
AUFRUF = {"temperature": 0, "max_tokens": 512,
          "chat_template_kwargs": {"enable_thinking": False}}

# Der Satz, der im Papier-/Suche-Zweig immer wieder kippte. `expect: "treffer"`
# heisst: `--tool` MUSS gewaehlt werden; "kein-treffer" heisst: darf NICHT.
STANDARDFAELLE = [
    # Ziel nach „scanne nach <X>" — kein Objekt dazwischen
    {"task": "scanne nach x-idra", "expect": "treffer"},
    {"task": "scanne nach X-IDRA", "expect": "treffer"},
    {"task": "scanne nach xidra", "expect": "treffer"},
    {"task": "scanne nach household", "expect": "treffer"},
    {"task": "scanne nach Haushalt", "expect": "treffer"},
    {"task": "scanne nach foo-bar", "expect": "treffer"},
    {"task": "scanne das Dokument", "expect": "treffer"},
    {"task": "scanne die Seiten", "expect": "treffer"},
    {"task": "einscannen nach x-idra", "expect": "treffer"},
    # Papier als Objekt: ungelesenes Papier hat keinen durchsuchbaren Inhalt
    {"task": "scanne die Seiten nach x-idra", "expect": "treffer"},
    {"task": "scanne die Seiten nach Haushalt", "expect": "treffer"},
    # Negativkontrollen: explizite Suchverben
    {"task": "suche nach x-idra", "expect": "kein-treffer"},
    {"task": "finde Dokumente von x-idra", "expect": "kein-treffer"},
    {"task": "zeig mir die Rechnungen von x-idra", "expect": "kein-treffer"},
    {"task": "welche Dokumente gibt es zu x-idra", "expect": "kein-treffer"},
    # Negativkontrollen: Objekt mit lesbarem Text zwischen Verb und „nach"
    {"task": "scanne das Dokument nach x-idra", "expect": "kein-treffer"},
    {"task": "scanne x-idra nach Rechnungen", "expect": "kein-treffer"},
    {"task": "scanne die Rechnung nach dem Betrag", "expect": "kein-treffer"},
    {"task": "durchsuche das Dokument nach x-idra", "expect": "kein-treffer"},
]


def signatur(prompt: str) -> str:
    """Wie `agent_service.preselect_input_signature` — der FERTIGE Prompt."""
    return hashlib.sha1(prompt.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def aufgabe_finden(prompt: str) -> str:
    treffer = re.search(r'^Aufgabe: "(.*)"$', prompt, re.M) or \
              re.search(r'^Task: "(.*)"$', prompt, re.M)
    if not treffer:
        sys.exit("Keine `Aufgabe:`/`Task:`-Zeile gefunden — --original-task angeben.")
    return treffer.group(1)


def aufgabe_einsetzen(prompt: str, alt: str, neu: str) -> str:
    """Tauscht die Aufgabe — ausschliesslich in der `Aufgabe:`/`Task:`-Zeile.

    🛑 Ein blankes `prompt.replace(alt, neu)` hat zwei stille Fehlschlaege, und
    beide sehen nach einem Ergebnis aus:
    - fehlt `alt` in einer Variante, misst JEDER Fall denselben Prompt, und die
      Ausgabe steht voller glatter 5/5 oder 0/5;
    - steht `alt` zufaellig auch in einer Werkzeugbeschreibung, wird die Liste
      mitveraendert und damit etwas anderes gemessen als beabsichtigt.
    Darum zeilengenau — und ein Fehlschlag bricht ab, statt zu schweigen.
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
    """Die angebotenen Werkzeugnamen aus dem Prompt — fuer das `enum`.

    🛑 `re.split(r"(?m)^- ")` und NICHT `splitlines()`: ein Eintrag geht ueber
    Zeilenumbrueche, weil die Hinweistexte welche enthalten. Dieser Schnitt hat
    am 2026-10-27 zweimal einen Falschbefund erzeugt.
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
        # Wurzel-Array, damit der PROMPT unveraendert bleibt: so unterscheiden
        # sich die beiden Messungen in genau einer Variable.
        nutzlast["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "preselect", "strict": True, "schema": {
                # `maxItems` ist Pflicht: `uniqueItems` wird NICHT erzwungen,
                # also wiederholt das Modell sonst einen gueltigen Namen bis das
                # Token-Budget aufgebraucht ist und die Antwort abgeschnitten
                # zurueckkommt. Muss zu `_build_preselect_schema` passen.
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
    # 🛑 `bin/preselect-cases-peer.json` liegt im Repo, damit die
    # Foederations-Messung wiederholbar ist. Die Standardfaelle unten
    # decken den Papier-/Suche-Zweig; die Peer-Faelle decken
    # „frag Xs renfield" / „was liegt in <Instanz>" / „in allen
    # renfields" samt der Negativkontrollen, an denen am 2026-10-29 die
    # Entscheidung gegen eine globale Grenze gefallen ist:
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
            # 🛑 Ohne diese Pruefung zaehlt ein Tippfehler stumm als
            # "kein-treffer" und dreht die Bewertung des Falls um.
            sys.exit(f"Fall {nr} ({fall.get('task')!r}): expect="
                     f"{fall.get('expect')!r}, erlaubt sind {sorted(erlaubt)}.")
    original = a.original_task or aufgabe_finden(next(iter(fassungen.values())))

    # 🛑 Jede Fassung muss die Aufgabe tragen, BEVOR 100+ Aufrufe laufen.
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
            # 🛑 Nur die UNVERAENDERTE Fassung kann die Signatur aus dem
            # Protokoll tragen; eine Variante ist per Definition ein anderer
            # Prompt. Darum eine Warnung, kein Abbruch — ausser es stimmt
            # NIRGENDS, dann wurde die falsche Datei gemessen.
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
