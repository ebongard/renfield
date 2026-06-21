# Renfield - Feature Dokumentation

## Übersicht

Renfield ist ein vollständig offline-fähiger, selbst-gehosteter **digitaler Assistent** — ein persönlicher AI Hub, der Wissen, Informationsabfragen und Multi-Channel-Steuerung in einer Oberfläche bündelt. Er dient mehreren Nutzern parallel im Haushalt. Kernfähigkeiten: abfragbare Wissensbasis (RAG), gebündelte Tool-Nutzung über 8 MCP-Server, Konversations-Gedächtnis, proaktive Benachrichtigungen und Smart-Home-Steuerung.

## Chat & Konversation

### Natural Language Understanding
- **Intent Recognition**: LLM-basierte Erkennung von Benutzerabsichten mit Ranked Intents (1-3 gewichtete Intents mit Fallback-Chain)
- **Dynamische Keywords**: Geräte- und Entity-Namen aus Home Assistant werden automatisch in den Intent-Prompt injiziert
- **MCP Tool Prompt Filtering**: `prompt_tools` in `mcp_servers.yaml` beschränkt den Intent-Prompt auf ~20 relevante Tools (alle bleiben ausführbar)

### Streaming Responses
- **WebSocket-basiert**: Echtzeit-Antworten mit Token-für-Token Streaming
- **Session-Persistenz**: `session_id` im WebSocket für Konversations-Kontext
- **Fallback auf HTTP**: REST API (`POST /api/chat/send`) als Alternative

