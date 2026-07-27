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

### Satelliten-Enrollment (Security Review H1, `SATELLITE_ENROLLMENT_ENABLED`, opt-in/dark)
- **Per-Satellite-Identität**: ein Satellit weist sich mit einem eigenen 256-bit-PSK (server-seitig nur als bcrypt-Hash in `satellites`, Migration `pc20260624`) im register-Frame aus, statt eine `satellite_id` nur zu **behaupten**. Schließt die H1-Wurzel: bislang konnte ein beliebiges LAN-Gerät als `sat-wohnzimmer` registrieren, den echten Satelliten verdrängen und den IRK-Push (Standort-Tracking-Schlüssel) abgreifen.
- **Effektiv-Modus**: aus (Default, Legacy, byte-identisch) → PERMISSIVE (Soak: vorgelegter PSK wird geprüft, falsch/unbekannt/widerrufen abgelehnt, kein PSK erlaubt-aber-geloggt; IRKs nur an verifizierte Sats) → ENFORCING (kein gültiger PSK → abgelehnt). Der Übergang ist ein **Auto-Flip mit persistenter Verriegelung** (`SATELLITE_ENROLLMENT_AUTOFLIP_ENABLED`): kippt erst, wenn JEDE eingeschriebene Zeile sich einmal authentifiziert hat, und öffnet die Flotte nie wieder von selbst.
- **Verwaltung**: Admin-UI auf der Satelliten-Seite (`/api/satellite-enrollment`, ADMIN-gated; Token wird **einmal** angezeigt) oder `bin/enroll_satellite.py`. Provisionierung per gitignored Ansible-host_var `satellite_enrollment_token` bzw. per-Pod-k8s-Secret. Eviction-Guard: ein unauthentifizierter Neuzugang verdrängt keinen eingeschriebenen Amtsinhaber. Gestaffelter Rollout + Break-glass in [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md).

### Signierte OTA-Pakete (Security Review H6, `SATELLITE_OTA_REQUIRE_SIGNATURE` / `update.require_signature`, opt-in/dark)
- **Code-Authentizität statt nur Integrität**: ein OTA-Release wird über ein **offline mit Ed25519 signiertes Quell-Manifest** (Version + Pro-Datei-SHA256) abgesichert. Der Satellit prüft Signatur (gegen gepinnte Public Keys), Datei-Hashes und Version **vor** dem Install — eine kompromittierte/gefälschte Backend-Instanz kann keinen Code ausrollen, den sie nicht offline hat signieren lassen. Der private Key bleibt OFFLINE; das Backend **leitet** Manifest+Signatur nur weiter (`satellite_update_service`), kann nicht selbst signieren.
- **Modell „signiertes Quell-Manifest"** (statt Tarball-Signatur), weil das Backend das Tarball **dynamisch** baut. Signiert mit `bin/sign_satellite_release.py` (`--gen-key`/`--sign`/`--verify`); Public Keys sind git-safe (group_vars `satellite_release_pubkeys`, mehrere = Rotation). Die TLS-Prüfung des Downloads (H6-Surgical) wird Defense-in-depth.
- **Dark by default**: kein committetes `RELEASE_MANIFEST.json` + `require_signature=false` → Backend leitet `None` weiter, Satellit prüft nur Checksum (Legacy, byte-identisch). Eine **vorhandene aber ungültige** Signatur bricht den Install immer ab. Rollout + Break-glass in [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md).

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

## Projekte (Business-Instanz, `projects_enabled`, opt-in/dark)

Ein minimales **Projekt**-Modell für Business-Instanzen (Phase 1): jedes Projekt besitzt genau **eine** eigene KnowledgeBase (1:1, tier-gescoped auf `circle_tier`, Default 2 = Team/Haushalt), sodass Chat- und Dokument-Verlauf pro Projekt getrennt für RAG nutzbar werden. CRUD über `/api/projects` (`POST` legt Projekt + KB an, `GET` listet owner-scoped, `GET /{id}` owner-gated 404, `DELETE` entfernt nur die Projekt-Zeile und **behält** KB + Dokumente). Die `/projects`-Seite (Liste + Anlege-Formular) ist über dasselbe Flag gegated.

- **Dark by default**: `PROJECTS_ENABLED=false` → alle Routen 404, kein Nav-Eintrag, Household-Instanz byte-identisch. Kein Codebase-Fork — rein additiv hinter dem Flag.
- **Owner-scoped**: Auth an → nur eigene Projekte; `AUTH_ENABLED=false` (Single-User) sieht alle. Der Anleger besitzt Projekt und KB.
- **1:1-Invariante** in `services/project_service.py`: frische KB pro Projekt, kollisionssichere KB-Namen über die Projekt-ID, Projekt + KB in einer Transaktion.
- **Später (nicht Teil dieser Phase)**: Notizen (als 5. atom_type geplant). Protokoll-Pipeline (Zusammenfassung/Entscheidungen/Action-Items) + Meeting-Transkription sind inzwischen gebaut — siehe unten.

Migration `pc20260713_projects`.

### Projekt-Timeline (Phase 4A, `projects_enabled`, migration `pc20260719_project_links`)

