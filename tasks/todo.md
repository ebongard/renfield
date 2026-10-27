# Scan-Fertigmeldung: Knopf zum Paperless-Dokument

**Bitte (2026-10-27):** „wenn ich scanne, dann möchte ich danach ein Link zu dem
Paperless Document in der Antwort haben, nicht nur die Nummer."

**Entschieden:** Chip in der Meldung, **beim Anzeigen aufgelöst**. LAN-Adresse
als Linkziel genügt.

## Warum nicht einfach einen Link in den Text

Gemessen, bevor entworfen wurde:

| Befund | Folge |
|---|---|
| Die Nummer ist `renfield_document_id`, nicht Paperless | heute zeigt die Antwort die falsche Kennung für diesen Zweck |
| Paperless-Kennung existiert zur Meldung noch nicht (⌀ 363 s Ingest, Haushalt, 60 Tage) | ein Link beim Schreiben wäre meistens leer |
| 6 von 37 abgeschlossenen bekommen **nie** eine (Dublette/Ablehnung) | der Fall „kein Link" muss sauber aussehen |
| Der Chat rendert **kein** Markdown | ein Rohtext-Link bliebe tote Zeichenkette |
| `render_completion_message` ist eine **Vertrauensgrenze** (Text wird in spätere Agentenzüge wieder eingespielt) | der Text bleibt unverändert; der Link lebt in den Metadaten |
| Die Meldung trägt bereits `message_metadata.scanner_job` | der Hausweg steht offen: strukturierte Daten → React-Komponente |

## Plan

### Backend
- [x] `services/artifact_service.py`: `ALLOWED_KINDS` um `document_ref` ergänzen
      (Backend deckelt nur Größe; die Form prüft zod im Frontend).
- [x] `services/scanner_jobs.py`: bei `status == "done"` ein `document_ref`-Artefakt
      über `merge_artifacts_into_metadata` an die Meldung hängen. **Der
      Meldungstext bleibt Zeichen für Zeichen gleich** — kein Link im Text.
- [x] `src/frontend/src/components/chat/artifacts/artifactSchema.ts`: `kind`
      `document_ref` in die zod-Union, Daten = `{ documentId: number }`.
- [x] 🛑 `api/routes/knowledge_schemas.py`: `paperless_url: str | None = None` auf
      `DocumentResponse` deklarieren. OHNE das verwirft Pydantic den Schlüssel
      **still** (`knowledge.py:706` baut `DocumentResponse(**kwargs)`), und das
      Feld käme nie beim Frontend an. Vorbild steht daneben: `in_paperless`.
      (Review-Korrektur C1, keine Wahl — ohne sie wirkt das Feature nicht.)
- [x] `api/routes/knowledge.py`: im gemeinsamen Dokument-Serialisierer
      `paperless_url: str | None` ergänzen, zusammengesetzt aus
      `settings.paperless_api_url` + `/documents/<id>/details`, nur wenn
      `paperless_document_id` gesetzt ist. `in_paperless` bleibt unverändert.
      🛑 Das Zusammensetzen gehört in **eine** Hilfsfunktion, damit es testbar
      ist und nicht an drei Endpunkten driftet (`/documents`,
      `/documents/batch`, `/documents/{id}` teilen den Serialisierer).
- [x] Keine neue Einstellung: `PAPERLESS_API_URL` wird wiederverwendet.

### Frontend
- [x] Neue Artefakt-Komponente `DocumentRefArtifact.tsx` neben den bestehenden
      (`KeyValueArtifact`, `PresenceMapArtifact`, `DeviceControlArtifact`), in
      `ArtifactRenderer.tsx` auf den neuen `kind` geschaltet. Sie holt
      `GET /api/knowledge/documents/<id>` und zeigt genau einen von drei
      Zuständen:
      - `paperless_url` gesetzt → Knopf „In Paperless öffnen"
      - noch im Ingest → „wird gerade abgelegt" (nicht klickbar)
      - abgeschlossen ohne Kennung → „nicht in Paperless abgelegt"
- [x] i18n-Schlüssel in `de.json` **und** `en.json` (und `it.json`, wo die
      Nachbarschlüssel existieren). Keine fest verdrahteten Texte.
- [x] Dark-Mode-Varianten, nur DESIGN.md-Token. Kein vom Modell erzeugtes HTML.