### Rich Content in Chat-Nachrichten
- **Album Art**: Jellyfin-Bild-URLs und gängige Bildformate (`.jpg`, `.png`, `.gif`, `.webp`) werden als Inline-Bilder gerendert statt als Klartext
- **Collapsible Agent Steps**: Agent-Schritte (Tool-Calls + Ergebnisse) sind einklappbar — offen während der Verarbeitung, eingeklappt nach Abschluss
- **Quellen-Chips**: Stützt sich eine Antwort auf die Wissensdatenbank (RAG), erscheinen unter der Antwort anklickbare Quellen-Chips — Dokumentname + Zugriffs-Tier (`TierBadge`), je verlinkt auf `/knowledge?doc={id}`. Nur die im Zug tatsächlich abgerufenen, bereits circle-gefilterten Dokumente erscheinen (keine zweite Berechtigungs-Prüfung); ohne KB-Treffer wird nichts gezeigt. Live über den `done`-Frame, persistiert in `message_metadata.sources` (rehydriert beim Laden der Historie).
- **Folgefragen-Chips** (`FOLLOWUP_CHIPS_ENABLED`, opt-in/dark): Nach einer Antwort schlägt ein kleiner Best-Effort-LLM-Aufruf 2-4 kurze Folgefragen vor, die unter der letzten Assistenten-Antwort als anklickbare Chips erscheinen; ein Tipp füllt das Eingabefeld (kein Auto-Senden). Im **Hintergrund nach dem `done`-Frame** erzeugt (verzögert die Antwort/TTS/Wakeword nie) und über einen separaten `followups`-Frame nachgereicht; bei gesprochenen (TTS-)Antworten, Fehler-Turns und sehr kurzen Antworten übersprungen. Ephemer (nicht persistiert).
- **Befehlspalette** (`COMMAND_PALETTE_ENABLED`, opt-in/dark): `/` im leeren Eingabefeld (oder ein Touch-Button) öffnet eine Aktions- und Navigations-Palette — zu Seiten springen, eine Routine/ein Tool-Kommando ins Eingabefeld **vorbereiten** (kein Auto-Senden), oder die Agenten-Rolle für den nächsten Turn setzen. Tastatur-Navigation (Pfeile/Enter/Esc) + Touch + a11y. Der Anzeige-Filter (welche Aktionen sichtbar sind) richtet sich nach den Berechtigungen des Nutzers; die **eigentliche Rechte-Prüfung bleibt serverseitig** (Tool-Kommandos laufen als natürliche Sprache durch den Agent-Loop). Der Rollen-Hint ist nur eine weiche Routing-Präferenz für den nächsten Turn (serverseitig gegen `agent_roles.yaml` validiert) — keine Rechte-Eskalation.
- **Korrigieren & neu beantworten**: Hat der Router eine Anfrage falsch eingeordnet, korrigiert der Nutzer den Intent über „Falsch erkannt?" — das speichert wie bisher das Lern-Feedback **und** blendet zusätzlich einen **„Neu beantworten"**-Button ein. Ein Tipp führt den Turn mit der korrigierten Route erneut aus (kein Auto-Rerun — ein bewusster Tipp; geteilte Haushalts-GPU). Der korrigierte Intent reist als `corrected_intent` über den WS-Frame; das Backend (`agent_router.role_for_intent`, alleinige Quelle der Server↔Rolle-Zuordnung aus `agent_roles.yaml`) bildet ihn auf die **spezifischste** Agenten-Rolle ab (eine breite Sammel-Rolle wie `routine` überschattet die Fachrolle nie). Wiederverwendet den `role_hint`-Pfad — **nur Routing, keine Rechte-Eskalation** (jedes Tool bleibt zur Ausführungszeit berechtigungs-geprüft); ein nicht zuordenbarer Wert fällt auf normales Routing zurück.
- **Agenten-Rolle anzeigen & steuern** (`ROLE_SURFACING_ENABLED`, opt-in/dark): Jede Assistenten-Antwort zeigt ein kleines Badge mit der **Agenten-Rolle**, die sie erzeugt hat (Smart Home / Medien / Dokumente / …) — macht die Router-Entscheidung sichtbar. Ein Tipp auf das Badge **heftet diese Rolle für den nächsten Turn an** (verwendet den bestehenden `role_hint`-Pfad — nur Routing, keine Rechte-Eskalation). Die Rolle reist auf dem `done`-Frame mit und wird in `message_metadata.agent_role` persistiert, sodass das Badge beim Laden der Historie erhalten bleibt. a11y: Rollenname als Text (nicht nur Farbe). Bekannte Grenzen: bei orchestrierten Mehr-Domänen-Turns zeigt das Badge die einzelne primär klassifizierte Rolle; eine per ConfigMap neu hinzugefügte Rolle erscheint mit roher ID (graceful Fallback).
- **Nachrichtensuche** (`MESSAGE_SEARCH_ENABLED`): Volltextsuche über Chat-Nachrichten — ein Suchfeld in der Konversations-Seitenleiste, in-Konversation (mit `session_id`) oder global (konversationsübergreifend). Treffer sind nach Postgres-FTS (`ts_rank`) sortiert, paginiert, mit hervorgehobenem Snippet, und ein Klick springt zur Nachricht (Scroll + Fokus). **Streng nach Konversations-Eigentümerschaft gefiltert** (`Conversation.user_id`) — Nachrichten sind keine Atome, daher NICHT über `circle_sql`; ein Nutzer findet nur Nachrichten in eigenen Konversationen (per Negativ-Test abgesichert). XSS-sicher (Backend liefert STX/ETX-Marker → echtes `<mark>`, kein `dangerouslySetInnerHTML`). Mehrsprachig (DE/EN/FR/IT/ES/NL `tsvector`-Union). a11y: Tastatur-Navigation (↑/↓/Enter/Esc), warmer Leer-Zustand bei 0 Treffern.
- **Artefakte** (`ARTIFACTS_TYPED_ENABLED`, opt-in/dark — Lane A): Generierte strukturierte Inhalte (Wochenplan, Einkaufsliste, Smart-Home-Status, kleines Diagramm) erscheinen inline im Chat-Turn als **typisierte Komponenten** statt als Code-Block — `table`, `list`, `keyvalue`, `chart`. Transportiert als **typisierte JSON-Daten → echte React-Komponenten** (kein Modell-HTML, kein Modell-SVG): jeder Wert läuft durch React's Escape-Boundary, Injection ist nicht anwendbar (dasselbe Vertrauensmodell wie der bestehende `AdaptiveCardRenderer`). Das `chart` ist hand-gezeichnetes Bar-/Line-SVG aus typisierten Serien (keine Charting-Abhängigkeit); Mehr-Serien-Diagramme unterscheiden per Farbe **plus** zweitem Kanal (End-of-line-Labels / Muster — WCAG 1.4.1, grayscale-lesbar). Produziert **nur** aus dem Hook-/Sub-Intent-/Orchestrierungs-Pfad (typisiertes Dict), **nie** durch Parsen der Agent-Freitext-Antwort. Backend prüft den DoS-Riegel (Kind-Allowlist + Größen-Caps), das Frontend-zod-Schema ist die maßgebliche Form-Validierung. **Fail-closed:** ungültige Form / werfender Renderer / unbekanntes Kind / hängender `partial` → escapter Code-Block-Fallback (nie ungesandboxtes Markup), gültig-aber-leer → warmer kind-spezifischer Leerzustand (≠ Fehler). Persistiert als `message_metadata.artifacts[]` (Array, per `id`), rehydriert beim Laden der Historie. Mehrere Artefakte pro Turn möglich; Streaming-Patches (gleiches `id` → anhängen, idempotent). Bounded `.card`-Container mit „generiert"-Hinweis; semantisches DOM (Screenreader), Tabellen mit horizontalem Scroll im Bubble. Lane B (free-form HTML/SVG in sandboxed iframe) ist **zurückgestellt** (eigenes Security-Review, `ARTIFACTS_HTML_SANDBOX_ENABLED` Platzhalter, nicht verdrahtet). Voraussetzung: enforcing baseline-CSP in `nginx.conf`. Design: `docs/design/chat-artifacts-sandbox.md`. **Erster Produzent:** eine Smart-Home-**Status/Übersicht**-Anfrage ("Wie ist der Status im Haus?", "Zeig mir den Smart-Home-Status") liefert über das `smart_home/status`-Sub-Intent (`ha_glue/services/smarthome_status.py`, `dispatch_sub_intent`-Hook) Prosa **plus** eine typisierte `table` (Raum · Gerät · Status) aus dem gecachten HA-Entity-Map — persistiert, rehydriert. Aktuierungs-Befehle ("mach das Licht an") klassifizieren NICHT als `status` und schalten weiterhin normal; bei HA-Ausfall: Prosa ohne Tabelle (nie Crash). Inert wenn `ARTIFACTS_TYPED_ENABLED` aus. **Vier Smart-Home-Produzenten** (alle `dispatch_sub_intent`, `ha_glue/services/smarthome_status.py` + `smarthome_artifacts.py`, aus dem gecachten HA-Entity-Map): `smart_home/status` → `table` (Raum·Gerät·Status); `smart_home/sensors` ("Temperaturen/Sensorwerte") → `keyvalue` (Raum → Temp·Feuchte); `smart_home/active_devices` ("Was ist gerade an?") → `list` der eingeschalteten Geräte; `smart_home/devices_per_room` ("Geräte pro Raum") → `chart` (Bar, Anzahl je Raum). Alle vier Sub-Intents stehen in der ConfigMap-served `config/agent_roles.yaml` (`smart_home.sub_intents`). **Gen-UI-Widgets** (Item 10, `services/widget_tools.py`): neben den festen Smart-Home-Produzenten kann der **Agent selbst** Widgets rendern. (1) **Wetter-Widget** — eine neue `weather`-Artefakt-Art (aktuelle Bedingungen + Tagesvorschau, WMO-Code → Wetter-Icon); bei Wetterfragen ruft der Agent `internal.weather_widget` auf, das `mcp.weather.get_weather` (Open-Meteo, `WEATHER_ENABLED`) holt und auf das `weather`-Artefakt abbildet. (2) **Listen/Tabellen auf Anfrage** — fragst du eine Liste/Tabelle an, rendert der Agent sie über `internal.render_table`/`internal.render_list` als Widget (strukturierte Daten als Tool-Argumente — weiterhin typisiertes JSON, NIE aus Freitext geparst; `artifact_service` validiert). Das Widget begleitet eine kurze Einleitung (ersetzt die Prosa nicht). (3) **Interaktive Gerätesteuerung** (`device_control`) — "steuere die Lichter" / "zeig mir die Lichtsteuerung fürs Wohnzimmer" rendert ein Widget mit klickbaren An/Aus-Schaltern (Lichter/Steckdosen), einem **Helligkeits-Schieberegler** für eingeschaltete Lichter, Szenen-Buttons und einem **Thermostat-Sollwert-Stepper** für Klima/Heizung (Producer `internal.device_controls`, liest **frische** HA-States, damit die Anfangswerte stimmen). Eine Interaktion ist der **Artefakt→Aktion-Rückkanal**: das Widget sendet einen `device_action`-WS-Frame (wie die Paperless-Bestätigungskarte, optional mit numerischem `value`), das Backend prüft **fail-closed die `HA_CONTROL`-Berechtigung** (ein Geräte-/Satelliten-Token ohne Nutzer wird bei aktiver Auth abgelehnt) und validiert Domain/Aktion/Wert (geklammert) erneut, bevor Home Assistant geschaltet wird — das Widget gewährt nichts, was du nicht ohnehin per Agent dürftest. (4) **Anwesenheitskarte** (`presence_map`, schreibgeschützt) — "wer ist wo?" zeigt Räume mit den aktuell anwesenden Personen (Producer `internal.presence_map`). Reitet auf `ARTIFACTS_TYPED_ENABLED`.
- **Raum-Handoff-Hinweis** (`ROOM_HANDOFF_ENABLED`, opt-in/dark — Chat-UI Item 8): folgt die Medienwiedergabe dem Nutzer in einen anderen Raum (Media-Follow), erscheint im Chat-Thread eine dezente Meta-Zeile "🔊 Wiedergabe folgt nach {Raum}" — der Payoff sichtbar gemacht, dass es Satelliten gibt. Backend `media_follow_service` emittiert einen `media_handoff`-Frame NUR bei erfolgreichem Resume, **raum-scoped** an die Zielraum-Geräte (dieselbe Privacy-Reichweite wie der bestehende Info-Toast — fremde Standorte werden nicht breiter sichtbar); Frontend `MediaHandoffIndicator` rendert transient (12 s, max 3, nie persistiert), `role="status"`/`aria-live`, Icon mit Text (nicht nur Farbe), dark + i18n. Unbekannter Raum → generisches "Wiedergabe folgt dir"; fehlgeschlagener Follow → nichts. (Der „Konversation fortgesetzt in {Raum}"-Fall ist als `continued`-Kind reserviert, Backend-Trigger noch offen.)
- **Nachrichten-Branching / Edit-and-Fork** (`CHAT_BRANCHING_ENABLED`, opt-in/dark — Chat-UI Item 1; Design `docs/design/chat-branching.md`): Du kannst **jede** deiner Nachrichten bearbeiten oder **jeden** Assistenten-Turn neu generieren — statt die Historie zu überschreiben, entsteht ein **Branch** im Konversationsbaum. Datenmodell: `messages.parent_message_id` (Self-FK) + `conversations.active_leaf_message_id`; der aktive Zweig ist der rekursive Pfad von der Leaf-Nachricht aufwärts (eine rekursive CTE, konversations-gescopt). Vier Pfade werden branch-bewusst: Historie-Laden, `conv_context`-Replay (heilt von selbst), Erinnerungs-Extraktion, und die Nachrichtensuche (auf den aktiven Zweig gefiltert + Index neu berechnet). Flag-aus = byte-identisch (CTE-always-on über den einmaligen Backfill). **Phase 2 (umgesetzt):** Fork von *jeder* Nachricht + ein **per-Nachricht `‹ n/m ›`-Branch-Umschalter** am Verzweigungspunkt (◂/▸ schaltet den aktiven Zweig um; `PUT …/active-leaf` löst auf die tiefste Leaf des Teilbaums auf) + **Branch löschen** (`DELETE /api/chat/{session_id}/branch/{message_id}` — eigentümer-gated 404, aktiver Zweig 409, Teilbaum gelöscht; zweig-lokale Memories werden **soft-deleted + entkoppelt**, KG-Provenienz entkoppelt). Das Erinnerungs-Handling ist ein **symmetrisches `recompute_memory_activation`** (`is_active = Quelle ∈ aktiver Pfad`, bei jedem Fork UND Umschalten neu berechnet) — das ergänzt die **Reaktivierung** beim Zurückwechseln und **schließt das Deactivate-at-Fork-Race**. Migration `pc20260618_message_branching`.
- **Credential Sanitization**: API-Keys und Tokens in MCP-Antworten werden automatisch aus der Chat-Anzeige redacted

### Chat-Historie
- **Session Management**: Getrennte Gespräche mit Datumsgruppierung (Heute, Gestern, Letzte 7 Tage, Älter)
- **Persistente Speicherung**: Alle Nachrichten in PostgreSQL
- **Follow-up Kontext**: LLM erhält Konversationshistorie; versteht "Mach es aus" oder "Und dort?" ohne explizite Referenzen
- **Volltext-Suche**: Durchsuche frühere Konversationen
- **Satellite-Sessions**: Tägliche Sessions für Voice-Commands

### Dateianhänge & Weiterleitung an Paperless
- **Paperclip-Upload**: Nutzer hängen Dateien im Chat-Eingabefeld an; `POST /api/chat_upload` speichert sie, OCR läuft asynchron
- **OCR-Text im Kontext**: Der Agent sieht den extrahierten Text per `document_context`-Prompt-Block inklusive `[attachment_id=N]`-Marker pro Datei
- **Natürliche Weiterleitung nach Paperless**: „Lade diese Rechnung nach Paperless hoch" → Agent ruft `internal.forward_attachment_to_paperless(attachment_id=N)` auf; die Datei wird **direkt von Server-Storage** gelesen und als echte Bytes zum Paperless-MCP geleitet — der Agent handhabt nie Base64-Inhalte und kann daher nichts halluzinieren
- **Session-Scope**: Die Attachment-Lookup ist auf die Chat-Session des Nutzers beschränkt — kein Cross-Session-Zugriff auf fremde Uploads
- **Interaktive Bestätigungskarte**: Beim Cold-Start-Confirm (erste N Uploads) zeigt der Chat eine klickbare Karte statt einer getippten Syntax — pro unklarem Feld (Korrespondent / Typ / Tags / Speicherpfad) eine Auswahl „vorhandenen Treffer übernehmen / neu anlegen (editierbar) / leer lassen", Vorgaben vorausgewählt (nie „neu"). Der Nutzer klickt und bestätigt; das Frontend sendet eine strukturierte Entscheidung (`paperless_confirm`-WS-Frame) direkt an `internal.paperless_commit_upload`. Die getippte `ja` / `nein` / `1: 2, 2: neu`-Antwort bleibt als Fallback erhalten.
- **HTTP-Fallback**: `POST /api/chat_upload/{id}/paperless` als direkter Endpoint für Frontend-Buttons (ohne LLM im Loop)

## Sprach-Interface

### Speech-to-Text (STT)
- **Whisper Integration**: OpenAI's Whisper für Offline-Transkription
- **Modell-Auswahl**: tiny, base, small, medium, large
- **Audio-Preprocessing**: Rauschunterdrückung und Normalisierung (opt-in)
- **GPU-Beschleunigung**: Optional mit NVIDIA GPU

### Text-to-Speech (TTS)
- **Piper Integration**: Natürlich klingende Stimmen, lokal generiert
- **Mehrsprachig**: Separate Stimmen pro Sprache (z.B. `de:de_DE-thorsten-high,en:en_US-amy-medium`)

### Voice Chat
- **End-to-End Voice**: Sprechen → Transkription → Verarbeitung → Antwort → Vorlesen
- **Sprechererkennung**: Automatische Identifikation mit SpeechBrain ECAPA-TDNN (192-dim Embeddings, Cosine Similarity)
- **Auto-Discovery**: Unbekannte Sprecher werden automatisch als Profile angelegt
- **Continuous Learning**: Verbesserte Erkennung durch jede Interaktion

