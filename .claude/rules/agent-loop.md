---
paths:
  - "src/backend/services/agent_service.py"
  - "src/backend/prompts/agent.yaml"
  - "src/backend/api/websocket/chat_handler.py"
---
# Agent loop

Loaded only when `agent_service.py`, `prompts/agent.yaml` or the chat handler is read.

## Eine wiederholte Bitte am GERÄT ist eine neue — alles Hinausgehende nicht
- Der Stale-Error-Marker deckt nur GESCHEITERTE Züge. Für eine **erfolgreich** erledigte Aktion fehlte das
  Gegenstück: am 2026-10-26 antwortete der Agent auf „scanne nach haushalt" mit „ist bereits abgeschlossen,
  Dokument 515" und rief das Werkzeug nicht. 🛑 Die Frischeprüfung der Historie hat das erst sichtbar gemacht —
  vorher sah er die Fertigmeldung gar nicht.
- `conv_context_template` trägt dafür ZWEI Hinweise, und sie gehören zusammen:
  **(a)** eine wiederholte Bitte an einem **Gerät**, dessen Vorlage gewechselt sein kann (Scan, Zähler, Foto),
  ist eine neue Bitte → Werkzeug ausführen, und das alte Ergebnis nicht als das neue ausgeben.
  **(b)** alles, was etwas **hinausgibt** — Mail, Weiterleitung, Ansage, Ablage, Kauf — ist ausdrücklich
  ausgenommen: dort **zurückfragen** statt wiederholen.
- 🛑 Ohne (b) ist (a) gefährlich. Im Review zu #1410 belegt: der erste Entwurf nannte „schicke …", „lege … ab",
  und `send_email` ist ein aktives Werkzeug; `broadcast_announcement` warnt in der eigenen Beschreibung „Do NOT
  retry … (it would re-announce in the rooms that already played)". Der häufigste Auslöser ist harmlos gemeint:
  der Nutzer wiederholt, weil er die Antwort nicht gesehen hat. Beim Scan ist das Papier, bei der Mail der Empfänger.
- Beide Hinweise stehen in **de UND en**, gleich stark formuliert; ein Test prüft beide Hälften je Sprache.
  Ein Einheitentest kann nur zeigen, dass der Text im Prompt steht — nicht, dass das Modell folgt.

## Der Steuertext muss die VORAUSWAHL erreichen
- `_preselect_tools` ist eine eigene LLM-Schicht VOR dem Agenten. Fällt ein Werkzeug dort heraus, kann der Agent
  es nicht rufen, egal wie gut seine Beschreibung ist. Am 2026-10-26 fiel `scan_document` auf „scanne nach xidra"
  heraus (3 von 33) und der Agent griff `mcp.paperless.search_documents` — er suchte statt zu scannen.
- Der `tool_hints`-Eintrag aus `mcp_servers.yaml` geht dort **ungekürzt** mit (`ToolDefinition.hint`, getrennt von
  `description`, in der er auch steckt). `_PRESELECT_DESC_CHARS` beschneidet nur die generische
  Herstellerbeschreibung.