### Tests (Pflicht, nicht optional)
- [x] Backend: `paperless_url` ist gesetzt, wenn die Kennung existiert, und
      `None`, wenn nicht. Zusammensetzung ohne doppelten Schrägstrich.
- [x] Backend: die Metadaten tragen `renfield_document_id` — **und** eine
      Negativkontrolle, dass der Meldungs*text* unverändert bleibt (die
      Vertrauensgrenze darf nicht aufweichen).
- [x] Backend: ein Scan ohne Dokument (unrouted/failed) trägt **keine**
      `renfield_document_id` und die Komponente erscheint dort nicht.
- [x] Frontend: RTL + MSW für alle drei Zustände.

### Doku (nach `/review`, vor dem Merge)
- [x] `.claude/rules/scanner.md`: ein Satz, dass die Meldung die Dokument-
      kennung in den Metadaten führt und der Knopf erst beim Anzeigen auflöst —
      samt Begründung (Kennung existiert zur Meldung noch nicht).

## Offene Punkte / Risiken
- **Berechtigung:** `GET /documents/{id}` muss für die anfragende Person
  zugelassen sein und über die Kreis-Stufen gehen (`user_id=asker_id`). Vor dem
  Bau prüfen, ob der Endpunkt das bereits tut — sonst ist das der erste Schritt.
- **LAN-Grenze:** Der Link trägt nur im Heimnetz. Bewusst so entschieden; falls
  das später stört, wird aus `PAPERLESS_API_URL` eine getrennte Link-Basis.
- **Ein Abruf je Scan-Nachricht.** Selten genug, kein Zwischenspeicher nötig.

## Decision ledger