Siehe [SPEAKER_RECOGNITION.md](SPEAKER_RECOGNITION.md) für Details.

### Visual Queries (Satellite Camera)
- **Kamera-Snapshot bei Wakeword**: Satellites mit Kamera (z.B. IMX219) fotografieren automatisch bei Wakeword-Erkennung
- **Vision-LLM**: Bild + Transkription werden an ein Vision-fähiges Modell (z.B. `qwen3-vl`) geschickt
- **Anwendungsbeispiele**: "Was steht auf diesem Zettel?", "Was siehst du?", "Welche Farbe hat das?"
- **Graceful Degradation**: Satellites ohne Kamera sind nicht betroffen; ohne Vision-Modell wird das Bild ignoriert
- **Audio-Ausgabe**: Antwort wird per TTS über DLNA-Renderer oder Satellite-Speaker abgespielt

Siehe [SATELLITE_CAMERA.md](SATELLITE_CAMERA.md) für Details.

## Second Brain — Persönliches Wissensnetz

Renfield pflegt für jeden Nutzer ein persönliches Wissenssystem aus vier Informationsarten — Dokument-Chunks (RAG), Conversation Memories, Knowledge-Graph-Entities und -Relations — die über eine gemeinsame Atom-Registry ansprechbar sind. Cross-Source-Suche unter `/brain` fusioniert diese Quellen via Reciprocal Rank Fusion. Hinzu kommen aus Dokumenten extrahierte **Schicht-A-Fakten** (`document_fact`), die als eigene Quelle in dieselbe Fusion einfließen (grünes „Fakt"-Badge). Die Extraktion ist offen/generisch: deterministische Identifikatoren (Steuernummer, IBAN) plus ein LLM-Pass mit freier Faktenliste, sodass jeder Dokumenttyp seine eigenen Eckdaten trägt (Rechnungsdatum, Vertragskonto, Leistungszeitraum, Beträge, Aussteller, Zahlungsverpflichtung …). Jede Dokumentkarte unter `/knowledge` zeigt eine inline **Fakten**-Panel, und die **Verpflichtungs-Agenda** unter `/brain/fristen` (Rechnungen + Behörden-Fristen, nach Dringlichkeit gruppiert) macht anstehende Fristen sichtbar — beide Flächen mit Herkunfts-Markierung (✓ deterministisch / ~ Modell-Vorschlag) und `⚑ rechtlich` bei gesetzlichen Fristen. Aus denselben Fakten synthetisiert ein LLM-Pass beim Ingest einen **sprechenden Dokumenttitel** (Aussteller + Dokumentart + Datum, z. B. „Mahnung BFS health finance" statt `2026_05_23_10_55_18.pdf`), gespeichert in `documents.generated_title` und in der Dokumente-Liste als Anzeigename genutzt (Fallback: Metadaten-Titel → Dateiname). Bestehende Dokumente werden per `bin/backfill_document_titles.py` aus ihren bereits extrahierten Fakten nachträglich betitelt (kein Re-OCR).

**Vereinheitlichter Wissens-Workspace** (Feature-Flag `wissen_workspace_enabled`, standardmäßig aus): Ist es aktiv, bündeln sich Dokumente, Suche, Graph, Erinnerungen, Fristen und die Review-Queue zu einem `/wissen`-Workspace mit persistenter Lens-Leiste, lens-bezogener Omnisuche und einem universellen Detail-Drawer (Klick auf ein beliebiges Atom → Detailansicht + Tier-Anpassung). Die bisherigen Einzelrouten leiten dann dorthin um; ist das Flag aus, bleibt die getrennte Navigation unverändert.

Siehe [SECOND_BRAIN.md](SECOND_BRAIN.md) für den narrativen Überblick und das Ingestion-Flow.

## Zugriffsebenen (Circles)

Eigentum und Sichtbarkeit wird über die **Circles** modelliert — eine fünfstufige Leiter (`self`, `trusted`, `household`, `extended`, `public`), die die reale Weitergabe-Intuition abbildet: etwas gehört mir, etwas meinen Vertrauten, etwas dem Haushalt, etwas benannten Externen, ein kleiner Teil öffentlich.

- Jedes Atom trägt **eine** Tier-Zuweisung
- Pro-Eigentümer-Dimension: Alice' `trusted` ist nicht Bobs `trusted`
- Vier-Zweig-Zugriffsregel: OWNER ∨ PUBLIC ∨ EXPLICIT GRANT ∨ TIER-REACH
- `AUTH_ENABLED=false` short-circuited den Filter (Single-User-Mode)

Siehe [CIRCLES.md](CIRCLES.md) für Datenmodell, Services, Routen und Frontend-Seiten.

## Konversations-Gedächtnis (Langzeit)

Renfield kann sich Dinge über Nutzer langfristig merken — Präferenzen, Fakten und Anweisungen werden als semantische Embeddings gespeichert und bei relevanten zukünftigen Gesprächen automatisch eingeblendet.

### Memory-Kategorien

| Kategorie | Beschreibung | Beispiel |
|-----------|-------------|---------|
| `preference` | Vorlieben und Stil | "Ich bevorzuge kurze Antworten" |
| `fact` | Gelernte Fakten | "Meine Katze heißt Luna" |
| `instruction` | Benutzerdefinierte Regeln | "Antworte immer auf Deutsch" |
| `correction` | Korrigierte Aussagen | Via Widerspruchserkennung |

### Funktionsweise
1. **Extraktion** — Memories werden automatisch aus Konversationen extrahiert (opt-in: `MEMORY_EXTRACTION_ENABLED`)
2. **Speicherung** — 768-dim Embeddings via pgvector mit semantischer Deduplizierung (Threshold: 0.9)
3. **Retrieval** — Bei neuen Nachrichten werden semantisch ähnliche Memories abgerufen (Cosine Similarity ≥ 0.7, max 3)
4. **Context Injection** — Relevante Memories werden in den LLM-Prompt eingefügt
5. **Decay** — Context-Kategorie Memories verfallen nach konfigurierbarer Zeit (default: 30 Tage)

### Vollständige Aufzählung (Self-Knowledge)

Die automatische Context Injection (Schritt 3) blendet nur die wenigen semantisch zur aktuellen Nachricht passenden Memories ein (max `MEMORY_RETRIEVAL_LIMIT`). Für **breite Fragen über den Nutzer selbst** ("Was weißt du über mich?", "Liste alle meine Vorlieben auf") reicht das nicht — eine vage Meta-Frage matcht die spezifischen Fakten schlecht. Dafür gibt es das Agent-Tool **`internal.list_my_memories`** (`services/memory_list_tool.py`): Es zählt die **eigenen** Memories des authentifizierten Nutzers ohne Vektor-Schwellwert auf, optional gefiltert nach Kategorie (`preference|fact|context|instruction|procedural`). Die `user_id` wird serverseitig aus dem authentifizierten Kontext injiziert — nie aus LLM-Parametern — sodass das Tool ausschließlich die Memories des fragenden Nutzers liest.

### Widerspruchserkennung

Opt-in Feature (`MEMORY_CONTRADICTION_RESOLUTION=true`): Beim Speichern neuer Memories werden bestehende auf semantische Ähnlichkeit geprüft. Memories im Threshold-Bereich (0.6–0.89) werden dem LLM zur Widerspruchsprüfung vorgelegt. Bei Widerspruch wird die alte Memory aktualisiert oder archiviert.

### Audit Trail

Jede Änderung an Memories wird in der History dokumentiert:
- **Aktionen**: `created`, `updated`, `deleted`
- **Quellen**: `system`, `user`, `contradiction_resolution`
- **API**: `GET /api/memory/history/{id}` liefert die vollständige Änderungshistorie

### Konfiguration

```env
MEMORY_ENABLED=false                    # Master-Switch (opt-in)
MEMORY_EXTRACTION_ENABLED=false         # Auto-Extraktion aus Konversationen
MEMORY_RETRIEVAL_LIMIT=3                # Max Memories pro Query
MEMORY_RETRIEVAL_THRESHOLD=0.7          # Cosine-Similarity Schwellwert
MEMORY_MAX_PER_USER=500                 # Max aktive Memories pro Nutzer
MEMORY_CONTEXT_DECAY_DAYS=30            # Verfall für Context-Kategorie
MEMORY_DEDUP_THRESHOLD=0.9              # Deduplizierungs-Schwellwert
MEMORY_CONTRADICTION_RESOLUTION=false   # LLM-basierte Widerspruchserkennung
MEMORY_CONTRADICTION_THRESHOLD=0.6      # Untere Grenze für Widerspruchs-Check
```

## Agent System (ReAct)

### Übersicht

Komplexe Anfragen werden automatisch erkannt und über einen ReAct-Loop (Reason + Act) bearbeitet. Der Agent Router klassifiziert jede Nachricht in eine spezialisierte Rolle, die bestimmt welche Tools und Modelle verwendet werden.

### Agent Router

Jede Nachricht wird vom Router in genau eine Rolle klassifiziert:

| Rolle | MCP-Server | Max Steps | Beschreibung |
|-------|-----------|-----------|--------------|
| `smart_home` | homeassistant | 4 | Licht, Schalter, Sensoren, Klima |
| `research` | search, news, weather | 6 | Web-Suche, Nachrichten, Wetter |
| `documents` | paperless, email | 8 | Dokument-Suche, E-Mail |
| `media` | jellyfin, dlna | 6 | Musik, Filme, Serien, DLNA-Wiedergabe |
| `workflow` | n8n | 10 | Workflow-Automation |
| `knowledge` | *(RAG-Pfad)* | — | Wissensbasis-Suche (kein Agent Loop) |
| `general` | alle Server | 12 | Komplexe domänenübergreifende Anfragen |
| `conversation` | *(kein Agent)* | — | Smalltalk, allgemeines Wissen |