- 🛑 Die Grenze ist **abgeleitet**: die entscheidenden Riegel in den `internal.*`-Beschreibungen (die Sätze, die
  eine Fehlauswahl verhindern, etwa „Use it ONLY for an explicit announcement to everybody") beginnen bei Zeichen
  56/121/284/385/472 — 480 deckt alle. Kosten gemessen: bei 106 Werkzeugen 3063 Token bei 80 gegen 6829 bei 480,
  also 1,2 % gegen 2,6 % eines 262.144-Token-Fensters. Die alten 80 waren aus #312 und nie begründet.
- 🛑 Die Protokollzeile nennt die **Namen**, nicht nur die Anzahl: „3/33 tools selected" lässt offen, ob das
  Werkzeug nicht angeboten oder nur nicht gewählt wurde — zwei verschiedene Reparaturen.
- 🛑 Und `(in=…)` ist der Fingerabdruck des **fertigen Prompts** — Vorlage, Sprache, Nachricht und Werkzeugliste
  zusammen, nicht eine Auswahl daraus. Gleiche Signatur bei verschiedenem Ergebnis heißt: Eingabe identisch, das
  Modell hat gewechselt; andere Signatur heißt: ein Eingang hat sich geändert (Reihenfolge — `get_tool_names()`
  gibt Einfügereihenfolge, die eine Neuregistrierung umstellt —, ein Beschreibungs- oder Hinweistext, die
  Nachricht oder die Sprache). **Jede engere Auswahl macht diese Aussage falsch**, und genau das passierte in
  zwei Fassungen hintereinander: erst fehlten Beschreibung und Sprache, dann die Nachricht (die Zeile zeigt nur
  `message[:60]`). `in=` steht auch auf allen vier Rückfallpfaden, die auf ALLE Werkzeuge zurückfallen. Auf DEBUG
  je Werkzeug ein Abdruck SEINER Zeile — die Namen allein sagten nur DASS sich etwas änderte, nie WAS.
  Ohne das kostete ein schwankender Fehlgriff am 2026-10-26 drei widerlegte Hypothesen, eine davon hätte die
  Trefferquote von 14/15 auf 7/15 gesenkt.
- 🛑 **Eine Nutzerkorrektur überstimmt die Vorauswahl — und zwar der ganze SERVER.** „Korrigieren & neu
  beantworten" zeigt den SERVERNAMEN („Scanner") und schickt als Wert das ERSTE Werkzeug dieses Servers als
  Vertreter (`IntentCorrectionButton.tsx:57-67`). Bis 2026-10-26 leitete der Handler daraus nur die ROLLE ab —
  die in jedem gemeldeten Fehlgriff schon die richtige war —, die Korrektur blieb also wirkungslos. Genau den
  Vertreter festzunageln wäre ebenso falsch: der Nutzer meint den Server, nicht `list_scanners`.
  `_tools_pinned_by_correction`: `mcp.<server>.<tool>` → alle Werkzeuge des Servers, `internal.<x>` → genau eines,
  `knowledge.ask`/`general.conversation` → nichts.
  🛑 **Die Korrektur muss an JEDEM Einstieg weitergereicht werden** — `agent.run` im `chat_handler`, der REST-Pfad
  (`routes/chat.py`, Rückfall wenn die WS-Verbindung noch nicht steht) und `run_orchestrated`, das sie per
  `**agent_kwargs` weitergibt. Alle drei fehlten zuerst; `test_agent_run_call_sites_forward_correction.py` hält
  die Klasse. Die Festnagelung läuft VOR dem Null-Werkzeuge-Rückfall, sonst geht sie genau dort verloren.
  🛑 Das Auswahlmenü holt die MCP-Einträge nur für **Admins** (`IntentCorrectionButton.tsx:114-117`,
  `/api/intents/status` ist admin-gated). Für alle anderen bleiben zwei Kern-Einträge, die nichts festnageln —
  die Korrektur ist dort wirkungslos, und das ist eine offene Entscheidung, keine Reparatur.
  🛑 Festnageln heißt ANBIETEN, nicht benutzen: der Agent sieht das Werkzeug, wählt aber weiter selbst. Eine
  Zeile sagt immer, was die Korrektur bewirkt hat (`pinned N, M newly added`) — fünf verschiedene Fälle sahen
  vorher gleich aus, nämlich still.
- 🛑 `AGENT_PRESELECT_PROMPT_DUMP` (dunkel) gibt den VOLLEN Prompt aus — eigener Schalter, nicht `LOG_LEVEL=DEBUG`:
  die Senke ist global und einstufig, ein DEBUG-Lauf schriebe auch gespeicherte Nachrichten mit. Der Prompt trägt
  die Nachricht ungekürzt.
- 🛑 Ein `hint` darf kein fremdes Gebiet beanspruchen. In der Rolle `general` (`mcp_servers: null`,
  `internal_tools: null`, zugleich Rückfall des Routers) liegen ALLE Werkzeuge in einer Liste; ein Rollenzaun aus
  `agent_roles.yaml` greift dort nicht. Ein unbedingtes „<Wort> heißt immer X" zieht die Geschwister mit herein.