### R1: Traegermechanik fuer den Knopf — bestehendes Artefakt-System oder eigene Komponente
Finding: F3 (P1, confidence 9/10, `src/frontend/src/components/chat/artifacts/artifactSchema.ts:110`,
Reviewer: plan-eng-review, ausgeloest durch die Nutzerfrage „Haben wir nicht eine extra
Funktionalitaet fuer das Rendering von Daten, wie z.B. Wetterdaten?")
Plan baseline: urspruenglicher Vorschlag — eigene Komponente, die `message_metadata.scanner_job`
liest und in `ChatMessages.tsx` eingehaengt wird. Nicht genehmigt.
Runtime evidence:
- `artifactSchema.ts:110-117` — zod-Union ueber `kind`: table, list, keyvalue, chart, weather,
  device_control, presence_map. `weather` ist woertlich der vom Nutzer genannte Fall.
- `chat_handler.py:1524-1525` — „Emitted as `artifact` WS frames and persisted into
  message_metadata[\"artifacts\"]". Artefakte sind also PERSISTENT, nicht nur Strom.
- `ChatContext.tsx:245` — `...(Array.isArray(meta?.artifacts) && … { artifacts: meta.artifacts })`
  — Rehydrierung beim Verlaufsladen. Genau der Weg, den eine Scan-Meldung nimmt.
- `ChatMessages.tsx:533` — Renderstelle existiert, hinter `artifacts_typed_enabled`.
- Live: `ARTIFACTS_TYPED_ENABLED=true` im Haushalt.
- `artifact_service.py:44` — `ALLOWED_KINDS` (Backend deckelt Groesse), Form prueft zod vorne.
- `artifactSchema.ts:36-38` — `keyValueDataSchema` ist nur `pairs: [{key, value}]`, reine
  Zeichenketten. Ein bestehender kind kann KEINEN Link tragen.

Comparison grid:

| Choice | Current | A (Artefakt-System) | B (eigene Komponente) |
|---|---|---|---|
| Traegermechanik | keine, pending | bestehendes Artefakt-System, neuer `kind` | eigenes Feld in `scanner_job` |
| Formpruefung | keine, pending | zod, mit Rueckfall auf escaped Codeblock | keine |
| Persistenz/Rehydrierung | keine, pending | vorhanden, wiederverwendet | eigener Pfad ueber `scanner_job` |
| Renderstelle | keine, pending | vorhanden (`ChatMessages.tsx:533`) | neue Einhaengung noetig |
| Funktionsschalter | keine, pending | `artifacts_typed_enabled` (live an) | keiner |
| Live-Aufloesung Paperless | gefordert | in der neuen Komponente | in der neuen Komponente |
| Dateien | — | ~13 | ~12 |
| `paperless_url` im Serialisierer | pending | gleich | gleich |

State: approved
Actual answer: A) Bestehendes Artefakt-System (Antwort auf D2)
Accepted scope: Der Knopf faehrt auf dem bestehenden Artefakt-System. Neuer `kind`
`document_ref` in `artifact_service.ALLOWED_KINDS` (Backend-Deckel) und in
`artifactSchema.ts` (zod-Form); die Scan-Fertigmeldung haengt ihr Artefakt ueber
`merge_artifacts_into_metadata` an; eine neue Artefakt-Komponente loest beim Anzeigen
auf und zeigt Paperless-Link / „wird abgelegt" / „nicht abgelegt". Renderstelle,
Persistenz, Rehydrierung und Funktionsschalter werden WIEDERVERWENDET, nicht neu gebaut.
Nicht genehmigt und nicht Teil dieser Antwort: F1 und F2 (siehe Befunde).
History: D1 (Komplexitaetstor, Dateiaufteilung) ist damit erledigt — unter A ist die
Komponentendatei vom System vorgegeben, nicht waehlbar; die Aufteilung wird nicht neu
gestellt. D1 (Komplexitaetstor, Dateiaufteilung) wurde gestellt und vom Nutzer mit dieser
Rueckfrage beantwortet statt mit einer Auswahl. D1 ist dadurch gegenstandslos geworden:
unter A ist die eigene Komponentendatei vom System vorgegeben, nicht waehlbar. D1 wird nach
der Entscheidung zu R1 nur dann neu gestellt, wenn die Aufteilung dann noch offen ist.

### R2: Wie der Artefakt-Anhang auf `done` begrenzt wird
Finding: C2 (P2, confidence 9/10, `src/backend/services/scanner_jobs.py:200`, Reviewer: plan-eng-review)
Plan baseline: nicht spezifiziert — der Plan sagt nur „bei `status == \"done\"`". Nicht genehmigt.
Runtime evidence:
- Prior learning `two-switches-over-one-status-set-drift` (9/10, 2026-10-04): in DIESER Datei
  verzweigten `render_completion_message` und `describe_failure_for_log` ueber dieselbe
  Statusmenge; die zweite vergass zwei Zustaende, ein `unrouted`-Auftrag meldete
  `error_code=<fehlt>`.
- `scanner_jobs.py:200` — Kommentar: „`render_completion_message` und `describe_failure_for_log`
  verzweigen ueber ..." — die Menge `_STATES_WITHOUT_ERROR_CODE` wurde daraufhin hochgezogen.
- Der Artefakt-Anhang waere die DRITTE Verzweigung ueber dieselbe Statusmenge.

Comparison grid:

| Choice | Current | A (Menge hochziehen) | B (inline `== "done"`) |
|---|---|---|---|
| Verhalten: Artefakt nur bei `done` | gefordert | gleich | gleich |
| Wo steht die Statusmenge | 1 hochgezogene + 1 inline | 2 hochgezogene, geteilt | 1 hochgezogen + 2 inline |
| Drift bei einem neuen Status | moeglich | Strukturtest faengt sie | moeglich, still |
| Test | pending | Test ueber die geteilte Menge | Test je Zustand |
| Aufwand | — | human ~20min / CC ~3min | human ~5min / CC ~1min |

State: approved
Actual answer: A) Menge hochziehen und teilen (Antwort auf D3)
Accepted scope: Die Zustaende, bei denen ein Dokument existiert, werden als benannte
Modulkonstante in `scanner_jobs.py` gefuehrt (Muster: `_STATES_WITHOUT_ERROR_CODE`), der
Artefakt-Anhang verzweigt ueber genau diese Menge, und ein Test haelt das fest.
History: —

## Testabdeckung (geplante Pfade)