Rollen werden in `config/agent_roles.yaml` definiert. Pro Rolle sind separate Modelle und Ollama-URLs konfigurierbar.

### Complexity Detection

Der `ComplexityDetector` erkennt per Regex, ob eine Nachricht den Agent Loop benötigt (Zero-Cost, kein LLM-Call):

| Muster | Erkennt | Beispiel |
|--------|---------|---------|
| Bedingung | Wenn-Dann-Konstrukte | "Wenn es regnet, schließe die Fenster" |
| Sequenz | Aufeinanderfolgende Aktionen | "Hole Wetter und dann suche ein Restaurant" |
| Vergleich | Schwellwert-Vergleiche | "Wärmer als 20 Grad" |
| Multi-Aktion | Zwei Aktionsverben mit "und" | "Schalte das Licht ein und stelle die Heizung ein" |
| Kombiniert | Zwei Fragewörter mit "und" | "Wie ist das Wetter und was gibt es Neues?" |

Nachrichten unter 10 Zeichen werden immer als einfach eingestuft. Alle Muster unterstützen Deutsch und Englisch.

### ReAct Loop

```
User → ComplexityDetector → einfach? → Single-Intent (schneller Pfad)
                          → komplex? → Agent Router → Rolle auswählen
                                        → ReAct Loop:
                                          ├─ LLM: Plan → Tool Call 1
                                          ├─ Tool Result → zurück zum LLM
                                          ├─ LLM: Reasoning → Tool Call 2
                                          └─ LLM: Final Answer → Stream
```

### WebSocket Messages (Agent Loop)

| Type | Beschreibung |
|------|-------------|
| `agent_thinking` | Agent analysiert die Anfrage |
| `agent_tool_call` | Tool-Name, Parameter, Begründung |
| `agent_tool_result` | Ergebnis (Erfolg/Fehler, Daten) |
| `stream` | Finale Antwort (Token-für-Token) |
| `done` | Abschluss mit `agent_steps` Count |

### Media Transport Shortcuts

Einfache Medien-Befehle (`stop`, `pause`, `play`, `skip`, `weiter`, `leiser`, `lauter`) umgehen den Agent Loop und werden direkt als MCP-Tool-Call ausgeführt — für sofortige Reaktion ohne LLM-Overhead.

### Loop-Schutz

Der Agent bricht automatisch ab, wenn wiederholte Suchen (z.B. Jellyfin-Suche) keine neuen Ergebnisse liefern. Verhindert Endlos-Schleifen bei nicht-vorhandenen Medien.

### Stale-Tool-Error-Schutz

Frühere fehlgeschlagene Aktionen werden im `KONVERSATIONS-KONTEXT` mit dem Marker `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` gekennzeichnet. Der Agent-Prompt weist explizit darauf hin, dass solche historischen Fehler **nicht** den aktuellen Zustand belegen — fordert der Nutzer dieselbe Aktion erneut an, wird das Tool neu ausgeführt statt mit einer alten Fehlermeldung zu antworten. Der Marker wird aus dem `action_success`-Metadatum am Message-Turn abgeleitet.

### Konfiguration

```env
AGENT_ENABLED=false               # Master-Switch (opt-in)
AGENT_MAX_STEPS=12                # Max Reasoning-Schritte
AGENT_STEP_TIMEOUT=30.0           # Per-Step LLM Timeout (Sekunden)
AGENT_TOTAL_TIMEOUT=120.0         # Gesamt-Timeout
AGENT_MODEL=                      # Optional: separates Modell
AGENT_OLLAMA_URL=                 # Optional: separate Ollama-Instanz
AGENT_CONV_CONTEXT_MESSAGES=6     # Konversations-Kontext im Agent Loop
AGENT_ROUTER_TIMEOUT=30.0         # Router-Klassifikation Timeout
```

## Intent Feedback Learning

Renfield lernt aus Nutzer-Korrekturen und verbessert die Intent-Erkennung über semantisches Matching.

### Korrektur-Typen

| Typ | Beschreibung | Beispiel |
|-----|-------------|---------|
| `intent` | Falsche Intent-Klassifikation | "Das war kein Wetter-Intent, sondern Smart Home" |
| `agent_tool` | Falsches Tool im Agent Loop | "Falsches Tool gewählt" |
| `complexity` | Falsche Einfach/Komplex-Einstufung | "Das hätte der Agent machen sollen" |

### Funktionsweise

1. **Korrektur speichern** — Nutzer gibt Feedback über `POST /api/feedback/correction` oder den UI-Button
2. **Embedding erstellen** — Die ursprüngliche Nachricht wird als 768-dim Vektor gespeichert
3. **Ähnlichkeitssuche** — Bei zukünftigen Nachrichten werden semantisch ähnliche Korrekturen abgerufen (Cosine Similarity ≥ 0.75)
4. **Few-Shot Injection** — Gefundene Korrekturen werden als Beispiele in den Intent-Prompt injiziert

### Konfiguration

```env
INTENT_FEEDBACK_CACHE_TTL=300     # Cache-TTL für Korrektur-Counts (Sekunden)
```

## MCP Integration (Model Context Protocol)

### Übersicht

Alle externen Integrationen laufen als MCP-Server. Tools werden automatisch als `mcp.<server>.<tool>` Intents registriert — keine Code-Änderung nötig.

### Verfügbare Server

| Server | Transport | Beschreibung | Tools |
|--------|-----------|-------------|-------|
| **weather** | stdio (Python) | OpenWeatherMap | 17 (Vorhersage, Standort) |
| **search** | stdio (npx) | SearXNG Metasearch | 1 |
| **news** | stdio (npx) | NewsAPI | 2 (Suche, Top Headlines) |
| **jellyfin** | stdio (Python) | Media Server | 13 (Musik, Filme, Serien) |
| **dlna** | streamable_http | DLNA Renderer (Gapless Queue Playback) | 7 (Play, Stop, Queue, Transport) |
| **n8n** | stdio (npx) | Workflow Automation | 12 (Workflow CRUD, Templates) |
| **paperless** | stdio (Python) | Dokumenten-Management | 4 agentsichtbar (search, get, update, download); `upload_document` ist bewusst nicht im Agent-Prompt — siehe `internal.forward_attachment_to_paperless` |
| **email** | stdio (Python) | Multi-Account IMAP/SMTP | 4 (List, Search, Read, Send) |
| **tracking** | stdio (Python) | Multi-Carrier Paketverfolgung — Direkt-APIs (DHL/Deutsche Post, UPS, FedEx), kein Aggregator; DPD/Hermes/GLS als Web-Deep-Link | 2 agentsichtbar (`track_parcel`, `list_carriers`); `detect_carrier` als Helfer |
| **homeassistant** | streamable_http | Smart Home | 5+ (Steuerung, Status) |

### Konfiguration

Server werden in `config/mcp_servers.yaml` definiert:

```yaml
servers:
  - name: weather
    command: ["python3", "-m", "renfield_mcp_weather"]
    transport: stdio
    enabled: "${WEATHER_ENABLED:-false}"
    refresh_interval: 300
    prompt_tools:
      - get_weather
    examples:
      de: ["Wie wird das Wetter morgen?"]
      en: ["What's the weather forecast?"]
```

**YAML-Felder:**

| Feld | Pflicht | Beschreibung |
|------|---------|-------------|
| `name` | Ja | Server-ID, genutzt als `mcp.<name>.<tool>` |
| `transport` | Ja | `streamable_http`, `sse` oder `stdio` |
| `enabled` | Ja | Env-Var Toggle (z.B. `"${WEATHER_ENABLED:-false}"`) |
| `prompt_tools` | Nein | Tool-Namen für den LLM-Intent-Prompt (alle bleiben ausführbar) |
| `examples` | Nein | Bilinguale Beispiel-Queries für den LLM-Prompt |

### Features

- **Eager Connection**: Verbindung beim Startup, nicht pro Request
- **Background Refresh**: Automatischer Health-Check und Tool-Refresh (konfigurierbar)
- **Partial Failure**: Ein fehlender Server blockiert nicht die anderen
- **Env-Var Substitution**: `${VAR}` und `${VAR:-default}` in der YAML-Konfiguration
- **Input-Validierung**: MCP-Antworten werden auf Größe begrenzt (`MCP_MAX_RESPONSE_SIZE`, default: 10KB)
- **Rate Limiting**: MCP-Tool-Calls unterliegen dem REST API Rate Limiting

### Admin-Endpoints

- `GET /api/mcp/status` — Server-Verbindungen, Tool-Counts, Fehler
- `GET /api/mcp/tools` — Alle entdeckten MCP-Tools mit Schemas
- `POST /api/mcp/refresh` — Tool-Listen manuell refreshen

### Konfiguration

```env
MCP_ENABLED=false                 # Master-Switch
MCP_CONFIG_PATH=config/mcp_servers.yaml
MCP_REFRESH_INTERVAL=60           # Background-Refresh (Sekunden)
MCP_CONNECT_TIMEOUT=10.0          # Verbindungs-Timeout
MCP_CALL_TIMEOUT=30.0             # Tool-Call-Timeout
MCP_MAX_RESPONSE_SIZE=10240       # Max Response-Größe (Bytes)
```

## Proaktive Benachrichtigungen & Erinnerungen

### Übersicht

Externe Systeme (Home Assistant, n8n) senden Events per Webhook an Renfield. Nutzer sehen Benachrichtigungen im Frontend und können sie per Sprache oder UI verarbeiten.

### Webhook-Integration

```
Home Assistant Automation → POST /api/notifications/webhook
                            (Bearer Token Auth)
                            → Renfield verarbeitet, dedupliziert, enriched
                            → Frontend zeigt Benachrichtigung
```

### Features