- 🛑 **Das Modell folgt den BEISPIELZEICHENKETTEN, nicht der Regel.** Am 2026-10-27 am echten Prompt gemessen
  (Signatur-Abgleich, nicht Nachbau): der `scan_document`-Hinweis nannte „scanne nach xidra" und „scanne nach
  Haushalt" als Beispiele — **nur diese beiden Schreibweisen trafen**. `household`, im selben Hinweis als *Wert*
  genannt, aber nie nach „scanne nach", fiel durch; ebenso jedes Ziel mit Bindestrich (`x-idra`, der gemeldete
  Fall). Mehr Beispiele heilen das nicht, sie SIND der Fehler. Ein Hinweis muss die **Schreibweise entwerten**
  („unabhängig davon, ob englisch, groß oder mit Bindestrich") statt einen weiteren Fall aufzuzählen.
- 🛑 **Eine Grammatikregel gehört in die VORLAGE, nicht in den Hinweis.** Im Hinweis braucht sie Beispiele, und die
  prägen wieder Zeichenketten: ein Suchbeispiel „scanne x-idra nach <X>" liess den gemeldeten Fall von 3/3 auf 0/3
  fallen. Als Grundregel in `tool_preselect_prompt` (nur `de`, mit Platzhalter `X`) wirkt dieselbe Regel: Hinweis
  allein 89 %, Grundregel allein 88 %, **zusammen 95 %** gegen 70 % ohne beides. Gegengeprüft, dass sie fremdes
  Gebiet NICHT übernimmt: `internal.bluetooth_scan` bleibt bei „scanne nach Bluetooth-Geräten" 20/20.
- 🛑 **Vor jeder Änderung an einem Hinweis messen:** `bin/measure-preselect.py` spielt den per
  `AGENT_PRESELECT_PROMPT_DUMP` eingefangenen ECHTEN Prompt wiederholt und verschränkt durch und prüft über
  `--verify-signature`, dass es der aus dem Protokoll ist. Vier Fassungen, die „offensichtlich besser" aussahen,
  massen schlechter (70/74/89 % gegen 95 %).
- 🛑 **Mess-Falle: ein Eintrag der Werkzeugliste geht über ZEILENUMBRÜCHE.** Beschreibungen enthalten Umbrüche; wer
  mit `startswith("- ")` zählt, misst nur die erste physische Zeile. So entstand zweimal der Befund
  „`scan_document` ist 102 Zeichen lang und trägt keinen Hinweis" — der Eintrag war 1936 Zeichen lang und trug ihn
  vollständig. Ein Eintrag reicht von `^- ` bis zum nächsten: `re.split(r"(?m)^- ", prompt)`.
- Interne und Föderations-Werkzeuge tragen **keinen** `hint` — ihre Anleitung steht in der Beschreibung, und
  darum muss die Grenze dafür reichen. Wer eine neue Werkzeugquelle anbindet, entscheidet: Hinweis anlegen oder
  die Anleitung in die ersten `_PRESELECT_DESC_CHARS` Zeichen setzen.

## Stale-error marker — the two halves must stay together
- A failed tool turn is persisted with `action_success: False` in the message metadata.
- The `conv_context` builder in `services/agent_service.py` prepends `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` to those
  assistant messages when it re-injects history into the next agent turn.
- The `conv_context_template` in `prompts/agent.yaml` carries the matching hint (in both the de and the en variant): marker
  lines are HISTORICAL, not current state — so a repeated user request RE-RUNS the tool instead of echoing the old
  error.
- Never rename the marker, drop the hint, or change the `action_success is False` test on one side only: without the
  hint the LLM treats the old failure as the present state and answers from it without calling the tool.

## …and the CONDITION BEFORE them is the third half (#1367)
Both halves can be intact and the marker still never fire, because nothing ever writes `False`. Measured 2026-10-04:
- **`success` means "the outcome", never "the call came back".** `mcp_client._detect_inner_error` reads the TOP level
  of the payload: an explicit `success`/`ok` boolean DECIDES, and only without one do `error` (str) and a truthy
  `error_code` count. Our own servers answer `{"ok": false, …}` with neither `success` nor `error` — that shape was a
  silent success for months. Never recurse into nested payloads: a status tool answering `{"ok": true, "result":
  {"ok": false}}` reports a PAST failure, and the call did succeed.
- **The agent path needs its own carrier.** A failed step has no `data`, so it never reaches `agent_tool_results`;
  fixing only the detector moves `action_success` from `True` to `None`, which is just as unmarked. `chat_handler`
  therefore collects EVERY `tool_result` step as `agent_tool_outcomes` `(tool, success)` and
  `_failed_agent_actions` decides per tool name.
- **An action's outcome is its LAST attempt with that tool.** A rejected parameter the agent immediately retries is a
  recovery, not a failed turn; an unretried failure stays a failure even when a different tool succeeded afterwards
  (the measured shape: `route_scan` fails, a read tool then succeeds while the agent explains why).
- `action_success` has four consumers — this marker, `turn_extraction` (no memory from a failed turn), the kiosk role
  activity, and the follow-up chips. A fifth, the proactive intent-feedback frame, is deliberately fenced to the
  ranked-intent path (`not agent_used`): the agent path has no recognised intent to confirm.