```
CODE PATHS                                              USER FLOWS
[+] services/scanner_jobs.py                            [+] Scan fertig -> Knopf
  |-- Artefakt-Anhang                                     |-- [GAP] Meldung erscheint, Knopf sichtbar
  |   |-- [GAP] status in DOCUMENT_STATES + doc_id        |-- [GAP] Klick oeffnet Paperless
  |   |-- [GAP] status in DOCUMENT_STATES, doc_id fehlt   |-- [GAP] Ablage laeuft noch -> "wird abgelegt"
  |   |-- [GAP] status NICHT in DOCUMENT_STATES           |-- [GAP] Dublette -> "nicht abgelegt"
  |   \-- [GAP] Meldungstext UNVERAENDERT (Negativkontrolle)
  \-- [GAP] Strukturtest: Verzweigung ueber die GETEILTE Menge (R2)
[+] api/routes/knowledge.py                             [+] Fehlerzustaende
  |-- paperless_url                                       |-- [GAP] Abruf 403 -> kein Knopf, keine Fehlerwand
  |   |-- [GAP] paperless_document_id gesetzt             |-- [GAP] Abruf 404 -> dasselbe
  |   |-- [GAP] None -> Feld ist None                     \-- [GAP] Netzfehler -> stiller Rueckfall
  |   \-- [GAP] Basis-URL mit Schraegstrich am Ende
  \-- [GAP] C1: Feld kommt TATSAECHLICH durch das response_model
[+] DocumentRefArtifact.tsx                             [+] Schalter
  |-- [GAP] [->E2E nicht noetig] drei Zustaende            \-- [GAP] artifacts_typed_enabled aus -> kein Knopf
  \-- [GAP] zod-Formfehler -> Rueckfall auf Codeblock (vom System geerbt)

COVERAGE: 0/18 geplante Pfade (Plan, noch kein Code)  |  GAPS: 18
```

🛑 Zwei Tests tragen mehr als die anderen:
- **Die Negativkontrolle auf den unveraenderten Meldungstext.** `render_completion_message`
  ist eine Vertrauensgrenze (ihr Text wird in spaetere Agentenzuege wieder eingespielt).
  Ohne diesen Test weicht sie beim naechsten Umbau unbemerkt auf.
- **C1 muss END-ZU-ENDE geprueft werden**, nicht am Serialisierer: ein Test, der nur
  `_doc_to_response_kwargs` aufruft, bliebe gruen, waehrend Pydantic das Feld verwirft.
  Der Test muss durch `DocumentResponse` gehen.

## NOT in scope
- **Zweite Nachricht, wenn die Ablage spaeter durchlaeuft.** Verworfen in der
  Entwurfsentscheidung: braucht eine zweite Zustellung mit denselben
  Genau-einmal-Garantien wie die erste.
- **Von aussen erreichbare Paperless-Adresse.** Bewusst zurueckgestellt; der Link
  traegt im Heimnetz. Aenderung waere spaeter eine getrennte Link-Basis, kein Code.
- **Buendelung der Dokumentabrufe** ueber `/documents/batch` (knowledge.py:643).
  Bei 37 Scans in 60 Tagen Vorratshaltung; P3.
- **Renfield-eigene Dokument-Detailseite.** Existiert nicht und wird hier nicht gebaut.

## What already exists (wiederverwendet statt nachgebaut)
- **Artefakt-System**: `artifactSchema.ts` (zod-Union), `ArtifactRenderer.tsx`,
  `artifact_service.merge_artifacts_into_metadata`, Persistenz in
  `message_metadata["artifacts"]`, Rehydrierung in `ChatContext.tsx:245`,
  Renderstelle `ChatMessages.tsx:533`, Schalter `artifacts_typed_enabled` (live an).
  Neu ist allein ein `kind` plus seine Komponente.
- **Zugriffsschutz**: `GET /documents/{id}` ist fail-closed (401/403 ueber
  `check_document_access`). Kein neuer Riegel noetig.
- **Dokument-Serialisierer**: `_doc_to_response_kwargs` bedient bereits alle fuenf
  Dokument-Endpunkte; `in_paperless` ist das Vorbild fuer `paperless_url`.
- **Statusmengen-Muster**: `_STATES_WITHOUT_ERROR_CODE` in `scanner_jobs.py`.

## Datenfluss