- **Webhook-Empfang**: `POST /api/notifications/webhook` mit Bearer Token Authentifizierung
- **Semantische Deduplizierung**: Ähnliche Benachrichtigungen innerhalb eines Zeitfensters werden zusammengefasst (pgvector, opt-in)
- **Urgency-Klassifikation**: Automatische Dringlichkeitseinstufung (opt-in)
- **LLM-Enrichment**: Benachrichtigungen werden durch LLM-Kontext angereichert (opt-in)
- **Suppressions**: Nutzer können bestimmte Benachrichtigungs-Typen unterdrücken (semantisch)
- **Feedback Learning**: System lernt aus Nutzer-Interaktionen mit Benachrichtigungen (opt-in)
- **Privacy-aware TTS / Multi-Room-Auslieferung**: Benachrichtigungen werden präsenzabhängig im aktiven Raum vorgelesen; das `privacy`-Feld (public/personal/confidential) steuert, wer sie hört (Presence Detection + Audio Output Routing).

### Konfiguration

- **Master-Switch:** `PROACTIVE_ENABLED=true` (opt-in; aus = `/api/notifications/webhook` antwortet `503`). Webhook-Token via `POST /api/notifications/token` (Admin) generieren und in Home Assistant (`input_text.renfield_webhook_token`) hinterlegen.
- **MCP-Polling:** `NOTIFICATION_POLLER_ENABLED=true` lässt Renfield MCP-Server (E-Mail, Kalender) aktiv nach Events abfragen, statt nur Push-Webhooks zu empfangen.
- Vollständiger Einrichtungs-Guide + HA-Vorlagen: [`docs/PROACTIVE_NOTIFICATIONS.md`](PROACTIVE_NOTIFICATIONS.md) und [`docs/PROACTIVE_SCHEDULING_TEMPLATES.md`](PROACTIVE_SCHEDULING_TEMPLATES.md).

### Erinnerungen

- **Zeitgesteuert**: Reminder mit Fälligkeitsdatum
- **API**: `POST /api/notifications/reminders` zum Erstellen, `GET /api/notifications/reminders` zum Auflisten
- **Hintergrund-Prüfung**: Fällige Erinnerungen werden automatisch als Benachrichtigungen ausgelöst

### Konfiguration

```env
PROACTIVE_ENABLED=false                     # Master-Switch (opt-in)
PROACTIVE_SUPPRESSION_WINDOW=60             # Dedup-Fenster (Sekunden)
PROACTIVE_TTS_DEFAULT=true                  # TTS standardmäßig aktiv
PROACTIVE_NOTIFICATION_TTL=86400            # Ablauf (24h)
PROACTIVE_SEMANTIC_DEDUP_ENABLED=false      # Semantische Deduplizierung
PROACTIVE_URGENCY_AUTO_ENABLED=false        # Auto-Urgency
PROACTIVE_ENRICHMENT_ENABLED=false          # LLM-Enrichment
PROACTIVE_REMINDERS_ENABLED=false           # Erinnerungen
PROACTIVE_REMINDER_CHECK_INTERVAL=15        # Prüf-Intervall (Sekunden)
```

## Wissensspeicher (RAG)

### Übersicht

Renfield verarbeitet Dokumente und nutzt sie als Wissensbasis für kontextbasierte Antworten. Hybrid Search kombiniert semantische Vektor-Suche mit BM25 Full-Text-Search.

### Unterstützte Formate
PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT — verarbeitet mit IBM Docling.

### Pipeline

1. **Upload** → Automatische Verarbeitung und Duplikat-Erkennung (SHA256)
2. **Chunking** → Semantische Textaufteilung (konfigurierbare Chunk-Größe und Overlap)
3. **Embedding** → Jeder Chunk wird mit dem konfigurierten Modell vektorisiert (768-dim default)
4. **Hybrid Search** → Dense Embeddings (pgvector) + BM25 (PostgreSQL tsvector), kombiniert via Reciprocal Rank Fusion (RRF)
5. **Context Window** → Benachbarte Chunks werden automatisch zum Treffer hinzugefügt (±1 default)

### Features