Jedes Projekt hat unter `/projects/{id}` einen **chronologischen Verlauf** (neueste zuerst), der die projekt-zugehörigen Artefakte zu EINEM Feed zusammenführt: **Dokumente** (aus der 1:1-KB des Projekts), **Besprechungen** (`Meeting.project_id`), **Entscheidungen** (aus den bestätigten Minutes eines Meetings flach gezogen) und **Chats** (`Conversation.project_id`). `GET /api/projects/{id}/timeline` (owner-gated 404, paginiert) fragt jede Quelle einzeln ab und merge-sortiert in Python (heterogene Zeilen teilen keine SQL-Projektion), per-Quelle gedeckelt — spiegelt das Presence-Analytics-Single-Scan-Muster als Multi-Source-Merge. Migration `pc20260719_project_links` fügt ein nullable `project_id` (FK `ON DELETE SET NULL`) an `meetings` + `conversations`. Meeting-Upload akzeptiert ein optionales owner-validiertes `project_id`, und **`PATCH /api/meetings/{id}`** (owner-gated 404, `project_id=null` löst die Verknüpfung) erlaubt das nachträgliche Ändern/Entfernen. **Frontend**: `ProjectDetailPage` (Klick-Durchgriff aus der Projektliste, Timeline-Zeilen mit Icon je Typ + Deep-Links nach `/knowledge?doc=` / `/meetings`); auf der **Meetings-Seite** ein Projekt-Auswahlfeld im Upload-Formular UND pro Besprechungskarte (mit „— Kein Projekt —"), das nur erscheint, wo Projekte existieren (also `projects_enabled`-Instanzen) — so bleibt der Haushalt unverändert. Dokumente füllen die Timeline aus der Projekt-KB. Der analoge Projekt-Picker in der **Chat-UI** (`Conversation.project_id`) bleibt ein kleiner Follow-up. Dark by default.

## Notizen als 5. atom_type (Phase 4B, `notes_enabled`, opt-in/dark)

Handgeschriebene atomare **Notizen** (Markdown) als **erstklassiger Atom** (`atom_type='note'`): kreis-getiert, im polymorphen RRF-Store, mit Badge im `/brain`, und `[[bidirektionale Verlinkungen]]` auf dem KG-Substrat. Design: [`design/notes-atom.md`](design/notes-atom.md).

- **Modell** (`notes`-Tabelle, Migration `pc20260720_notes`): NOT-NULL `atom_id` (via `AtomService.create_with_source` — geboren als Atom, wie `document_facts`), `owner_user_id`, `project_id` (4A-Timeline-Synergie), `title` (der `[[link]]`-Schlüssel, unique je Owner), Markdown-`body`, GENERATED mehrsprachige FTS `search_vector` und ein **dense `embedding`** (`halfvec`, HNSW, Migration `pc20260721_notes_embedding`). Registriert in `AtomService._table_for_atom_type` + `_source_id_for`, `circle_sql.notes_circles_filter`.
- **Retrieval**: `NoteRetrieval` ist eine 8. Quelle in `PolymorphicAtomStore` (gated `notes_enabled`) → Notizen erscheinen in `/brain` / einer `/wissen`-Lens ohne Route-Änderung. **Hybrid** (`notes_semantic_search_enabled`, default an): ein dichter Embedding-Zweig (halfvec-Cosine) wird per RRF mit der Postgres-FTS fusioniert; sqlite fällt auf LIKE zurück. `note_service` bettet Titel+Body beim Schreiben ein (best-effort, Postgres-only, off-request via BackgroundTask) und degradiert auf FTS, wenn das Embed-Modell ausfällt. Backfill: `bin/backfill_note_embeddings.py`. Alles kreis-gefiltert.
- **`[[links]]`** (4B.2, KG-Substrat, **Option A**): jede Notiz spiegelt auf eine `kg_entities`-Zeile (`entity_type='note'`), jeder `[[Target]]` wird eine `kg_relations`-Zeile (`predicate='note_link'`) — nutzt `resolve_entity` (Dedup + Dangling-Stub) + `save_relation` + graph_expansion + den 3D-`/wissen`-Graphen wieder, KEINE parallele `note_links`-Tabelle. Owner-scoped über `resolve_entity(user_id=owner, entity_type='note', match_entity_type=True)` (zwei Owner mit „Roadmap" kollidieren nicht; eine Notiz „Bonn" verlinkt nie die Ort-Entität). **note→note** im MVP (note→beliebige-KG-Entität ist v2). `GET /api/notes/{id}/links` → {outgoing, backlinks}. Ein projekt-gescopetes Note erscheint auf der `/projects/{id}`-Timeline.
- **Frontend**: `/notes` (Anlegen + Liste + Inline-Bearbeiten + Löschen), `NoteLinksPanel` (Outgoing- + Backlink-Chips, Dangling gedämpft), Badge im `/brain`. **CRUD `/api/notes`** owner-gated 404, 409 bei Titel-Kollision. Dark by default.
- **Editor-Politur (4B.3)**: Markdown-Vorschau + Karten-Rendering via `react-markdown` + `remark-gfm` + ein `remarkWikilink`-Plugin (`[[Target]]`→Chips; `NoteMarkdown.tsx` rendert zu echten React-Elementen — kein Roh-HTML, CSP-sicher), ein `[[ ]]`-Titel-Typeahead-Editor (`NoteBodyEditor.tsx`) und eine **`/wissen` Notizen-Lens** (`pages/wissen/lenses.ts`; bei aktivem Unified-Workspace leitet `/notes` in die Lens um und der flache `nav.notes`-Eintrag entfällt, sonst die eigenständige `/notes`-Seite). Design: [`design/notes-atom.md`](design/notes-atom.md).

## Meeting-Transkription + Diarisierung (§2, `MEETING_TRANSCRIPTION_ENABLED` / voice-server `MEETING_ENABLED`, dark by default)

Eine Mehrsprecher-Aufnahme hochladen → sprecher-attribuiertes Transkript in der Wissensbasis (RAG). Spike-gated gebaut (die `tests/eval/diarization/gates.yaml`-Gates bestanden 2026-07-14). Design: [`design/meeting-transcription.md`](design/meeting-transcription.md).

- **Upload** `POST /api/meetings/transcribe`: Einwilligung (`consent_confirmed`) ist Pflicht (sonst 422); mehrstündiges Audio wird chunk-weise auf die geteilte Uploads-PVC gestreamt (nie ganz im RAM), 202 `{id}`. Owner-gated Status-Poll `GET /api/meetings/{id}` (+ `/segments`), owner-gated Liste `GET /api/meetings` (neueste zuerst, 1-200) und owner-gated `DELETE /api/meetings/{id}` (Transkript-Dokument + Audio + Zeile via `purge_meeting`).
- **Sprache** (`language`, optional): pro Besprechung wählbar — `auto` (Whisper erkennt automatisch) oder ein ISO-Code (`de`/`en`). Das Upload-Formular bietet ein Auswahlfeld (Standard **Auto**). Ohne Angabe greift der Server-Default (`de`). **Hintergrund:** der Meeting-ASR-Pfad war fest auf Deutsch verdrahtet, sodass englische Aufnahmen als halluziniertes Deutsch transkribiert wurden — für die gemischt DE/EN-sprachige Geschäftskundschaft (xidra) ist die Sprache jetzt frei wählbar (Spalte `meetings.language`, Migration `pc20260722c`; im voice-server-Endpoint durchgereicht).
- **Worker** (`workers/meeting_worker.py`, `k8s/meeting-worker.yaml`, replicas:1): eigener Redis-Stream `renfield:tasks:meeting`; klont den Document-Worker plus einen **Row-Level-`status`+`heartbeat_at`-In-Flight-Guard** (4h-Jobs), Poison-Pill-Quarantäne und eine **4xx-terminal / 5xx-retryable**-Klassifikation (eine kaputte Aufnahme scheitert schnell statt die GPU endlos zu belasten).
- **voice-server** `POST /transcribe-meeting`: pyannote-Diarisierung + faster-whisper-Wort-Timestamps + eine **reine, fixture-getestete** `align_words_to_segments` + per-Cluster-ECAPA im ONNX-`/stt`-Raum. pyannote lädt nur bei `MEETING_ENABLED`; das Image backt GPU-torch cu128 + das pyannote-Modell (BuildKit-Secret).
- **Attribution**: ehrliche Pseudonyme ("Sprecher N") + Ein-Klick-Human-Labeling (`POST /api/meetings/{id}/relabel` → Re-Render → **Reindex in-place**, stabile `transcript_document_id`). Auto-Match ist DEFERRED (`meeting_auto_match_enabled` dark).
- **Ingest**: in eine dedizierte „Meetings"-KB via `folder_ingest.ingest_document` mit `source="meeting_transcript"` (neue `documents.source`-Spalte, Migration `pc20260714b`), das **Schicht-A abschaltet** (D14 — keine Phantom-Fristen aus Small Talk) und `file_to_paperless=False`.
- **Retention**: `retention_until` beim Upload aus `meeting_retention_days` gestempelt; ein täglicher Job (`services/meeting_retention.py`) löscht abgelaufene Transkripte (über den Dokument-Lösch-Pfad) + Segmente + Audio, und räumt Audio abgeschlossener/fehlgeschlagener Meetings nach `meeting_audio_grace_days` (`meeting_keep_audio` opt-in).
- **Frontend** (`pages/MeetingsPage.tsx`, PR-3): flag-gated auf `meeting_transcription_enabled` (aus `/api/config/features` — Nav+Route fehlen, wenn aus). Upload-Formular mit **Pflicht-Einwilligungs-Checkbox**, Status-Liste, die nur pollt, solange eine Aufnahme pending/processing ist, Aufklappen einer fertigen Besprechung → Transkript-Turns + Sprecher-Umbenennung + Deep-Link zu `/knowledge?doc=`.
- **Robustheit** (nach dem ersten produktiven Upload gehärtet): der Upload-Aufruf setzt `timeout: 0`, weil der geteilte `apiClient` sonst eine mehrere-hundert-MB-Aufnahme nach dem 30-s-Default abbricht (client-seitig, ohne Server-Fehler — #1009). Und die voice-server-**GPU-OOM-Ursache** war **ECAPA**, nicht Whisper: das Meeting reicht das GESAMTE zusammengefügte Audio eines Sprechers ans Embedding, dessen onnxruntime-Arena die Spitze hält → `speaker_service.cap_clip` deckelt die ECAPA-Eingabe auf ein zentriertes 30-s-Fenster (`speaker_embed_max_seconds`, voice-server v0.3.6 #1012; verifiziert an wiederholten 32-min-Aufnahmen). Chunked-Transcription (`meeting_chunk_seconds`, v0.3.5) bleibt als Backstop für pathologisch lange Aufnahmen.
- **Status**: Migrationen `pc20260714_meetings` + `pc20260714b_document_source`. `MEETING_TRANSCRIPTION_ENABLED` ist auf Haushalt (`renfield`) **und** xidra umgelegt und Ende-zu-Ende verifiziert; der Config-Default bleibt `false` (neue Instanzen starten dark).

### Meeting-Protokoll (§2 Phase 3, `MEETING_MINUTES_ENABLED` / Frontend `meeting_minutes_enabled`, dark by default)

Aus einem fertigen, sprecher-attribuierten Transkript ein strukturiertes **Protokoll** erzeugen — Zusammenfassung + Entscheidungen + Aufgaben (Action-Items) — mit **Human-Confirm-Gate**, bevor irgendetwas ins Transkript-Dokument übernommen wird. Design: [`design/meeting-minutes.md`](design/meeting-minutes.md). Backend PR #984, Frontend PR-B #986.

- **Lifecycle** `minutes_status`: `none` → `draft` → `confirmed`. `POST /api/meetings/{id}/minutes/generate` (409, wenn nicht `completed`) lässt eine **`MinutesExtractor`**-LLM-Passage (spiegelt `schicht_a_extractor`, strikte JSON-Schema-Prompts `prompts/meeting_minutes.yaml`) einen Entwurf erzeugen; `GET/PUT …/minutes` liest/editiert; `POST …/minutes/confirm` (409, wenn nicht `draft`) rendert das Protokoll **in dasselbe Transkript-Dokument** (gleicher stabiler `transcript_document_id`-Reindex-Pfad wie die Re-Attribution — kein zweiter Ingest); `DELETE …/minutes` verwirft den Entwurf. Alles owner-gated 404. Neue Spalten `minutes`/`minutes_status`/`minutes_generated_at`/`minutes_confirmed_at` (Migration `pc20260718_meeting_minutes`, additiv).
- **Action-Items sind meeting-scoped** — bewusst KEINE Fristen/Obligations für die `/brain/fristen`-Agenda (`due_hint` ist ein WORTLAUT-Hinweis, kein berechnetes Datum).
- **Frontend** (`MinutesPanel` in `pages/MeetingsPage.tsx`, flag-gated auf `meeting_minutes_enabled`): auf einer aufgeklappten fertigen Besprechung — `none` → „Protokoll erzeugen"; `draft` → editierbare Zusammenfassung + Entscheidungen[] + Aufgaben[] mit Add/Remove, „Entwurf speichern"/„Bestätigen"/„Neu erzeugen"/„Verwerfen"; `confirmed` → Read-only + „Bestätigt"-Badge + „Bearbeiten". **Bestätigen speichert offene Edits automatisch zuerst** (kein stiller Datenverlust). Typed JSON in/out (React-Escape-Grenze), keine Model-HTML.
- **Dark by default** → `meeting_minutes_enabled=false` auf beiden Instanzen; Panel fehlt, bis der Flag umgelegt wird.

### Meeting KG + Sprecher-Identität — Redesign (§2, Design [`design/meeting-kg-and-speaker-identity.md`](design/meeting-kg-and-speaker-identity.md))

Ein Meeting ist eine hochwertige KG-Quelle, aber der generische Ingest-Pfad verrauscht den Graphen (Pseudonym-Junk, Chunk-vs-Turn-Attribution) und der bestätigte Status/die Action-Points enden im JSON-Blob. Der Redesign behandelt ein Meeting als **eine strukturierte, sprecher-bewusste, human-confirmed Extraktion** (Tracks A–D). **Phase 0** (erster Schritt, low-risk):

- **Sprecher-Pseudonym-Strip**: `kg_post_document_ingest_hook` entfernt die `Sprecher N:`-Zeilenpräfixe aus Meeting-Transkript-Chunks (gated auf `source == "meeting_transcript"`), bevor der KG-Extraktor läuft — so wird „Sprecher 1" nicht als Junk-Person-Entität geminted, die über Meetings hinweg kollidiert. Bewusst eng: nur die Pseudonyme, ein human-umbenannter echter Name bleibt (legitime Cross-Meeting-Entität). Der generische Hook läuft weiter (Track B ersetzt ihn später durch die bestätigte sprecher-bewusste Passage).
- **UX „Deliverable first"** (Track D): die Meeting-Liste trägt einen **„Protokoll: Entwurf bereit"-Badge** auf der Karte (`minutes_status='draft'`, auf `GET /api/meetings` mitgeliefert; verlinkt auf die Detailseite). Die eigentliche Track-D-Oberfläche ist die **dedizierte Meeting-Detailseite** `/meetings/{id}` (`pages/MeetingDetailPage.tsx`, `useMeeting` → `GET /api/meetings/{id}`): das **Protokoll (Zusammenfassung/Entscheidungen/Aufgaben) ist die Standard-Ansicht ganz oben**, das rohe Transkript ist sekundär und **standardmäßig eingeklappt** darunter; ein prominentes **Entwurf-Bestätigungs-Banner** (`draftNudge`) verhindert, dass ein erzeugter Entwurf unbestätigt verrottet. Die **Listenkarte einer fertigen Besprechung ist jetzt ein Link auf die Detailseite** (kein Inline-Aufklappen mehr — die Karten-Affordanz war die erste Track-D-Scheibe). Geteilte Bausteine unter `components/meetings/` (`StatusBadge`/`TranscriptView`/`MinutesPanel`/`ProjectSelect`), von Liste und Detailseite gemeinsam genutzt. Gated wie zuvor (`meeting_transcription_enabled` für Route, `meeting_minutes_enabled` für die Protokoll-First-Ansicht).

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
- **knowledge_search Agent Tool** — Internes Tool im Agent Loop für kombinierte Suche über RAG-Dokumente und MCP-Quellen (Paperless). Liefert zusätzlich **circle-gefilterte Schicht-A-Fakten** (Steuernummer/IBAN/Aussteller/Fristen aus `DocumentFactRetrieval`) als eigenen `FAKTEN`-Block, sodass der Agent den präzisen Wert zitiert statt der Passage (aktiv bei `schicht_a_extraction_enabled`; Quell-Dokumenttitel separat circle-gefiltert, damit ein tier-übersteuerter Fakt keinen privaten Dokumenttitel preisgibt)
- **OCR-Engine (Tesseract, Standard)** — Garbled/gescannte PDFs werden automatisch neu-OCRt (`force_full_page_ocr`); Standard-Engine ist **Tesseract** (deu+eng), umschaltbar via `RAG_OCR_ENGINE` (`tesseract`|`easyocr`) und **fail-safe** auf EasyOCR, wenn die Tesseract-Runtime (CLI+deu/eng-Traineddata oder tesserocr-Binding) fehlt — Ingest stürzt nie ab. Ein 148-Dokument-Eval über den Flagged-Korpus (`bin/run_ocr_engine_eval.py --all-flagged`) zeigte Tesseract klar überlegen: 111/148 verbessert, nur 8 verschlechtert, Quality-Gate-Drop 0.68→0.30, kein Speed-Nachteil
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
- **Mehrsprachiger Haushalt**: Sowohl Satelliten als auch der Browser-Detektor laden **alle** gepushten Wake-Words gleichzeitig (kommagetrennter Satz, z.B. `renfield_de` + `renfield_en`) — nicht nur das erste.

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
2. **OCR-Qualitätsprüfung** — Heuristische Bewertung (1-5) ohne LLM via `utils/ocr_quality.score_ocr_quality` (geteilt mit der Ingest-Pipeline, damit die „Zeichensalat"-Definition nicht auseinanderdriftet): bewertet fehlende Leerzeichen (zusammengelaufene Wörter), hohe Sonderzeichen-Dichte, Fragmentierung (sehr kurze Zeilen) **und Zeichen-Level-Garbling bei normalem Wortabstand** (`_garbled_token_ratio`: Anteil word-artiger Tokens mit interner Interpunktion `Bez:-ihl` oder vokallosen Runs `KIJ` — bei schwerem Garble Score-Cap auf ≤2). Letzteres fängt genau den Fall, den die drei anderen Signale verfehlen: ein rotierter/schlechter Scan OCR't zu *echt aussehendem* Abstand, aber falschen Buchstaben (gemessen an einem realen Beleg: Garble-Ratio 0.46, sauberer Text 0.00). Eine „wiederholte Zeichen"-Regel gibt es bewusst **nicht** — am realen Korpus erzeugte sie nur Fehlalarme.
3. **LLM-Analyse** — 8 Validierungsfelder: Titel, Korrespondent, Dokumenttyp, Tags, Speicherpfad, Datum, Sprache, Archivstatus
4. **Fix-Modi**: `review` (manuelle Freigabe im Admin UI), `auto_threshold` (ab Konfidenz ≥ Schwellwert), `auto_all`

**Metadaten-Autocreate beim Anwenden.** Beim Anwenden eines Fixes werden Korrespondent / Dokumenttyp / Tags durch die geteilten resolve-**oder-CREATE**-Helfer der Folder-Ingest-Leg geleitet: ein passender (fuzzy) Taxonomie-Eintrag wird zugewiesen, ein genuin neuer **angelegt** — so bleibt das Audit nicht auf einem frischen/leeren Paperless stecken (der Fuzzy-Guardrail verhindert Beinahe-Duplikate; `None` bei Beinahe-Treffer → Feld bleibt unbesetzt, konsistent für alle drei Felder). Gated über `paperless_autocreate_document_type` / `paperless_autocreate_tags` (Default an). Die volle Taxonomie wird pro Batch **einmal** geladen (nicht 3× pro Dokument — die Paperless-MCP ist ~60/min rate-limitiert).

### Re-OCR (lokaler Stack mit Paperless-Fallback)

Die „Re-OCR"-Aktion läuft **nicht** mehr blind über Paperless' eigene OCR (die mit denselben Einstellungen scheitern würde). Stattdessen pro Dokument: Original-Bytes per MCP `download_document` laden (`truncate=False` — sonst wird das Base64 am LLM-Response-Limit abgeschnitten), lokal mit erzwungener Ganzseiten-OCR (Renfields Docling/EasyOCR-Stack inkl. Garbled-Layer-Recovery) neu erkennen, das Ergebnis bewerten und **nur bei striktem Qualitätsgewinn** den sauberen Text via `update_document(content=…)` zurück nach Paperless schreiben. Schlägt die lokale OCR fehl oder ist sie nicht besser, greift der Fallback auf Paperless' natives `reprocess`. Hinweis: Es wird nur der durchsuchbare Content aktualisiert, das archivierte PDF wird nicht neu erzeugt.

**VLM-Re-OCR-Fallback (`ocr_vlm_fallback_enabled`, dark/opt-in).** Bei rotierten/schlechten Scans produziert *jede* Zeichen-OCR (Tesseract wie EasyOCR) Müll — dieselben schlechten Pixel. `DocumentProcessor._vlm_ocr_fallback` rendert dann die Seite(n) (pypdfium2 → PNG) und lässt das **Vision-Modell** (`OLLAMA_VISION_MODEL`, z.B. `qwen3-vl`) den Text transkribieren — robust gegen Rotation/Rauschen. **Trigger + Akzeptanz per zwei Signalen**, weil OCR-Garble in zwei Stilen kommt: (1) **Interpunktions-Garble** (`Bez:-ihl`), den der billige `score_ocr_quality` fängt (≤ `ocr_vlm_fallback_score_threshold`), und (2) **aussprechbare Pseudo-Wörter** (`ZOGEOLONIGGY`), die Zeichenstatistik **nicht** von echten Wörtern trennen kann — das braucht Sprachverständnis, also fängt ein schneller **LM-Gibberish-Check** (`is_ocr_gibberish`, Intent-Modell, `ocr_vlm_gibberish_gate_enabled`) diesen Stil. Der LM-Check ist zugleich der Akzeptanztest: der VLM-Text wird genutzt, wenn das OCR Gibberish war und der VLM-Text lesbar ist (ihre groben 1-5-Scores können bei Stil 2 beide auf 5 liegen). **Live validiert:** Intent-Modell klassifiziert Tesseract-Müll → GIBBERISH, sauberen Beleg → READABLE; VLM transkribiert den Beleg sauber. **Bewusst Fallback, nicht primär:** das VLM ist ~2× langsamer und GPU-gebunden, verliert Docling-**Struktur** (Tabellen/positionierte Tokens fürs Schicht-A), und kann **halluzinieren** (erfundene Beträge/Nummern — in einem Dokumentenarchiv gefährlicher als ein treues Garble). Tesseract bleibt für die saubere Mehrheit schneller, treuer, strukturiert. Der Fallback sitzt in der **geteilten** OCR-Schicht (`extract_text_only` UND `process_document`), also profitieren **Audit-Re-OCR, Ingest UND KB-Reindex** — eine OCR-Reparatur im Audit erreicht so auch die KB (statt vom Tesseract-Reindex neu ver-garbled zu werden). Bounded auf `ocr_vlm_fallback_max_pages`; best-effort (jeder Fehler → OCR-Text bleibt).

**Re-OCR → KB-Propagation (`paperless_audit_reindex_on_reocr`, Default an).** Das Audit schreibt den bereinigten Text nur ins **Paperless-Archiv** — renfields Retrieval läuft aber über die eigenen `document_chunks` (beim Ingest gebaut). Ohne Propagation behielte die KB also das alte, verrauschte OCR. Nach einem erfolgreichen Re-OCR (`improved`) stößt das Audit daher einen renfield-**Reindex** für dasselbe Dokument an (`paperless_document_id → documents.id`, `force_ocr=True`, gleicher `user_reindex`-Worker-Pfad wie `POST /api/knowledge/documents/{id}/reindex`), sodass Chunks/Schicht-A-Fakten/KG den OCR-Gewinn übernehmen. Best-effort (bricht das Re-OCR nie), mit In-flight-Dedup; **übersprungen für Paperless-only-Dokumente** (kein renfield-Gegenstück). **Nur Re-OCR propagiert** — reine Metadaten-Fixes (Korrespondent/Typ/Tags) berühren die KB nicht, da renfields Retrieval sie weder speichert noch nutzt.

**Re-OCR → Metadaten-Neuableitung (`paperless_audit_rederive_metadata_after_reocr`, Default an).** Die Paperless-Metadaten (Titel/Korrespondent/Typ/Tags/Datum) werden beim Ur-Ingest aus dem OCR-Text abgeleitet — war der garbled, sind sie falsch (z.B. ein Parkbeleg als „Krankenhausabrechnung/Klinikum"). Ein bloßes Re-OCR reparierte den Text, ließ aber die Metadaten (und die alten Audit-Vorschläge) aus dem Müll. Nach einem erfolgreichen Re-OCR (`improved`) leitet das Audit die Metadaten daher aus dem **jetzt guten Text neu ab** (`_analyze_document` auf den frischen Content) und **wendet sie im selben Schritt an** (`_apply_fix` mit resolve-or-create für fehlende Taxonomie) — der Nutzer muss keinen separaten Audit-Lauf starten. Best-effort (bricht das Re-OCR nie); auto-apply (Metadaten sind leicht korrigierbar, und der Vorzustand war falsch). Für **neue** Dokumente ist das gar nicht nötig — der VLM-Fallback beim Ingest liefert schon guten Text → korrekte Metadaten von Anfang an; die Neuableitung schließt nur die Lücke bestehender Docs.

### Admin UI (`/admin/paperless-audit`)

Tabs: Audit Control (Start/Status), Review Queue (Sortierung, Suche, Freigabe), OCR Issues (Re-OCR-Angebot), **Niedrige OCR-Qualität** (siehe unten), Vollständigkeit, Duplikate, Ansprechpartner, Statistics.

### Editierbare Review-Queue (selektive + manuelle Freigabe)

Die Review-Queue ist nicht mehr „alles-oder-nichts": jeder vorgeschlagene Wert wird **im Jira-Stil inline bearbeitet** (Klartext → Klick macht das Feld zum Editor; Enter/Verlassen speichert, Esc verwirft), und pro geändertem Feld gibt es eine **Übernehmen-Checkbox** — so lässt sich pro Dokument auswählen, *welche* der vorgeschlagenen Änderungen angewendet werden, und ein Vorschlag vor der Freigabe von Hand korrigieren.

**Lookup-Felder + Kalender:** Ansprechpartner, Typ und Ablage sind **Lookup-Felder**, vorgefüllt mit den vorhandenen Paperless-Werten (über den Endpunkt `GET /api/admin/paperless-audit/taxonomy`) — eine **Combobox** (Headless UI, `components/paperless/reviewControls.tsx`) filtert die Bestandswerte und bietet, wo erstellbar, eine „«X» anlegen"-Zeile (Ansprechpartner immer, Typ/Tags per `paperless_autocreate_*`-Flag; Ablage nur aus Bestand, da Storage-Paths nicht auto-angelegt werden). Tags nutzen dieselbe Combobox im Chip-Editor; das **Datum** öffnet ein **Kalender-Popover** (`react-day-picker`, Monatsraster, Intl-Locale, Tastatur-Navigation, „Zurücksetzen"). Benutzerdefinierte Felder: Key/Value-Editor in einer pro-Zeile ausklappbaren Lade. Die Overlays rendern per **Portal** an `<body>` (Headless UI floating-ui bzw. eigenes fix-positioniertes Popover) — so entkommen sie dem `overflow-x-auto`-Clipping der Tabelle. Standard-Komponenten statt handgebaut (bewusste Wartungs-/a11y-Entscheidung).

Das Review-Overlay wird **persistiert** — zwei additive JSON-Spalten auf `paperless_audit_results` (Migration `pc20260726_audit_review`): `user_overrides` (`{Feld: editierter Wert}`, getrennt von `suggested_*`, damit der LLM-Vorschlag als Provenance erhalten bleibt) und `field_selection` (anzuwendende Felder; beide `NULL` = Legacy „alle vorgeschlagenen Änderungen", byte-identisch). Neuer Endpunkt `PATCH /api/admin/paperless-audit/results/{id}` (ADMIN, validiert Feldnamen/Typen → 400/404); `POST /apply` bleibt unverändert und liest das persistierte Overlay von der Zeile. In `_apply_fix` ist der effektive Wert pro Feld = Override ?? Vorschlag, angewendet nur wenn ausgewählt **und** ≠ aktuell (ein manueller Override gilt immer, ein Feld auf den aktuellen Wert editiert ist ein No-op). Die Frontend-Freigabe (einzeln + Sammel) flusht ausstehende Feld-Speicherungen, bevor angewendet wird.

### Niedrige OCR-Qualität (Triage statt SQL)

Eigener Tab, der Dokumente sichtbar macht, deren **Ingest** an der Qualität gescheitert ist — damit der Operator sie in der UI statt per SQL bearbeitet. Ein Dokument gilt als „niedrige OCR-Qualität", wenn **eines** zutrifft:

1. Die renfield-interne `documents`-Zeile hat `status='failed'` mit `error_message LIKE 'ocr_quality%'` (die Ingest-Pipeline hat es an der Qualitätsschwelle abgewiesen), **oder**
2. der **letzte** `document_processing_history`-Eintrag hat ≥ 30 % der Chunks an der Qualitätsschwelle verworfen (`chunks_dropped_low_quality / (produced + dropped) ≥ 0.30`).

Das Signal lebt am renfield-internen `Document`, der Audit-Datensatz am Paperless-externen `paperless_doc_id` — verknüpft über `Document.paperless_document_id`. Paperless-only-Dokumente (nie in die KB ingestet) tragen kein Badge. Jede betroffene Zeile zeigt ein Badge (`X % verworfen` bzw. `OCR fehlgeschlagen`) und zwei Aktionen: **Erneut OCR** (derselbe lokale Re-OCR-Pfad wie der OCR-Tab) und **Ignorieren** — letzteres setzt `documents.quality_ignored` (Migration `pc20260618_doc_quality_ignored`), wodurch das Dokument vom periodischen Cleanup-Lauf (`bin/purge_low_quality_chunks.py`) übersprungen und aus dem Tab herausgefiltert wird; **Wieder berücksichtigen** hebt das auf. Das Badge erscheint zusätzlich inline im OCR-Tab. Endpunkt: `POST /api/admin/paperless-audit/quality-ignore` (ADMIN-gated, wie alle Audit-Routen); der `low_quality_only`-Filter beschränkt die Ergebnisliste serverseitig.

### Duplikate selbst finden und löschen (`internal.paperless_dedupe`)

Renfield räumt doppelte Paperless-Dokumente selbst auf — per Chat, nicht per API-Skript („finde und lösche die Duplikate in Paperless", „räum die doppelten Dokumente auf", „gibt es Dubletten?"). Das Agent-Tool `internal.paperless_dedupe` (`services/paperless_dedupe_tool.py`, `documents`-Rolle):

1. **Kandidaten** aus günstigen Such-Metadaten gruppieren (Korrespondent · Dokumenttyp · Erstelldatum · Titel, `ordering=-created` — Re-Upload-Bursts sind die neuesten Zeilen; Sweep bis `SWEEP_CAP=500`, größerer Korpus wird als teilweise geprüft gemeldet).
2. **Voller OCR-Text-Vergleich** je Kandidatengruppe (`get_document` mit `truncate=False`) — nur **byte-identische** Kopien gelten als Duplikat; ähnliche, aber nicht identische Dokumente werden nur gemeldet, **nie** gelöscht.
3. **Löschen**: das älteste Dokument (kleinste Paperless-ID) bleibt, jede weitere identische Kopie geht über `mcp.paperless.delete_document` in den **wiederherstellbaren Papierkorb** (Paperless-ngx 2.x — eine übereifrige Bereinigung ist rückgängig machbar).

`dry_run=true` meldet die Gruppen, ohne zu löschen. **Fail-closed**: bei aktiver Auth erfordert das Löschen einen authentifizierten **ADMIN**; ein unidentifizierter Turn (`user_permissions=None` — Geräte-/Satelliten-Token oder unerkannte Sprachstimme) wird abgelehnt (Bulk-Archiv-Löschung hat größere Tragweite als die reversiblen Wartungs-Tools). Auth aus (Einzel-Haushalt) überspringt den Gate. Ergänzt den admin-seitigen **Duplikate**-Tab (nur Anzeige) um die aktive Bereinigung.

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

- `GET /api/wissensbasis/graph` — Korpus-Ansicht: Connected-Component-Cluster mit Hub-Entities; jeder Hub trägt `circle_tier`, jedes Cluster seine echten Hub↔Hub-Relationen (`hub_edges`)
- `GET /api/wissensbasis/focus?entity_id=` — Nachbarschaft einer Entity (hop1 + hop2, inkl. `circle_tier` + echter `edges`-Liste)
- `GET /api/wissensbasis/search?q=` — Namens-Suche für das Such-Overlay

Alle drei sind `KG_VIEW`-gated und circle-gefiltert (`services/kg_graph_service.py`): eine Kante erscheint nur, wenn beide Endpunkte für den Anfragenden sichtbar sind. Die Reva-eigenen Endpunkte (`/trace`, `/me/mix`) sind in der Standalone-Renfield-Variante absichtlich nicht implementiert.

**Szene (volumetrisch, seit 2026-07):** Cluster-Zentren, Hubs und beide Focus-Schalen werden per Fibonacci-Verteilung über Kugeln platziert (deterministisch, stabil über Reloads) — die frühere Darstellung kollabierte auf eine flache XZ-„Ekliptik". Knotenfarbe = Circle-Tier (DESIGN.md-Tier-Token, Tier zusätzlich als Text im Label — Farbe nie alleiniges Signal), Knotengröße = Mention-/Importance-Anteil, Cluster-Hüllen alternieren nur Markenfarben (Crimson/Türkis/Cream). Focus-Modus zeichnet die echten Relationskanten des Backends; Hover hebt die inzidenten Kanten in Akzent-Türkis hervor. Kamera framet die Bounding-Sphere; langsamer Auto-Orbit liefert die Tiefen-Parallaxe, stoppt bei der ersten Interaktion und entfällt unter `prefers-reduced-motion`. Ferne Sekundär-Labels werden distanz-gecullt.

## Kiosk (Wand-Display)

Radiale Live-Konstellation des laufenden Systems auf dem Wandtablet/-display — Kern (aktive Agenten-Rolle + Sprachzustand), Ring der Agenten-Rollen, Ring der MCP-Tools (gesund/eingeschränkt/ausgefallen), Ring der Räume/Satelliten (online + Belegung), Föderations-Peers als äußerer Bogen, plus der **Aktives-Subsystem-Puls** (welcher Renfield-Teil diesen Turn genutzt wurde). Rein lesend, inhaltsfrei, Admin-gated.

> **Hinweis (2026-07):** Das frühere Admin-Board `/admin/command-center` wurde **stillgelegt** — der Kiosk ist die verbleibende Oberfläche. Historie + „warum kein Polling" siehe `docs/design/command-center.md` (SUPERSEDED-Banner) und `tasks/kiosk-active-subsystem-plan.md`.

### Fullscreen-Kiosk (`/kiosk`)

Kinoreife Vollbild-Variante fürs Wandtablet/-display (Admin-gated, außerhalb des App-Layouts). Bricht DESIGN.md **bewusst** (Glow/Bloom, JARVIS-Ästhetik) — diese Optik lebt NUR im Kiosk, nie auf dem zurückhaltenden Admin-Board.

- **Datenpfad = Event-Push, kein Polling:** das Live-Modell (Satelliten-Roster + Zustand, Präsenz, Tool-Health, Wetter, Now-Playing, aktives Subsystem) kommt über den Admin-gated `/ws/kiosk`-Hub — EIN `snapshot` beim Connect, danach inhaltsfreie Deltas (`satellite_state`, `satellite_online`/`satellite_offline`, `presence_changed`, `now_playing_changed`, `tool_health_changed`, `internal_health_changed`, `weather_updated`, `turn_activity`). Ersetzt die frühere react-query-Poll-Kette; kein Browser-Timer trifft mehr unsere eigene REST-API. **Liveness ist backend-autoritativ:** ein Satellit im Roster IST online (das Backend entfernt einen abgestürzten per `satellite_offline` bei Heartbeat-Timeout) — keine Wanduhr-Verfallslogik auf eingefrorenen Snapshot-Werten; ein Reconnect re-verankert alles aus einem frischen Snapshot. (Föderations-Peers behalten einen Wanduhr-Frische-Backstop, solange es noch kein `peer_status_changed`-Delta gibt.)
- **Aktives Subsystem:** wenn ein Turn ein Tool nutzt, leuchtet der zugehörige MCP-/Renfield-Knoten auf (inhaltsfrei: `{subsystem_id, at}` aus `agent_tool_results`, nie Äußerung/Entität/Nutzer). `mcp.<server>.*` → der Server-Knoten; `internal.*`-Tools mappen über eine Allowlist auf ein Subsystem (Wissen / Präsenz / Home Assistant / Wetter / Medien). Für die drei rein-internen Subsysteme ohne MCP-Server (Wissen / Präsenz / Medien) rendert der Kiosk **synthetische, gesundheits­lose Puls-Pseudoknoten** (aus der Tool-Health-Telemetrie ausgenommen).
- **Knoten-Health = Konnektivität UND Funktionalität:** ein Tool-/MCP-Knoten im Werkzeug-Ring ist `healthy` (verbunden + funktionsfähig), `degraded` (erreichbar, aber beeinträchtigt) oder `down` (getrennt). **Degraded** greift, wenn ein Server zwar verbunden ist, aber (a) ein an ihn gebundenes Startup-Plugin nicht geladen werden konnte (`PLUGIN_MCP_BINDINGS`, s. `docs/ENVIRONMENT_VARIABLES.md`) — der Auslöser war ein still fehlgeschlagener Adapter, während der MCP-Transport grün blieb — oder (b) er keine Tools exponiert (Föderations-Peers ausgenommen), sowie (c) die bestehende niedrige Tool-Call-Erfolgsrate. Das Backend synthetisiert `health` in `get_status()` (geteilt mit dem `tool_health_changed`-Delta, das `health` + einen stabilen `impaired_code` mitträgt); der Kiosk **lokalisiert** den Code (`kiosk.impaired.*`, nie ein roher Backend-String) als `<title>`-Tooltip. So kann ein grün-erreichbarer, aber funktional toter Knoten nicht mehr als gesund durchgehen.
- **Interne Subsysteme (Wissen/Präsenz/Medien) mit echtem Status:** die drei internen Pseudo-Knoten (kein eigener MCP-Server) tragen jetzt ebenfalls ein echtes Health-Urteil aus Live-Zustand (`compute_internal_subsystem_health()` → Snapshot + `internal_health_changed`-Delta), statt dauerhaft grau zu sein: **Präsenz** `degraded`, wenn ein eingeschriebener Satellit verbunden, aber nicht authentifiziert ist (kein IRK-Push → stille Präsenz-Blindheit) oder kein Satellit online ist; **Wissen** `degraded` bei totem Ingest-Worker oder hohem Live-Backlog (XPENDING, nicht die monoton wachsende Stream-Länge); **Medien**/**Präsenz** `off` (gedämpft, NICHT rot) wenn per Konfiguration deaktiviert — `off` ist ein eigener Zustand, unterscheidbar von `down` (roter Ausfall) und `unknown` (noch kein Urteil).
- **Kern:** durchscheinender Licht-Globus (Meridian-Filamente, heller Rand, kein Status-Text) statt massiver Scheibe.
- **Status = physische Satelliten-LEDs** (`hardware/led.py`): idle=blau, listening=grün, processing=gelb, speaking=cyan, error=rot, offline=dunkel-gestrichelt — angewandt auf Kern, Raum-Punkte (pro Raum der signifikanteste Live-Zustand seiner Online-Satelliten) und Legende. **Das gesamte Ambiente-Feld** (Halo/Nebel/Basis-Verlauf/Sweep) folgt der Kernfarbe, damit der Hintergrund den Zustand mitträgt (blau im Ruhezustand, grün beim Zuhören …).
- **Ambiente-Kacheln** (blenden sich bei Nichtverfügbarkeit aus; Daten kommen jetzt über den `/ws/kiosk`-Push, nicht mehr per Poll): **Wetter** (Wetter-MCP für `KIOSK_WEATHER_LOCATION`, ~10-min-Cache, ins Snapshot/`weather_updated`-Delta eingespeist) und **Now-Playing** (aus `MediaFollowService.active_sessions()`, ein Eintrag pro Raum, inhaltsminimal — keine Nutzer-IDs, `now_playing_changed`-Delta).
- **Orientierungs-agnostisch:** `preserveAspectRatio="meet"` (Konstellation nie beschnitten) + Basis-Verlauf als CSS-Gradient am Wrapper — Landscape-Wand-TVs UND Portrait-Raumtablets rendern korrekt. `prefers-reduced-motion` respektiert; inhaltsfrei by design.
- Öffnbar über den „Kiosk"-Button im Admin-Board-Header.

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
- **Trusted Proxies**: CIDR-basiert (`TRUSTED_PROXIES`) — gesetzt: spoof-sicherer Rechts-nach-Links-XFF-Durchlauf; leer: legacy `X-Forwarded-For[0]` (rückwärtskompatibel, spoofbar)
- **Rate-Limit-Storage**: per-Pod (`memory://`) oder per-Cluster (`API_RATE_LIMIT_STORAGE_URI=${REDIS_URL}`)
- **Account Lockout**: Pro-Username-Sperre nach Fehl-Logins (`LOGIN_LOCKOUT_ENABLED`, Redis, fail-open)
- **Auth-Observability**: `renfield_login_failure_total` / `renfield_authz_denied_total` + strukturierte Logs auf 401/403
- **Forced Password Rotation**: `must_change_password` serverseitig erzwungen (Allowlist bis zur Rotation)
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