```
Scanner (Mac) --POST /api/scanner/job-event--> Backend
                                                 |
                                   render_completion_message()   [Text, UNVERAENDERT]
                                                 |
                              status in DOCUMENT_STATES ? --nein--> keine Artefakte
                                                 | ja
                              merge_artifacts_into_metadata(meta, [document_ref])
                                                 |
                                         save_message() --> DB
                                                 |
           /ws/user scan_job_finished --> Frontend laedt Gespraech neu
                                                 |
                      ChatContext rehydriert meta.artifacts --> ArtifactRenderer
                                                 |
                                      DocumentRefArtifact (kind=document_ref)
                                                 |
                              GET /api/knowledge/documents/<id>   [401/403 fail-closed]
                                                 |
                 paperless_url ? --ja--> Knopf "In Paperless oeffnen"
                                 --nein, in Arbeit--> "wird gerade abgelegt"
                                 --nein, abgeschlossen--> "nicht in Paperless abgelegt"
```

## Failure modes
| Pfad | Realistischer Produktionsfehler | Abdeckung | Sichtbar? |
|---|---|---|---|
| Artefakt-Anhang | Caps des Backends weisen das Artefakt ab | Test geplant | Knopf fehlt, Meldung bleibt — kein Bruch |
| `paperless_url` | Feld nicht auf `DocumentResponse` (C1) | Test geplant (end-zu-ende) | 🛑 STILL, deshalb C1 als Pflicht im Plan |
| Abruf im Knopf | 403, weil das Dokument einer anderen Person gehoert | Test geplant | Knopf zeigt keinen Link, keine Fehlerwand |
| Abruf im Knopf | Netzfehler / Backend weg | Test geplant | stiller Rueckfall auf "wird abgelegt" |
| Schalter aus | `artifacts_typed_enabled=false` | Test geplant | Knopf verschwindet ganz |

**Kritische Luecken: 0** — jede Stelle, die still scheitern koennte, hat einen geplanten Test.
C1 waere ohne den Plan-Eintrag eine gewesen.

## Worktree parallelization strategy
Sequential implementation, no parallelization opportunity. Das Frontend braucht
`paperless_url` aus dem Backend, und der neue `kind` muss in beiden Erlaubnislisten
stehen, bevor die Komponente etwas zu rendern hat.

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

- [x] **T1 (P1, human: ~45min / CC: ~5min)** — backend/artifacts — `document_ref` als kind registrieren
  - Surfaced by: R1 (Antwort D2) — bestehendes Artefakt-System statt Parallelgleis
  - Files: `src/backend/services/artifact_service.py`, `src/frontend/src/components/chat/artifacts/artifactSchema.ts`
  - Verify: Backend-Test auf `ALLOWED_KINDS`; `npx tsc --noEmit -p .` im Frontend
- [x] **T2 (P1, human: ~1h / CC: ~8min)** — scanner — Artefakt an die Fertigmeldung haengen
  - Surfaced by: R2 (Antwort D3) — Verzweigung ueber die geteilte Statusmenge
  - Files: `src/backend/services/scanner_jobs.py`, `tests/backend/test_scanner_jobs*.py`
  - Verify: Tests inkl. Negativkontrolle „Meldungstext unveraendert"
- [x] **T3 (P1, human: ~30min / CC: ~4min)** — api — `paperless_url` ausliefern
  - Surfaced by: C1 — Pydantic verwirft unbekannte Schluessel still
  - Files: `src/backend/api/routes/knowledge_schemas.py`, `src/backend/api/routes/knowledge.py`, `tests/backend/test_knowledge*.py`
  - Verify: Test geht durch `DocumentResponse`, nicht nur durch den Serialisierer
- [x] **T4 (P1, human: ~2h / CC: ~12min)** — frontend — `DocumentRefArtifact` mit drei Zustaenden
  - Surfaced by: Entwurfsentscheidung „beim Anzeigen aufloesen"
  - Files: `src/frontend/src/components/chat/artifacts/DocumentRefArtifact.tsx`, `ArtifactRenderer.tsx`, `i18n/locales/{de,en,it}.json`, `tests/frontend/react/`
  - Verify: RTL fuer alle drei Zustaende + 403/Netzfehler; dark mode, DESIGN.md-Token
- [x] **T5 (P2, human: ~15min / CC: ~2min)** — docs — Rule nachziehen
  - Surfaced by: Doku-Tor des Projekts
  - Files: `.claude/rules/scanner.md`
  - Verify: Sweep ueber `docs/`, `CLAUDE.md`, `.claude/rules/`