- **Knowledge Bases** — Organisiere Dokumente in thematischen Sammlungen
- **KB-Sharing** — Teile Wissensdatenbanken mit anderen Nutzern (RPBAC)
- **Follow-up-Fragen** — RAG-Kontext bleibt für Nachfragen erhalten
- **Quellen-Zitation** — wissensgestützte Chat-Antworten zeigen die verwendeten Dokumente als anklickbare **Quellen-Chips** (siehe „Rich Content in Chat-Nachrichten")
- **Re-Embedding** — `POST /admin/reembed` nach Modellwechsel
- **knowledge_search Agent Tool** — Internes Tool im Agent Loop für kombinierte Suche über RAG-Dokumente und MCP-Quellen (Paperless)
- **EasyOCR Fallback** — Garbled PDFs werden automatisch mit EasyOCR nachverarbeitet für bessere Text-Extraktion
- **Separate Embedding-Instanz** — `OLLAMA_EMBED_URL` für dedizierten Embedding-Server (entlastet die Haupt-Ollama-Instanz)

### Konfiguration

```env
RAG_ENABLED=true
RAG_CHUNK_SIZE=512                # Chunk-Größe (64-4096)
RAG_CHUNK_OVERLAP=50              # Overlap zwischen Chunks
RAG_TOP_K=5                       # Max Ergebnisse
RAG_SIMILARITY_THRESHOLD=0.4      # Mindest-Ähnlichkeit

# Hybrid Search
RAG_HYBRID_ENABLED=true           # Dense + BM25
RAG_HYBRID_BM25_WEIGHT=0.3
RAG_HYBRID_DENSE_WEIGHT=0.7
RAG_HYBRID_FTS_CONFIG=german       # simple/german/english (german für BM25 mit OR-Matching)

# Context Window
RAG_CONTEXT_WINDOW=1              # Benachbarte Chunks (0=deaktiviert)
```

## Multi-Room Device System

### Unterstützte Gerätetypen

| Typ | Beschreibung | Verbindung |
|-----|-------------|------------|
| Satellite | Raspberry Pi Hardware | `/ws/satellite` |
| Web Panel | Stationäre Wand-Tablets | `/ws/device` |
| Web Tablet | Mobile Tablets | `/ws/device` |
| Web Browser | Desktop/Mobile Browser | `/ws/device` |
| Web Kiosk | Kiosk-Terminals | `/ws/device` |

### Raspberry Pi Satellites
- **Pi Zero 2 W** — Kostengünstige (~63€) Satellite-Einheiten
- **ReSpeaker 2-Mics HAT** (V1 + V2) — Mikrofonerfassung mit 3m Reichweite
- **ReSpeaker 4-Mic Array** — 4-Kanal-Erfassung mit arecord-Isolation (siehe [AUDIO_CAPTURE_4MIC.md](AUDIO_CAPTURE_4MIC.md))
- **Ansible Provisioning** — Automatisierte Einrichtung via Playbook (`src/satellite/provisioning/`)
- **Lokale Wake-Word-Erkennung** — OpenWakeWord mit ONNX Runtime (~20% CPU)
- **LED-Feedback** — Visuelles Feedback: Idle (Blau), Listening (Grün), Processing (Gelb), Speaking (Cyan), Error (Rot)
- **Hardware-Button** — Manuelle Aktivierung
- **Auto-Discovery** — Backend-Erkennung via Zeroconf/mDNS
- **OTA-Updates** — Version-Tracking und Update-Pakete
- **Orange Pi / k8s-Variante** — der Esszimmer-Satellit ist der erste **arm64-/k8s-Pod**-Satellit: ein Orange Pi Zero 3W (Allwinner A733) mit USB-ReSpeaker XVF3800, als node-fixierter privilegierter Pod im privaten Cluster (`k8s/satellite-esszimmer.yaml`) statt bare-metal. Stärkeres Bluetooth (BT 5.4) → gut geeignet als BLE-Präsenz-Anker.

### Zentrale Wake-Word-Verwaltung
- Admin-UI für zentrale Konfiguration
- Automatische Synchronisation per WebSocket an alle Geräte
- Konfigurierbare Keywords (Alexa, Hey Mycroft, Hey Jarvis, etc.)

Siehe [WAKEWORD_CONFIGURATION.md](WAKEWORD_CONFIGURATION.md) für Details.

### Audio-Output-Routing
Intelligentes TTS-Routing zum optimalen Ausgabegerät pro Raum (prioritätsbasiert, mit Verfügbarkeitsprüfung). Unterstützt Renfield-Geräte, HA Media Players und DLNA Renderer. Optional (`OUTPUT_PROVIDERS_ENABLED`): **generische Output-Provider** — neue Marken (Samsung TV, künftig Sonos/LG) werden room-auswählbar/-abspielbar/-steuerbar per `output_provider:`-Stanza in `mcp_servers.yaml`, ohne Backend-/Frontend-Code; inkl. Power-on (Wake-on-LAN) vor Wiedergabe. Siehe `docs/OUTPUT_ROUTING.md`.

### DLNA Gapless Album Playback
DLNA-Renderer ermöglichen lückenlose Album-Wiedergabe: Jellyfin liefert die Tracks, der DLNA MCP Server queued sie gapless auf dem Renderer. Album-Art und Metadaten werden an den Player und als Inline-Bild in den Chat weitergegeben. Siehe `internal.play_album_on_dlna` Tool.

Siehe [OUTPUT_ROUTING.md](OUTPUT_ROUTING.md) für Details.

### Automatische Raum-Erkennung
- **IP-basiert**: Stationäre Geräte werden anhand der IP-Adresse erkannt
- **Kontext-Weitergabe**: Raum-Kontext wird an LLM übergeben
- **Implizite Befehle**: "Schalte das Licht ein" funktioniert ohne Raum-Angabe

### Frontend-Verbindungsarchitektur

Das Frontend nutzt **zwei unabhängige WebSocket-Verbindungen**:

| Verbindung | Endpoint | Zweck |
|------------|----------|-------|
| Chat WS | `/ws` | Chat-Nachrichten, Session-Persistenz |
| Device WS | `/ws/device` | Geräte-Registrierung, Raum-Zuweisung, Capabilities |

Chat funktioniert ohne Geräte-Registrierung, aber Raum-Kontext erfordert diese.

## Raum-Management

- **CRUD-Operationen**: Räume erstellen, bearbeiten, löschen
- **Alias-System**: Normalisierte Namen für Sprachbefehle
- **Home Assistant Area Sync**: Import und Export von Areas mit Konfliktlösung
- **Source-Tracking**: Ursprung des Raums (Renfield, Home Assistant, Satellite)
- **Geräte pro Raum**: Übersicht, Online-Status, Geräte verschieben

## Home Assistant Integration

Steuerung erfolgt über den Home Assistant MCP-Server (`HA_MCP_ENABLED=true`) oder die direkte REST API (`/api/homeassistant`).

### Gerätesteuerung
- **Lichter**: Ein/Aus/Dimmen/Farbsteuerung
- **Schalter**: Beliebige Schalter steuern
- **Klimaanlagen**: Temperatur und Modi
- **Rollläden**: Öffnen/Schließen/Position
- **Sensoren**: Status abfragen

### Natural Language Control
```
"Schalte das Licht im Wohnzimmer ein"
"Mach die Heizung im Schlafzimmer auf 21 Grad"
"Sind alle Fenster geschlossen?"
"Aktiviere Filmabend"
```

### Entity Discovery
- Automatische Erkennung aller Home Assistant Entities
- Keyword-Refresh: `POST /admin/refresh-keywords`
- Domain-Filterung, Echtzeitstatus

## Kamera-Überwachung

### Frigate Integration
- **Event-Erkennung**: Person, Auto, Tier, etc.
- **Snapshot-Zugriff**: Bilder von Events abrufen
- **Zone-Überwachung**: Verschiedene Bereiche
- **Event-Historie**: Zeitliche Suche, Objekt-Filterung, Konfidenz-Werte

### Konfiguration
```env
FRIGATE_URL=http://frigate.local:5000
FRIGATE_TIMEOUT=10.0
```

## Paperless Audit

Automatisierte Metadaten-Prüfung für Paperless-NGX Dokumente via LLM. Opt-in via `PAPERLESS_AUDIT_ENABLED=true`.

### Funktionsweise

1. **Scan** — Alle Paperless-Dokumente werden per MCP abgerufen
2. **OCR-Qualitätsprüfung** — Heuristische Bewertung (1-5) ohne LLM via `utils/ocr_quality.score_ocr_quality` (geteilt mit der Ingest-Pipeline, damit die „Zeichensalat"-Definition nicht auseinanderdriftet): bewertet fehlende Leerzeichen (zusammengelaufene Wörter), hohe Sonderzeichen-Dichte und Fragmentierung (sehr kurze Zeilen). Eine „wiederholte Zeichen"-Regel gibt es bewusst **nicht** — am realen Korpus erzeugte sie nur Fehlalarme (Spalten-/Punkt-Formatierung, Schwärzungsmasken `XXXX`, Null-Auffüllung `0000`); echtes Garbling fangen die anderen Signale ab.
3. **LLM-Analyse** — 8 Validierungsfelder: Titel, Korrespondent, Dokumenttyp, Tags, Speicherpfad, Datum, Sprache, Archivstatus
4. **Fix-Modi**: `review` (manuelle Freigabe im Admin UI), `auto_threshold` (ab Konfidenz ≥ Schwellwert), `auto_all`

### Re-OCR (lokaler Stack mit Paperless-Fallback)

Die „Re-OCR"-Aktion läuft **nicht** mehr blind über Paperless' eigene OCR (die mit denselben Einstellungen scheitern würde). Stattdessen pro Dokument: Original-Bytes per MCP `download_document` laden (`truncate=False` — sonst wird das Base64 am LLM-Response-Limit abgeschnitten), lokal mit erzwungener Ganzseiten-OCR (Renfields Docling/EasyOCR-Stack inkl. Garbled-Layer-Recovery) neu erkennen, das Ergebnis bewerten und **nur bei striktem Qualitätsgewinn** den sauberen Text via `update_document(content=…)` zurück nach Paperless schreiben. Schlägt die lokale OCR fehl oder ist sie nicht besser, greift der Fallback auf Paperless' natives `reprocess`. Hinweis: Es wird nur der durchsuchbare Content aktualisiert, das archivierte PDF wird nicht neu erzeugt.

### Admin UI (`/admin/paperless-audit`)

Tabs: Audit Control (Start/Status), Review Queue (Sortierung, Suche, Freigabe), OCR Issues (Re-OCR-Angebot), **Niedrige OCR-Qualität** (siehe unten), Vollständigkeit, Duplikate, Ansprechpartner, Statistics.

### Niedrige OCR-Qualität (Triage statt SQL)

Eigener Tab, der Dokumente sichtbar macht, deren **Ingest** an der Qualität gescheitert ist — damit der Operator sie in der UI statt per SQL bearbeitet. Ein Dokument gilt als „niedrige OCR-Qualität", wenn **eines** zutrifft:

1. Die renfield-interne `documents`-Zeile hat `status='failed'` mit `error_message LIKE 'ocr_quality%'` (die Ingest-Pipeline hat es an der Qualitätsschwelle abgewiesen), **oder**
2. der **letzte** `document_processing_history`-Eintrag hat ≥ 30 % der Chunks an der Qualitätsschwelle verworfen (`chunks_dropped_low_quality / (produced + dropped) ≥ 0.30`).

Das Signal lebt am renfield-internen `Document`, der Audit-Datensatz am Paperless-externen `paperless_doc_id` — verknüpft über `Document.paperless_document_id`. Paperless-only-Dokumente (nie in die KB ingestet) tragen kein Badge. Jede betroffene Zeile zeigt ein Badge (`X % verworfen` bzw. `OCR fehlgeschlagen`) und zwei Aktionen: **Erneut OCR** (derselbe lokale Re-OCR-Pfad wie der OCR-Tab) und **Ignorieren** — letzteres setzt `documents.quality_ignored` (Migration `pc20260618_doc_quality_ignored`), wodurch das Dokument vom periodischen Cleanup-Lauf (`bin/purge_low_quality_chunks.py`) übersprungen und aus dem Tab herausgefiltert wird; **Wieder berücksichtigen** hebt das auf. Das Badge erscheint zusätzlich inline im OCR-Tab. Endpunkt: `POST /api/admin/paperless-audit/quality-ignore` (ADMIN-gated, wie alle Audit-Routen); der `low_quality_only`-Filter beschränkt die Ergebnisliste serverseitig.

### Konfiguration

```env
PAPERLESS_AUDIT_ENABLED=false
PAPERLESS_AUDIT_MODEL=                     # leer = Default-Modell
PAPERLESS_AUDIT_SCHEDULE=02:00             # Täglicher Lauf (HH:MM)
PAPERLESS_AUDIT_FIX_MODE=review            # review | auto_threshold | auto_all
PAPERLESS_AUDIT_CONFIDENCE_THRESHOLD=0.9
PAPERLESS_AUDIT_OCR_THRESHOLD=2            # OCR-Score ≤ 2 → Re-OCR anbieten
PAPERLESS_AUDIT_BATCH_DELAY=2.0            # Pause zwischen Dokumenten (Sekunden)
```

## Knowledge Graph — Qualitäts-Features

Ergänzend zur Basis-Extraktion (siehe Hook System):

### Entity-Validierung

Post-Extraction-Filter `_is_valid_entity()` entfernt OCR-Artefakte, URLs, E-Mails, IDs, Datumsangaben, Telefonnummern, IBANs, generische Rollen und Zeichen mit Leerzeichen. Kompilierte Regex-Patterns für Performance.

### Duplikat-Erkennung

String-Similarity (difflib SequenceMatcher) statt Embedding-Similarity für zuverlässigere Ergebnisse. Name-Normalisierung entfernt Titel (Herr/Frau/Dr.) und Org-Suffixe (GmbH/AG). Default-Threshold: 0.82.

### Bulk Cleanup API

- `POST /api/knowledge-graph/cleanup/invalid` — Scan + Soft-Delete invalider Entities (`dry_run=true` default)
- `GET /api/knowledge-graph/cleanup/duplicates` — Duplikat-Cluster per String-Similarity finden
- `POST /api/knowledge-graph/cleanup/merge-duplicates` — Auto-Merge (Threshold 0.93+ empfohlen für sicheres Auto-Merge)

### 3D-Wissensgraph (Visualisierung)

Der `/knowledge-graph` Graph-Tab rendert eine 3D-Szene (`GraphView.tsx`) über native Backend-Endpunkte:

- `GET /api/wissensbasis/graph` — Korpus-Ansicht: Connected-Component-Cluster mit Hub-Entities
- `GET /api/wissensbasis/focus?entity_id=` — Nachbarschaft einer Entity (hop1 + hop2)
- `GET /api/wissensbasis/search?q=` — Namens-Suche für das Such-Overlay

Alle drei sind `KG_VIEW`-gated und circle-gefiltert (`services/kg_graph_service.py`): eine Kante erscheint nur, wenn beide Endpunkte für den Anfragenden sichtbar sind. Die Reva-eigenen Endpunkte (`/trace`, `/me/mix`) sind in der Standalone-Renfield-Variante absichtlich nicht implementiert.

## Admin Maintenance Page

Zentrale Wartungsseite unter `/admin/maintenance` mit Re-Embedding, Keyword-Refresh, MCP-Status und weiteren Admin-Operationen.

## Zugriffskontrolle (RPBAC)

Optional aktivierbares JWT-basiertes Role-Permission System.

### Berechtigungs-Hierarchie
```
Knowledge Bases: kb.all > kb.shared > kb.own > kb.none
Smart Home:      ha.full > ha.control > ha.read > ha.none
Kameras:         cam.full > cam.view > cam.none
```

### Standard-Rollen

| Rolle | KB | Smart Home | Kameras |
|-------|----|-----------|---------|
| Admin | Vollzugriff | Vollzugriff | Vollzugriff |
| Familie | Eigene + geteilte | Vollzugriff | Ansehen |
| Gast | Keine | Nur lesen | Keine |

### Features
- JWT Access + Refresh Tokens
- Voice Authentication (optional)
- Resource Ownership (KBs, Konversationen)
- KB-Sharing zwischen Nutzern

### Konfiguration
```env
AUTH_ENABLED=false                 # Master-Switch (opt-in)
SECRET_KEY=changeme               # JWT Secret
VOICE_AUTH_ENABLED=false          # Stimm-Authentifizierung
```

Siehe [ACCESS_CONTROL.md](ACCESS_CONTROL.md) für vollständige Dokumentation.

## Mehrsprachigkeit (i18n)

### Unterstützte Sprachen
- **Deutsch (de)**: Vollständig übersetzt (Standard)
- **Englisch (en)**: Vollständig übersetzt

### Implementierung
- **react-i18next** für Frontend-Internationalisierung
- **Automatische Erkennung** der Browsersprache
- **Persistente Speicherung** in localStorage
- **Header-Dropdown** mit Globus-Icon für Sprachwechsel

### Übersetzte Bereiche
Navigation, Chat, Dashboard, Einstellungen, Geräteverwaltung, Benutzer & Rollen, Fehlermeldungen

Siehe [MULTILANGUAGE.md](MULTILANGUAGE.md) für die vollständige Anleitung.

## Dark Mode

- **Drei Modi**: Hell, Dunkel, System (folgt OS-Präferenz)
- **Tailwind CSS**: Class-basiertes Dark Mode mit `dark:` Prefix
- **ThemeContext**: React Context für globale Theme-Verwaltung
- **Persistenz**: Einstellung wird in localStorage gespeichert
- **FOUC-Prevention**: Kein Flackern durch Pre-Render-Script

## Progressive Web App

- **Multi-Platform**: Desktop, Tablet, Smartphone
- **Installierbar**: Home-Screen auf iOS/Android
- **Full-Screen**: Ohne Browser-UI
- **Responsive**: Mobile-First Design mit adaptivem Layout
- **Offline**: Funktioniert ohne Internet (Service Worker)

## Sicherheit

### Offline-First
- Alle Daten bleiben lokal, keine Cloud-Verbindungen für Kernfunktionen
- Keine Telemetrie, kein Tracking

### Rate Limiting
```env
# REST API
API_RATE_LIMIT_DEFAULT=100/minute
API_RATE_LIMIT_AUTH=10/minute       # Login/Register (strenger)
API_RATE_LIMIT_VOICE=30/minute
API_RATE_LIMIT_CHAT=60/minute
API_RATE_LIMIT_ADMIN=200/minute

# WebSocket
WS_RATE_LIMIT_PER_SECOND=50        # Ermöglicht Audio-Streaming
WS_RATE_LIMIT_PER_MINUTE=1000
WS_MAX_CONNECTIONS_PER_IP=10
WS_MAX_MESSAGE_SIZE=1000000         # 1MB
```

### Circuit Breaker

Automatische Ausfallsicherung für LLM- und Agent-Aufrufe:

| Zustand | Beschreibung |
|---------|-------------|
| CLOSED | Normal — Requests werden durchgeleitet |
| OPEN | Service ausgefallen — Requests sofort abgelehnt |
| HALF_OPEN | Recovery-Test — Einzelne Requests durchgelassen |

```env
CB_FAILURE_THRESHOLD=3              # Fehler bis OPEN
CB_LLM_RECOVERY_TIMEOUT=30.0       # LLM Recovery (Sekunden)
CB_AGENT_RECOVERY_TIMEOUT=60.0     # Agent Recovery (Sekunden)
```

### Secrets Management

Produktion nutzt Docker Compose file-based Secrets (`/run/secrets/`) statt `.env` für sensitive Werte. Pydantic Settings lädt aus `secrets_dir="/run/secrets"`, und MCP-Client injiziert Secrets in `os.environ` für YAML-Substitution und stdio-Subprozesse.

Siehe [SECRETS_MANAGEMENT.md](SECRETS_MANAGEMENT.md) für Details.

### Weitere Sicherheitsfeatures
- **CORS**: Konfigurierbare Origins (`CORS_ORIGINS`)
- **Trusted Proxies**: CIDR-basiert (`TRUSTED_PROXIES`)
- **WebSocket Auth**: Optional aktivierbar (`WS_AUTH_ENABLED`)
- **Passwort-Hashing**: bcrypt
- **MCP Response Limits**: Max Response-Größe begrenzt (`MCP_MAX_RESPONSE_SIZE`)

## Monitoring

### Prometheus Metrics

Opt-in Endpoint für Prometheus-kompatible Metriken:

```env
METRICS_ENABLED=false               # Aktivieren: true
```

Endpoint: `GET /metrics` (Prometheus Exposition Format)

### Health Checks
- `GET /health` — Backend Health Check
- `GET /api/mcp/status` — MCP-Server Status
- Docker Compose Health Checks für alle Container

### Logging
- **Strukturierte Logs**: Konfigurierbar via `LOG_LEVEL` (DEBUG, INFO, WARNING, ERROR)
- **Container-Logs**: `docker compose logs -f backend`

## LLM-Konfiguration

### Multi-Modell Support

Renfield unterstützt separate Modelle pro Aufgabe. Jedes kann auf einer anderen Ollama-Instanz laufen.

```env
# Basis
OLLAMA_URL=http://ollama:11434
OLLAMA_NUM_CTX=32768                # Context Window

# Pro Aufgabe
OLLAMA_CHAT_MODEL=qwen3:14b        # Chat-Antworten
OLLAMA_INTENT_MODEL=qwen3:8b       # Intent-Erkennung
OLLAMA_RAG_MODEL=qwen3:14b         # RAG-Antworten
OLLAMA_EMBED_MODEL=nomic-embed-text # Embeddings (768 Dim.)
OLLAMA_MODEL=llama3.2:3b           # Legacy Fallback

# Agent (optional)
AGENT_MODEL=                        # Separates Agent-Modell
AGENT_OLLAMA_URL=                   # Separate Ollama-Instanz
```

### Externe Ollama-Instanz

Ollama kann auf einem separaten GPU-Server laufen:

```env
OLLAMA_URL=http://cuda.local:11434
```

### LLM Client Factory

Alle Services nutzen eine zentrale Factory (`utils/llm_client.py`) mit URL-basiertem Caching (gleiche URL → gleiche Client-Instanz) und einem `LLMClient` Protocol.

### Ollama Fallback

Connect-Timeout (10s) verhindert Hänger bei unerreichbaren Hosts. Bei `ConnectError`/`ConnectTimeout` wird automatisch auf `OLLAMA_FALLBACK_URL` umgeschwenkt — nützlich wenn der GPU-Server (z.B. cuda.local) offline ist und die Host-Ollama-Instanz einspringen soll.

```env
OLLAMA_FALLBACK_URL=http://host.docker.internal:11434
```

## Presence Detection

### Übersicht

Raum-basierte Präsenzerkennung aus drei Quellen:

| Quelle | Auslöser | Hysterese | Konfidenz |
|--------|----------|-----------|-----------|
| **BLE-Scanning** | Satellit erkennt BLE-Gerät (Telefon, Uhr) | Ja (N Scans) | RSSI-basiert |
| **Classic-BT-Scanning** | Satellit erkennt Classic-BT-Gerät (Telefon) per `hcitool name` | Ja (N Scans) | RSSI-basiert (gedrosselt) |
| **Voice Presence** | Sprechererkennung identifiziert Nutzer | Nein (sofort) | 1.0 |
| **Web Auth Presence** | Authentifizierter Nutzer mit Raum-Kontext | Nein (sofort) | 1.0 |

Voice- und Auth-Presence umgehen die BLE-Hysterese — eine einzelne Interaktion verschiebt den Nutzer sofort und feuert Enter/Leave-Hooks.

### BLE-Scanning

Satelliten scannen per `bleak` nach registrierten BLE-Geräten und melden RSSI-Werte per WebSocket. Backend `PresenceService` nutzt "stärkstes RSSI gewinnt" + Hysterese (N aufeinanderfolgende Scans) um Raum-Flicker zu verhindern.

### Classic-BT-Scanning

Telefone, die ihre BLE-MAC rotieren (Apple), werden zusätzlich per Classic-BT erkannt: `hcitool name <MAC>` (binäre An/Abwesenheit). Da ein Name-Request kein Signal liefert, las dies früher ein konstantes synthetisches `-50` — bei zwei Satelliten ein Unentschieden, das den Raum „flattern" ließ. Der Satellit liest jetzt ein **echtes** RSSI über eine kurzlebige ACL-Verbindung (`hcitool cc/rssi/dc` via passwortloses `sudo`), **gedrosselt** auf einmal pro `classic_rssi_interval` (Default 300 s) pro Gerät — häufiges Verbinden lässt das Telefon sonst aufhören, auf `name` zu antworten (Präsenz fällt auf „abwesend"). Das golden-range-RSSI wird auf die Backend-Skala abgebildet und gepuffert; bei Read-Fehler greift der synthetische Fallback, sodass Präsenz nie verloren geht. Schalter: `ble.classic_rssi` (siehe `docs/ENVIRONMENT_VARIABLES.md`).

### Voice Presence

Wenn die Sprechererkennung einen Nutzer auf einem Satelliten identifiziert, wird `register_voice_presence()` aufgerufen. Dies aktualisiert den Raum sofort und feuert die entsprechenden Hooks (`presence_enter_room`, `presence_leave_room`, etc.).

### Web Auth Presence

Authentifizierte Nutzer, die über die Web-Oberfläche von einem raumzugewiesenen Gerät interagieren, aktualisieren ebenfalls ihre Position über denselben `register_voice_presence()`-Pfad.

### Privacy-Aware TTS

Benachrichtigungen tragen `privacy` und `target_user_id` Metadaten. Die Privacy-Gate prüft die Raumbelegung vor TTS-Ausgabe:
- `public` — TTS immer aktiv
- `personal` — TTS nur wenn alle Raumbewohner Haushaltsmitglieder sind
- `confidential` — TTS nur wenn der Zielnutzer allein im Raum ist

### Nachricht an eine Person ausrichten (Message Relay)

"Sag ihm/ihr, …" → der Agent ermittelt per Presence den Raum der Person und sagt
die Nachricht dort an (`internal.get_user_location` → `internal.announce_in_room`,
LLM-orchestriert). **Fail-closed Privacy-Gate:** eine `personal`-Nachricht wird
nur laut ausgegeben, wenn jeder erkennbar Anwesende Empfänger ist; sonst neutrale
"Nachricht wartet"-Ansage + Bestätigung-mit-`force`. Zusätzlich (wenn eine
Satelliten-Kamera im Raum ist) ein **Kamera-Belegungs-Check** per Vision-Modell,
um nicht per BLE getrackte Anwesende zu erkennen. Details: `docs/MESSAGE_RELAY.md`.

### Automation-Hooks

Presence-Events feuern Hooks für externe Automatisierung (z.B. n8n-Workflows):
- `presence_enter_room` — Nutzer betritt Raum
- `presence_leave_room` — Nutzer verlässt Raum
- `presence_first_arrived` — Erster Nutzer erkannt (Haus war leer)
- `presence_last_left` — Letzter Bewohner hat Raum verlassen

Optional: Webhook-Dispatch an externe URL (`PRESENCE_WEBHOOK_URL`).

### Konfiguration

```env
PRESENCE_ENABLED=false
PRESENCE_STALE_TIMEOUT=120
PRESENCE_HYSTERESIS_SCANS=2
PRESENCE_RSSI_THRESHOLD=-80
PRESENCE_HOUSEHOLD_ROLES="Admin,Familie"
PRESENCE_WEBHOOK_URL=""
PRESENCE_WEBHOOK_SECRET=""
```

### Endpunkte

- `GET /api/presence/rooms` — Alle Räume mit Anwesenden
- `GET /api/presence/user/{id}` — Standort + allein?
- `POST /api/presence/devices` — BLE-Gerät registrieren (Admin)

### Präsenz-Historie (persistent, überlebt Neustarts)

Jeder Raumwechsel (`enter`/`leave`) wird dauerhaft in `presence_events` protokolliert (inkl. `satellite_id`). Abfragbar als Zeitleiste — die In-Memory-Live-Präsenz bleibt davon unberührt.

- **Chat-Tool** `internal.presence_history`: "Wo war Eduard heute?", "Wer war um 20 Uhr im Wohnzimmer?", "Wann war ich zuletzt in der Küche?".
- **Routen** `GET /api/presence/analytics/{timeline,last-seen-by-room,room-window}`. Fremduser-Abfragen erfordern `ROOMS_MANAGE` (IDOR-Schutz); Selbst-Abfragen frei.
- Flag `PRESENCE_HISTORY_ENABLED` (Default an, rein additiv).

### Bluetooth-Geräte-Scan aus dem Chat

"Scanne die Bluetooth-Geräte" → das Tool `internal.bluetooth_scan` fächert eine Discovery an alle Satelliten aus (Backend hat keine BT-Hardware). Jeder Satellit führt eine Classic-BT-Inquiry (`hcitool scan`) **und** eine BLE-Discovery (`BleakScanner`) aus; das Backend dedupliziert per MAC, behält das stärkste RSSI, sammelt pro Raum und löst die Herstellerkennung (OUI→Vendor) auf. Ergebnis: Adresse, Name (oft leer), Raum/Satellit, RSSI, Hersteller.

Hinweise: nur **sichtbare/advertisende** Geräte erscheinen (die meisten Handys sind standardmäßig nicht auffindbar); ein Scan dauert ~15-30 s. Opt-in via `BT_SCAN_ENABLED` (zählt alle Geräte im Haus auf → Privatsphäre).

### Telefon-Präsenz per IRK (rotierende BLE-Adressen auflösen)

Moderne Telefone senden eine **rotierende** BLE-Adresse (Resolvable Private Address), daher kann eine feste MAC-Whitelist sie nicht verfolgen. Renfield löst die wechselnde Adresse über den **Identity Resolving Key (IRK)** des Geräts auf eine stabile Identität auf — dasselbe Verfahren wie Home Assistants *Private BLE Device* / Bermuda. **Keine App, keine Zusatz-Hardware.** Reines Software-Matching (AES-128, BLE-Spec `ah`-Hash) auf den gescannten Advertisements → funktioniert auf jedem Adapter (kein rohes HCI, kein Classic-BT, kein Pairing nötig zur Auflösung).

**IRK-Erfassung über die UI** ("Telefon für Präsenz koppeln", auf der Anwesenheits-Seite): Admin wählt Nutzer + Satellit + Bezeichnung → der gewählte Satellit öffnet ein einmaliges, zeitlich begrenztes Kopplungsfenster (auto-akzeptierender BlueZ-Agent). Der Nutzer koppelt das Telefon über **Einstellungen → Bluetooth → „Renfield \<Raum\>"**; der Satellit liest den dabei ausgetauschten IRK aus BlueZ, sichert den Adapter wieder ab und meldet ihn zurück. Der IRK wird **verschlüsselt at rest** gespeichert (Fernet aus `SECRET_KEY`) und über die WS-Verbindung an die Satelliten verteilt; er wird **nie** in einer API-Antwort oder im Log im Klartext zurückgegeben. Verwaltung: `GET/POST/PATCH/DELETE /api/presence/irks` (Admin). Entkoppeln/Widerrufen am Telefon **oder** über „Entfernen" in der UI.

**Kontinuierliches Scannen** (`ble.continuous`, opt-in pro Satellit): ein dauerhaft laufender Scanner mit EWMA-geglättetem RSSI statt periodischer Bursts → geringere Latenz + ruhigere Raumzuordnung; Standard bleibt periodisch. Bei mehreren Satelliten mit aktivem BLE-Stack führt das Backend eine **Multi-Satelliten-Raumzuordnung** durch (mittleres RSSI + Bonus je zusätzlichem Satelliten + Hysterese). Design + Status: `docs/design/ble-presence-improvement.md`.

## Tageszeit-Bewusstsein & LED-Nachtdimmung

### Tag/Nacht-Bewusstsein

Renfield kennt die Tageszeit (Tag / Abend / Nacht, aus konfigurierbaren Uhrzeit-Fenstern in der lokalen Zeitzone). Der Agent bekommt in **jedem** Prompt eine `ZEITKONTEXT`-Zeile ("Aktuelle Zeit: 22:14 Uhr (Nacht, Donnerstag)"), kann seine Antworten also tageszeitgerecht anpassen. Ein Hintergrund-Watcher (alle 5 Min) feuert bei Übergängen den `daypart_changed`-Hook, an den sich weitere Funktionen hängen. Konfiguration: `DAYPART_NIGHT_START`/`DAYPART_NIGHT_END`/`DAYPART_EVENING_START`/`DAYPART_TIMEZONE`.

### LED-Nachtdimmung der Satelliten

Bei Einbruch der Nacht (Übergang zu "Nacht") dimmt das Backend automatisch die LED-Ringe aller Satelliten (`LED_NIGHT_BRIGHTNESS`=5); jeder Übergang **aus** der Nacht stellt die Tageshelligkeit wieder her (`LED_DAY_BRIGHTNESS`=20). Symmetrisch — kein separates Morgen-Handling nötig. Die Animationen (Wake-Word, Zuhören, Sprechen) laufen weiter, nur gedimmt. Ein Satellit, der sich nachts neu verbindet, kommt bereits gedimmt hoch (die Helligkeit reist im `register_ack` mit). Backend-getrieben (eine Quelle der Wahrheit), reagiert auf `daypart_changed`.

## Hook System (Extension API)

Async Hook-System für die Open-Core-Architektur. Externe Pakete registrieren Callbacks an 10 Lifecycle-Stellen — renfield crasht nie wegen eines Plugin-Fehlers.

**Aktivierung:**
```bash
# Ein Plugin:
PLUGIN_MODULE=example_pkg.plugin:register
# Mehrere (komma-separiert, dedupliziert, fehlertolerant):
PLUGIN_MODULES=pkg_a.plugin:register,pkg_b.plugin:register
```

**Verfügbare Hook Events:**

| Event | Zweck | Ausführung |
|-------|-------|------------|
| `startup` | Extension-Services initialisieren | Awaited beim Start |
| `shutdown` | Ressourcen aufräumen | Awaited beim Shutdown |
| `register_routes` | FastAPI-Routen hinzufügen | Awaited beim Start |
| `register_tools` | Agent-Tools registrieren | Background Task |
| `post_message` | Nachrichten nachverarbeiten (z.B. Graph-Extraktion) | Fire-and-forget |
| `retrieve_context` | Zusätzlichen LLM-Kontext injizieren | Awaited, Ergebnis angehängt |
| `presence_enter_room` | Nutzer betritt Raum | Fire-and-forget |
| `presence_leave_room` | Nutzer verlässt Raum | Fire-and-forget |
| `presence_first_arrived` | Erster Nutzer im Haus erkannt | Fire-and-forget |
| `presence_last_left` | Letzter Bewohner hat Raum verlassen | Fire-and-forget |

**Key files:** `utils/hooks.py`, `api/lifecycle.py` (Plugin-Loading)

> **Hinweis:** Hooks sind für tiefe Integrationen gedacht (Kontext-Injektion, Post-Processing, Custom Routes). Für einfache Tool-Integrationen bleiben MCP-Server der bevorzugte Weg.

---

Ausführliche Entwickler-Dokumentation (Architektur, Patterns, Commands) in [CLAUDE.md](../CLAUDE.md).