## Unresolved decisions that may bite you later
Keine. R1 und R2 sind beantwortet; C1 ist eine sachliche Korrektur ohne Wahl;
A1, A2 und die Leistungsnotiz sind berichtet und bewusst nicht remediert.

Approval readiness: PASS — R1 (Antwort D2), R2 (Antwort D3). C1 als notwendige
Umsetzung eines bereits genehmigten Vertrags mitgefuehrt, ohne eigene Frage.

## Completion summary
- Step 0: Scope Challenge — scope accepted as-is (Komplexitaetstor ausgeloest, D1 durch D2 gegenstandslos)
- Architecture Review: 2 issues found (A1, A2; A3 geprueft und entkraeftet)
- Code Quality Review: 2 issues found (C1, C2)
- Test Review: diagram produced, 18 gaps identified (Plan ohne Code)
- Performance Review: 1 issue found (P3, Buendelung zurueckgestellt)
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 0 items proposed to user
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0 in this review
- Outside voice: codex, unavailable (nicht installiert; nativer Rueckfall mangels TaskOutput nicht moeglich)
- Parallelization: 1 lane, 0 parallel / 5 sequential
- Lake Score: 2/2 = 10/10 choices

## Review
(wird nach der Umsetzung ergänzt)

## Gestaltungsentscheidungen (plan-design-review)

### Zustandstabelle

| Zustand | Auftritt | Token | Text (de) |
|---|---|---|---|
| Link vorhanden | Knopf, `.btn-secondary`, Radius `sm` 4px | Standard-Knopf aus DESIGN.md §Buttons | „In Paperless öffnen" |
| wird gerade abgelegt | gedämpfter Text, kein Knopf | neutral gedämpft (`text-gray-500 dark:text-gray-400`) | „wird gerade abgelegt" |
| nicht abgelegt | gedämpfter Text, **Grund im Text** | dieselbe neutrale Dämpfung | „schon vorhanden" (Dublette) / „nicht abgelegt" |

🛑 **Entscheidung D2: KEIN Bernstein für „nicht abgelegt".** Eine Dublette ist kein
Fehlschlag — das Dokument liegt in Paperless, nur schon länger. Gemessen betrifft das
rund jeden sechsten Scan (6 von 37 in 60 Tagen); eine Warnfarbe bei jedem sechsten Mal
nutzt sich ab und warnt bald vor nichts mehr. Bernstein bleibt den Fällen vorbehalten,
die DESIGN.md am 2026-09-28 dafür vorgesehen hat.
Preis, bewusst in Kauf genommen: eine echte Ablehnung durch Paperless sieht damit so
ruhig aus wie eine Dublette. Der Grund steht aber im Text.

### Zustandsabdeckung (Entscheidung D3)

| Feature | LADEN | LEER | FEHLER | ERFOLG | TEILWEISE |
|---|---|---|---|---|---|
| Dokumentknopf | Ladegerüst des Artefakt-Rahmens (`animate-pulse motion-reduce:animate-none`) | entfällt — ohne Dokumentkennung entsteht kein Artefakt | wie „wird gerade abgelegt": der Stand ist unbekannt | Knopf „In Paperless öffnen" | erbt der Artefakt-Rahmen (`partial` → Rückfall) |

🛑 **Nur der klickbare Zustand ist ein `<button>`/`<a>`.** Die übrigen sind schlichter
Text ohne Fokus — kein Tabben durch tote Schaltflächen, und der Screenreader kündigt
keine Taste an, die keine ist. Ein deaktivierter Knopf wäre zudem regelmäßig unter
4,5:1 Kontrast.

Bewusst nicht unterschieden: gescheiterter Abruf und „noch in Arbeit" sehen gleich aus.
Für die Person bedeutet beides dasselbe — noch kein Link.

### Zugänglichkeit (aus DESIGN.md übernommen, keine neuen Entscheidungen)
- Tippfläche im Link-Zustand **≥ 44 px** (DESIGN.md §Accessibility).
- Kontrast **≥ 4,5:1** für allen Text, auch den gedämpften.
- Dunkelmodus über `dark:`-Varianten; keine fest verdrahteten Farben.
- Reduzierte Bewegung: erbt der Artefakt-Rahmen (`motion-reduce:animate-none`).
- Der Knopf öffnet in einem neuen Tab (`rel="noopener"`), weil er aus dem Gespräch
  herausführt — das Gespräch bleibt stehen.

### Sperrlisten-Abgleich
Keine Treffer. Kein Symbol in farbigem Kreis (Sperrliste 3), keine farbige linke
Kante (8), kein eigener Radius (5 — es gilt `sm` 4px aus der Skala), keine Emoji (7),
kein Verlauf (1). Der Knopf ist ein Knopf.

## Implementation Tasks (Gestaltung, Ergänzung zu T1–T5)

- [x] **T4a (P1, human: ~30min / CC: ~4min)** — frontend — Zustandstabelle umsetzen
  - Surfaced by: Pass 2 / D3 — Lade- und Fehlerzustand fehlten im Plan
  - Files: `src/frontend/src/components/chat/artifacts/DocumentRefArtifact.tsx`
  - Verify: RTL deckt Laden, Erfolg, „wird abgelegt", „schon vorhanden", Abruffehler
- [x] **T4b (P2, human: ~20min / CC: ~3min)** — frontend — Zugänglichkeit festnageln
  - Surfaced by: Pass 6 — nichts spezifiziert
  - Files: dieselbe Komponente
  - Verify: nur der Link-Zustand ist fokussierbar; Tippfläche ≥44px; Kontrast geprüft

```
  +====================================================================+
  |         DESIGN PLAN REVIEW — COMPLETION SUMMARY                    |
  +====================================================================+
  | System Audit         | DESIGN.md vorhanden + festgelegt; UI-Umfang:|
  |                      | eine Artefakt-Komponente, drei Zustaende    |
  | Step 0               | 4/10, Fokus auf Zustaende/Zugang/Text       |
  | Pass 1  (Info Arch)  | nicht bewertet — vom Nutzer ausgeklammert   |
  | Pass 2  (States)     | 3/10 -> 9/10 after fixes                    |
  | Pass 3  (Journey)    | nicht bewertet — vom Nutzer ausgeklammert   |
  | Pass 4  (AI Slop)    | nicht bewertet — Sperrlisten-Abgleich: 0    |
  | Pass 5  (Design Sys) | 2/10 -> 9/10 after fixes                    |
  | Pass 6  (Responsive) | 1/10 -> 9/10 after fixes                    |
  | Pass 7  (Decisions)  | 2 resolved, 0 deferred                      |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (4 items, aus der Technikpruefung)   |
  | What already exists  | written                                     |
  | TODOS.md updates     | 0 items proposed                            |
  | Approved Mockups     | 0 generated, 0 approved (vom Nutzer         |
  |                      | ausgeklammert)                              |
  | Decisions made       | 2 added to plan (D2 Token, D3 Zustaende)    |
  | Decisions deferred   | 0                                           |
  | Overall design score | 4/10 -> 9/10                                |
  +====================================================================+
```

🛑 **Drei Dimensionen wurden auf ausdruecklichen Wunsch NICHT geprueft** (Informations-
hierarchie, Nutzerreise, volle Slop-Pruefung mit Bildentwuerfen). Der Gesamtwert bezieht
sich auf die drei geprueften. Das ist eine bewusste Einschraenkung, keine Abdeckung.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 3 | STALE (2026-06-22, issues_open) | mode: HOLD_SCOPE, 5 critical gaps |
| Outside Review | codex (`/plan-eng-review`) | Independent 2nd opinion | 17 | UNAVAILABLE (2026-10-27) | nicht installiert; nativer Rueckfall mangels TaskOutput nicht moeglich |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 29 | ISSUES OPEN (PLAN) | 5 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 7 | CLEAR (FULL) | score: 4/10 -> 9/10, 2 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | STALE | — |

- **OUTSIDE COVERAGE:** codex, Phase plan-review, **unavailable** (nicht installiert,
  nativer Rueckfall mangels `TaskOutput` nicht moeglich). Die Gestaltungspruefung hat
  ihre eigenen Fremdstimmen NICHT angefordert — der Umfang wurde auf drei Dimensionen
  ohne Bildentwuerfe begrenzt. Beides sind Luecken, keine sauberen Ergebnisse.
- **VERDICT:** Design Review CLEAR. Eng Review ISSUES OPEN — 5 Befunde, alle in
  Aufgaben ueberfuehrt, 0 kritische Luecken. eng review required

NO UNRESOLVED DECISIONS
