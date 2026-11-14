# Rückstands-Inventar

**Erhebung: 2026-11-09** (vorherige: 2026-09-18). Vollständige Auflistung aller offenen Posten aus beiden
Quellen — GitHub-Issues und Dokumentation. Diese Datei ist das **Nachschlagewerk**;
die *Rangfolge* steht in `TODOS.md` → „Priorisierte Gesamtsicht" und nennt bewusst
nur die Spitzenposten je Stufe. Wer wissen will, was insgesamt offen ist, liest hier.

Warum es beides gibt: Die Rangfolge beantwortet „was zuerst", das Inventar
beantwortet „was überhaupt". Eine Rangfolge für eine Aussage über den Gesamtumfang
zu halten, führt in die Irre.

## Umfang auf einen Blick

| Quelle | 2026-11-09 | 2026-09-18 |
|---|---|---|
| Offene GitHub-Issues | **69** — davon **35 auf `main` erledigt**, nur nicht geschlossen; 21 seit über 90 Tagen unberührt | 41 |
| `TODOS.md` — offene Einträge | **71** (P1 2 · P2 57 · P3 12) | 88 |
| Design-Dokumente mit offenem Status | **30 von 42** | 24 von 35 |
| `CLAUDE.md` / `.claude/rules/` — zurückgestellt/dunkel | **7 von 13** Stellen gelten weiter | 12 |
| Nie eingeschaltete Funktionsschalter | **28** (von 101 `bool = False`) | 33 |
| `TECHNICAL_DEBT.md` (Backend + Satellit) | **9 offene Posten** (5 + 4) | 9 |
| `tasks/` — echte offene Kästchen | **343** (+ 23 fremdes Repo, 28 überholt) | 325 |

## Grenzen dieser Erhebung — ehrlich benannt

- **Stichtag.** Erhoben am 2026-11-09 gegen `main` bei `cbe0da8f` und die LIVE-ConfigMaps beider Instanzen.
- **Zeilennummern.** Alle `datei:zeile`-Angaben gelten für diesen Stand. `TODOS.md` wurde seit der letzten
  Erhebung umgebaut; die alten Nummern sind nicht vergleichbar.
- **Uncommittetes ist nicht gezählt.** Die Arbeitskopie von `TODOS.md` trägt drei zusätzliche P3-Einträge zur
  Chat-Bildanalyse (vermerkt in Abschnitt 1 und Teil C).
- **Vertraulichkeit.** Inhalte aus `docs/private/` und den unversionierten `tasks/`-Dateien werden nicht
  wiedergegeben, nur Dateinamen und Zahlen.
- **„Erledigt" ist am Commit oder am Code belegt, nie am Titel.** Wo das nicht ging, steht „nicht prüfbar".

---

# Teil A — Dokumentation

Reine Erfassung, keine Priorisierung. Fundstellen als `datei:zeile`.

Ausgewertete Quellen: `TODOS.md` (eingecheckter Stand), `docs/design/*.md` (42 Dokumente), `.claude/rules/*.md`,
`src/backend/utils/config.py` ↔ LIVE-ConfigMaps beider Instanzen (gegen `k8s/configmap.yaml` und
`../x-ren/k8s/configmap.yaml` abgeglichen), `docs/TECHNICAL_DEBT.md`, `src/satellite/TECHNICAL_DEBT.md`, übrige `docs/*.md`.

---

## 1. TODOS.md — 71 offene Einträge

> Erhoben 2026-11-09 gegen den **eingecheckten** Stand (`git show HEAD:TODOS.md`, HEAD `cbe0da8f`, 782 Zeilen); alle `TODOS.md:<n>` beziehen sich auf diese Fassung. Die Zeilennummern der Erhebung vom 2026-09-18 gelten nicht mehr (die Datei wurde umgebaut: „Priorisierte Gesamtsicht" kam hinzu, die Tier-Abschnitte sind gewandert). Die Nummern #1–#71 sind neu vergeben und **nicht** mit denen der alten Erhebung vergleichbar.
>
> **Nicht gezählt:** die „Priorisierte Gesamtsicht" (`TODOS.md:30-92`) — sie ordnet nur. Einzelne Posten stehen allerdings NUR dort, z. B. Haushalt auth-on ohne HttpOnly-Sitzung `:46`, KV-Cache `cuda.local` `:48`, ungepinntes `ollama:latest` `:71`.
>
> **Uncommitted, nicht gezählt:** Die Arbeitskopie fügt in P3 drei Einträge „Chat-Bildanalyse" hinzu (CEO-Review 2026-11-08: D8 Vorschaubild, D4 Folgevorschläge, D5 `describe()`-Naht).

### P0 — Active / blocking: 0 Einträge
`TODOS.md:98` — „(no active blockers — all prior P0 items resolved and merged)". Ausdrücklich leer.

### P1 — Next substantive batch (2 offen, 3 durchgestrichen)

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 1 | #10 Presence / Media-Follow Raumwechsel-Latenz | RSSI-EWMA-Filter live zur Feldvalidierung; Watch-List F1–F5 | „LANDING ACTIVE for real-world validation" — widerspricht `:328` („LARGELY RESOLVED 2026-07-09/10", dort nur noch Grenzmessung (a) offen) | `TODOS.md:107` |
| 2 | #6 Provisioning config drift | Rest: Gästezimmer-host_var-Enrollment-Token (offline), Inventory → mDNS + DHCP-Reservierungen | Ops, kein PR | `TODOS.md:108` |

Durchgestrichen: Broadcast (`:104`), #12 KB-Maintenance (`:106`), Security-Audit-Dach (`:110`, zwei Durchgänge, Reste in #1116).

Im Kopf als „bewusst nicht implementiert" weiter vermerkt (Tracker, kein Arbeitspaket): #1116, #342 (Stand 2026-09-20: W3 + W4.1 offen), #876, #875, #877; #1121 dort als „closed 2026-09-04" markiert — `TODOS.md:14`.

### P2 — Scheduled follow-ups (57 offen, davon 2 laut Code erledigt und 3 teilweise)

**🎙️ Browser-Voice auf xidra** (`docs/design/browser-voice-auth-on-instances.md`)

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 3 | Sprechererkennung-Opt-in auf xidra (D4) | 4 Speaker-Flags auf xidra `false`; DSGVO-Assessment zuerst (ECAPA = Art. 9) | „LATER — not now" | `TODOS.md:120` |
| 4 | Voice-Server-Härtung (D7) | (a) Token im Access-Log redigieren, (b) Per-Client-Origins vom Voice-Server erzwungen | **(a) laut Code erledigt, TODOS nicht nachgezogen:** `e3b4664d` (#1452, `voice_server/log_safety.py`). **(b) offen:** keine Origin-Prüfung in `voice-server/voice_server/` | `TODOS.md:121` |

**🧬 KB-Near-Duplicate (#1170)**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 5 | Text-Signal-Präzision | Blocking-Key vor dem Cosinus oder schärfere Doc-Signatur | nur wenn 0.995 zu verrauscht | `TODOS.md:126` |
| 6 | `max_docs=2`-Kompromiss bei Dreifachkopien | ein Dokument in genau 3 Kopien wird nicht gefunden | bei echter Dreifachkopie | `TODOS.md:127` |
| 7 | `content_embedding`-Backfill auf neuen Instanzen | Backfill-Skript + Schwellen-Sweep vor dem Flag-Flip | beim Aktivieren auf weiterer Instanz | `TODOS.md:128` |
| 8 | PR2 — föderierte Dokumentsuche | über Paperless + (xidra) Simba | zurückgestellt aus #1156 | `TODOS.md:129` |

**🎯 Meeting-Transkriptionsqualität**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 9 | Overlap-Deletions senken | Per-Sprecher-Audio vom XVF3800 / Per-Turn-ASR / Whisper-Tuning | — | `TODOS.md:140` |
| 10 | cpWER als Gate halten | während Track-A-Diarisierungsänderungen | mit Track A | `TODOS.md:141` |
| 11 | Deutsche Meeting-Qualität UNGEMESSEN | deutsches Referenzset + `de`-Baseline | beide Instanzen null Meetings (`:134-139`); nächster Schritt ein echtes Meeting | `TODOS.md:142` |

**🗂️ Meetings-UX**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 12 | Track-D-Folgearbeit | Auto-Match / stärkere Confirm-Erinnerungen | eigene Tracks (Auto-Match laut `docs/design/meeting-kg-and-speaker-identity.md:3` „not planned") | `TODOS.md:150` |

**🔐 Security-Audit Login**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 13 | ⚠️ MERGE/DEPLOY-GATE — Reva-(OIDC-)Login verifizieren | (a) Reva-OIDC auf xidra an?, (b) Browser-E2E, (c) Reva-Repo prüfen | Operator-Aufgabe; M1 im SSO-Cutover aufgegangen, M2-Frontend, Low/Info und M5 in derselben Zeile erledigt | `TODOS.md:154` |
| 14 | Ungegatetes Agenten-Schreiben in Paperless | `update_document` ohne Permission-Gate | **laut Code erledigt, TODOS nicht nachgezogen:** `update_document: mcp.paperless.write` (`config/mcp_servers.yaml:429`, `ebd5e16a`, #1123). Offen nur der Betriebsschritt, Rollen auf `mcp.paperless.read` zu verengen | `TODOS.md:160` |

**SSO-Token-Handoff — Cutover**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 15 | Cutover in 3 Schritten | (1) Emitter auf `?code=`, (2) `SSO_HANDOFF_ENABLED`, (3) Fragment-Handler löschen | **teilweise:** `/api/auth/sso/start` + PKCE-Start gebaut, dark (`613b9bc5`, #1501); Schritte 1–3 offen. Handler heute in `utils/ssoFragment.ts` | `TODOS.md:164` |
| 16 | BLOCKIERENDE offene Frage §10.1 | Wo läuft der Reva-OIDC-Callback relativ zu Renfields Redis? | Operator-Entscheid | `TODOS.md:165` |

**JWT/Cookie-Restschuld** (`TODOS.md:169` „Remaining debt")

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 17 | `/api/ws/token`-Faucet + Bearer-Interceptor abschaffen | — | sobald Reva cookie-nativ | `TODOS.md:169` |
| 18 | Browser-Voice auf einer auth-on-Instanz freischalten | dann Live-E2E für `scope:voice` | xidra `FEATURE_VOICE: "false"` (`../x-ren/k8s/configmap.yaml:332`) | `TODOS.md:169` |

**Föderation**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 19 | Personenbezogene Föderation (F-ID-2-Handschlag + Aktivierung) | „open: F-ID-2 handshake + activation" | **laut Code erledigt, TODOS nicht nachgezogen:** `eecd3009` (#1419); `FEDERATION_IDENTITY_LINKS_ENABLED: "true"` beidseitig (`k8s/configmap.yaml:76`, `../x-ren/k8s/configmap.yaml:94`) | `TODOS.md:194-199` |

**Chat-UI-Modernisierung — Rest**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 20 | (10) Gen-UI-Restposten | Positionssteuerung Cover/Jalousien; reichere Widgets „gated on Lane B" | Lane B ist „not planned" (`.claude/rules/chat-ui.md:17`) — dieser Teil gegenstandslos | `TODOS.md:235` |
| 21 | (9) shared-private | braucht haushaltsgeteilte Unterhaltungen | T3 | `TODOS.md:236` |
| 22 | Artefakt-Persistenz „last-frame-wins" | streamender Produzent braucht serverseitiges Append | — | `TODOS.md:237` |
| 23 | PRÄMISSEN-VORBEHALT | Anteil Web-`/chat` vs. Sprache nie gemessen | — | `TODOS.md:239` |

**Einzelthemen P2**

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 24 | BT-Scan: deterministische Geräteanzahl je Raum | Backend rechnet die Zählungen vor | kosmetisch | `TODOS.md:241-257` |
| 25 | Steuererklärungs-Sammler — schlanke P1 | T1–T5, `STEUER_ASSISTANT_ENABLED` | „[P1-ready]"; im Code weder `tax_categories` noch das Flag | `TODOS.md:273` |
| 26 | [P2] §35a Zahlungsjahr (Zuflussprinzip) | — | — | `TODOS.md:280` |
| 27 | [P2] Kategorie-Override bei Re-Extraktion erhalten | Sticky-Bit nach `tier_overridden` | — | `TODOS.md:281` |
| 28 | [P2] Deterministisches `/api/steuer/{year}` + Steuer-Linse | — | — | `TODOS.md:282` |
| 29 | [P2] Abzugsbetrag je Kategorie extrahieren | — | — | `TODOS.md:283` |
| 30 | [P2] Gespeicherter Situations-Fragebogen | — | — | `TODOS.md:284` |
| 31 | [P3] CSV/PDF-Dossier-Export + ELSTER/ERiC | — | — | `TODOS.md:285` |
| 32 | Fristen-Kalender-Sync in Produktion einschalten | Calendar-MCP im Pod nicht verdrahtet; Checkliste `docs/OBLIGATION_CALENDAR_SYNC.md` | Operator-Credentials; Flag in keiner ConfigMap | `TODOS.md:287-289` |
| 33 | Fristen-Kalender: Duplikat-Fenster ohne Idempotenz-Key; terminierte statt ganztägige Termine | „P2 follow-up" | — | `TODOS.md:399` |
| 34 | Paperless PR4: Mehrnutzer-Zuordnung in ui_sweep-Zeilen | MCP-`owner` + Benutzer-Mapping | — | `TODOS.md:294` |
| 35 | Paperless PR 4b: `superseded`-Filter | Spalte existiert | wenn ui_sweep-Rauschen real auftritt | `TODOS.md:297-300` |
| 36 | Satellit: Rauschunterdrückung im Backend | Hoch; Alternative XVF3800-AEC | — | `TODOS.md:304` |
| 37 | Satellit: Echo Cancellation | Mittel | — | `TODOS.md:305` |
| 38 | Satellit: 4-Mikrofon-Beamforming | Niedrig | — | `TODOS.md:306` |
| 39 | Presence-Nacharbeit (a) | Grenze Esszimmer↔Arbeitszimmer neu messen | (b) gemergt #942/#943 | `TODOS.md:328` |
| 40 | [P3] 55" Interactive Signage Flip spielt kein TTS | 404 in `_confirm_playback_started` | separat untersuchen | `TODOS.md:341` |
| 41 | [P3] dlna-mcp Default-MIME `audio/flac` | falsch für Nicht-FLAC | eigenes Repo | `TODOS.md:343` |
| 42 | [Tech debt] TTS an Renderer bewusst über http | Begründung `ADVERTISE_SCHEME` in `docs/ENVIRONMENT_VARIABLES.md` | akzeptiertes Risiko | `TODOS.md:344` |
| 43 | Browser-TTS-Barge-in schneidet nach Satz 1 ab | Richtungen (a)–(d) | — | `TODOS.md:346-347` |
| 44 | Expliziter „mobile/never-route"-Schalter je Gerät | — | sobald ein beweglich registriertes Gerät auftaucht | `TODOS.md:349-350` |
| 45 | Tool-Health-Charts | Trendlinien/Heatmap | ≥30 Tage Daten; #1299 brachte nur den System-Eimer | `TODOS.md:361` |
| 46 | Diff-Ansicht Trajektorien v1/v2 | `/admin/memory-v2-shadow` | gated auf `memory_extraction_v2_authoritative` | `TODOS.md:362` |
| 47 | Bulk-Approve in der Skills-Inbox | — | ≥2 Wochen Burn-in | `TODOS.md:363` |
| 48 | Partielle Indexe für `procedural_skills.status` | `docs/TECHNICAL_DEBT.md` #12 | Tabelle muss wachsen | `TODOS.md:365` |
| 49 | Wurzelbefund: Self-Learning hungert, Sprecher→User-Identität ungenutzt | operativ | Messung 2026-07-05 | `TODOS.md:367` |
| 50 | Speaker Phase 0.5 | `audio_duration_s` auf den Chat-WS-Frame | Backend-Feld existiert (`models/websocket_messages.py:232`); Frontend/voice-server reichen es nicht weiter | `TODOS.md:372` |
| 51 | Speaker operativ | 3 Mitglieder einlernen, `purge_unknown_speakers.py --commit` | aus dem Repo nicht prüfbar | `TODOS.md:375` |
| 52 | Speaker Phase 3 umlegen | einlernen ≥2, kalibrieren; kein Code offen | Flag steht bereits `true` (`k8s/configmap.yaml:621`) | `TODOS.md:378` |
| 53 | Speaker Phase 4 (optional) | Far-Field, AS-Norm, Upstream-Capture | — | `TODOS.md:379` |
| 54 | `/brain` zeigt Fakt + Quell-Chunk doppelt | akzeptieren + beobachten | wenn verrauscht | `TODOS.md:402` |
| 55 | pg_trgm-Index für Identifier-Suche | — | messbare Latenz | `TODOS.md:403` |
| 56 | Billiger Vorfilter vor dem LLM-Fristen-Pass | — | messbare Ingest-Latenz | `TODOS.md:404` |
| 57 | Echter Cross-Encoder-Reranker | `RAG_RERANK_ENABLED: "false"` beidseitig | wenn die Top-K-Ordnung schmerzt | `TODOS.md:407` |
| 58 | Output-Provider: Marken-Shims + Discovery-Methoden entfernen | blockiert auf Migration der Medienauflösung im Agenten (§5) | — | `TODOS.md:415-422` |
| 59 | `docs/STRATEGY.md` fertigschreiben | **9 `[FOUNDER FILL-IN]`** | nur vom Gründer | `TODOS.md:454-483` |

### P3 — Conditional / on signal (12 offen)

| # | Eintrag | Inhalt | Trigger | Fundstelle |
|---|---|---|---|---|
| 60 | GUI-Contribution-Model | Registry deklarativer Slots; Vorbedingung Off-Token-Drift | nächstes Feature mit 5+ Shell-Dateien | `TODOS.md:489-490` |
| 61 | Satelliten-Meeting-Aufnahme | Sprachstart → Upload-Pfad | §2 gebaut + Spike XVF3800-Audio diarisierbar | `TODOS.md:512-531` |
| 62 | Shadow-Log `would_have_injected` entfernen | Tabelle existiert noch (`models/database.py:2232`) | Owner prüft die Metrik | `TODOS.md:551-558` |
| 63 | Kaskadenabstimmung im Haushalt für geteilte Skills | — | ≥10 household-Tier-Auto-Skills oder Feedback | `TODOS.md:560-567` |
| 64 | Subsume-Recall-Verlust (Phase 3) | Residuum: zwei Fakten zum selben Subjekt in einem Turn | **Trigger-Lage geändert:** `MEMORY_SUBSUME_TO_KG: "false"` im Haushalt seit dem Cutover (`k8s/configmap.yaml:571`) | `TODOS.md:581-592` |
| 65 | Subsume-Vorbehalt „nur Single-User" | — | **teilweise:** Auflösung je Frager über Entitäts-IDs (`69421cd2`, #1313); Startwächter `config.py:2440` verweigert `true` + `AUTH_ENABLED` weiter; `:594` nicht nachgezogen | `TODOS.md:594` |
| 66 | Dedup von Personen-OCR-Varianten nur über Review | Reconciler-Gate seit #1329 breiter | Duplikatspersonen beobachten | `TODOS.md:616-618` |
| 67 | Paperless-kNN-Tier | — | alle drei Gates | `TODOS.md:626-629` |
| 68 | v2.5 KG-Retrieval-Upgrade | KG-2…KG-5 offen (KG-1 ausgeliefert); MVP KG-2+KG-4 ≈ 4 Wochen | v2-Föderation zuerst | `TODOS.md:631-663` |
| 69 | Auto-Archiv-Politik der Brain-Review-Queue | — | 4–8 Wochen Nutzungssignal | `TODOS.md:711-734` |
| 70 | In-Memory-Cache für Sprecher-Centroide + Wakeword-Templates | „measure first" | A1 zuerst | `TODOS.md:738-748` |
| 71 | Mobile-Receipt-Capture: 6 zurückgestellte Posten | E6–E9, Split-ZIP, App-Icon | M2 bzw. Phase 1.5a | `TODOS.md:752-766` |

*(Nicht als Arbeitspaket gezählt: der reine Info-Eintrag „Meetily" `TODOS.md:492-510`.)*

**Als erledigt durchgestrichen: 45 Zeilen** (Einträge und Unterpunkte), dazu `:356` mit ausschließlich gestrichenen Teilposten. Ohne Durchstreichung als erledigt markiert: `:133`, `:143`, `:147-149`, `:221-231`, `:234`, `:307`, `:371`, `:373-377`.

**Seit 2026-09-18 erledigt/entfernt** (Nummern der alten Liste): #3 Security-Audit-Dach (`:110`) · #5 D7(a) Token im Log (#1452) · #6 F7 Browser-Mikro im Haushalt (`:122`) · #11 `meeting_whisper_model=large-v3-turbo` beidseitig (`:133`) · #16 M1 im SSO-Cutover aufgegangen · #17 M2-Frontend · #18 Low/Info (#1295, #1296, #1122, #1123) · #20 M5 TOCTOU (#1122) · #21 Paperless-Schreib-Gate (#1123; TODOS `:160` nicht nachgezogen) · #26 Kollision `remote_user_id` (F-ID-3, #1420) · #27 Personenbezogene Föderation (#1419; TODOS `:194` nicht nachgezogen) · #31 Lane B als Eintrag entfernt (#1294, „not planned") · #36 Union-Extractor gestrichen (#637) · #44 Multi-Replica-Lock „COVERED" (`:293`) · #50 Satellit Multi-Core „kein Posten" (`:307`) · #51(b) Presence-Debug gemergt (#942/#943) · #60 Playwright-Resolver wontfix (`:364`) · #67 OCR-Engine-Evaluation gelaufen 2026-07-23 (`:386`) · #72 erkannte Sprache auf `documents` — kein offener Eintrag mehr (`:408`) · #73 KG-Provenienz je Dokument obsolet (`:409`) · #76 `/design-consultation` → DESIGN.md (`:426`) · #82 Agenten-String-Pfad (`:578`) · #87 KG-1 Multi-Hop (`:650`) · #88 `itsm`-MCP ins Reva-Backlog (`:703`).

**Innere Widersprüche in `TODOS.md`** (zum Nachziehen): Presence `:107` („LANDING ACTIVE") gegen `:328` („LARGELY RESOLVED"); `:594` bildet #1313 nicht ab; `:164` nennt noch `main.tsx::_consumeOidcHashHandoff`; die Zeilenverweise der Gesamtsicht (`:45`, `:50`, `:56`, `:64`, `:66`, `:89`, `:91`) zeigen auf die Fassung vor dem Umbau; `:78` führt Föderations-Identitätslinks noch als „Entwurf, braucht Freigabe".

---

## 2. docs/design/*.md — 42 Dokumente, davon 30 mit offenem Status

*(Stand 2026-11-09; gezählt `*.md` direkt in `docs/design/`, ohne den Unterordner `spikes/` (4 Dateien). Seit 2026-09-18 neu: `auth-cookie-session`, `chat-upload-pdf-split`, `cross-instance-document-reference`, `household-auth-on-cutover`, `scanner-button-destination-and-delivery`, `shared-organisation-entities`, `structured-memory`. Am 2026-09-18 lagen dort 35 Dateien, nicht 37.)*

### Beschlossen, aber NICHT gebaut (Design fertig, kein Code) — 6
| Dokument | Statuszeile | Offene Stufen / Fragen |
|---|---|---|
| `kg-contact-points.md:4` | „Review-Entscheidungen getroffen 2026-09-13 (§11) — **kein Go zum Bauen**; Stufe 1 braucht ein ausdrückliches Go" | kein Code, kein Commit nach `22b5a0fb` (#1241); Standardregion aus der Instanzkonfiguration (`:125`); Stufen §10 (`:217`), Risiken §12 (`:241`) |
| `kg-cross-user-canonicalization.md:3` | „**SUPERSEDED for #876** — §14-Spike (2026-09-21) wählte einen `sameAs`-Co-Referenz-Linker; Phasen A–D NICHT gebaut, named-circles-v2 bleibt geparkt" | Linker „**no go** yet" (`:683-685`); offene Entscheidungen E-1…E-5 in `spikes/kg-coreference-vs-shared-ownership.md`; Risiken + O-1 `:450-486` |
| `gui-contribution-model.md:3` | „Design / proposal (not yet built)" | 3 offene Fragen (Registrierungsmechanismus, Read-only-Vokabular, Contract-Version-Policy) `:196-200` |
| `non-verbal-communication.md:3` | „PROPOSED / DESIGN — not implemented, not scheduled" | Head B (Körpersprache) zurückgestellt `:468,:482,:491`; MediaPipe-NPU-Port `:516-519`; Risiken/offene Fragen `:273` |
| `mobile-receipt-capture.md:3` | „DESIGN, revision 4 … R5–R7 am selben Tag"; nach R4 alle Produktentscheidungen getroffen (`:46`) | offen: Steuerberater-Checkliste + Phase-0-Checks (`:46`); Web-Capture (Option c) zurückgestellt R6-1 (`:407,:519,:2517`); `MOBILE_CAPTURE_ENABLED` fehlt im Code |
| `shared-organisation-entities.md:3` **(neu)** | „Entwurf zu Issue #1314. Status: **entschieden, nicht gebaut.**" | offen (`:163-183`): (b) nur `organization` oder auch `place`/`product`, (d) Backfill der gespaltenen Knoten, Reihenfolge zum Cutover, Klassengrenze Weltwissen vs. Haushalt; Abnahme in 7 Punkten (`:185`) |

### Teilweise gebaut / mit zurückgestellten Stufen — 24
| Dokument | Statuszeile | Offen |
|---|---|---|
| `kg-bitemporal-edges.md:4` | „**kein Go zum Bauen**" — **veraltet**: Go 2026-09-29 (§12.1, `:391-394`), Stufe 1 gebaut `331f0a52` (#1376, Migration `pc20260929_kg_relation_validity`) | Stufe 2 gesperrt, eigenes Go (`:396`); Stufe 3 (`/brain/review`) zurückgestellt (`:388,:400`) |
| `federation-identity-mapping.md:3` | „F-ID-1 SHIPPED … **F-ID-2 SHIPPED 2026-10-27** … **F-ID-3 SHIPPED 2026-10-28** … **Open:** nichts mehr aus dieser Reihe"; Flag an (`k8s/configmap.yaml:76`) | zurückgestellt: Erreichbarkeitsprobe beim Pairing (P3), TLS-gepinnter Transport übers Internet (`:200,:202-205`); §7 (`:146-151`) größtenteils beantwortet |
| `cross-instance-document-reference.md:3` **(neu)** | „Entwurf, 2026-10-27" — **veraltet**: Stufe 1 gebaut `6e37b739` (#1418; `config.py:129` `instance_id`, `scanner_jobs.py:354-358`, `artifact_service.py:373`), Stufe 2 erledigt laut `federation-identity-mapping.md:3` | Stufe 3: Proxy-Endpunkte `peer.document_status`/`peer.document_fetch` (`:179`), kein Code; 4 offene Entscheidungen (`:185-196`) |
| `household-auth-on-cutover.md:4` **(neu)** | „Entscheidungen D-2 … D-10 getroffen 2026-09-21 … Umschalten erst nach dem Runbook" — **veraltet**: Cutover lief 2026-09-23 (#1324), P0 (1)–(8) „gemergt/gebaut" (`:209`) | Subsume-Wiedereinschalten offen: `MEMORY_SUBSUME_TO_KG: "false"` „nach dem Cutover bewusst wieder einschalten" (`k8s/configmap.yaml:571`; `docs/ENVIRONMENT_VARIABLES.md:1892-1898`) |
| `auth-cookie-session.md:95` **(neu, ohne Statuszeile)** | „**Deferred (own follow-ups)**" | SSO-Fragment-Handler/`?code=`-Cutover (braucht Reva); `/api/ws/token` + Bearer-Interceptor ablösen (`main.py:494`, `utils/wsToken.ts:26`); `FEATURE_VOICE` auf xidra (`:95-99`). `:101` „`SECRET_KEY` … no split, no rotation" **veraltet**: Rotation gebaut `980ff7fd` (#1297, `config.py:1417`) |
| `structured-memory.md` **(neu, ohne Statuszeile)** | Langform, aus `CLAUDE.md` ausgelagert | „**Still single-user only**" — namensbasierte Erfassung und nutzerübergreifende Subjektauflösung (`:308-309`); Graph-Expansion-Neubau erneut zurückgestellt, MVP auf `feature/structured-memory-phase4-subsume` (`:327-328`) |
| `voice-identity-wakeword-verification.md:3` | „DESIGN, REVIEWED … **PR 1 built:** P0 fail-loud + C1 Opus … A0/A1/C2/C3/A2 not built" | A0-Experiment als Tor für A1 (`:5,:35`); Operator-Entscheidungen §7: A1-Template-Scope, C2-Session-Scope (`:153,:155`) |
| `sso-token-handoff-hardening.md:3` | „EMPFÄNGERSEITE AUSGELIEFERT, dunkel … was fehlt, ist der **Cutover**"; Renfield-eigener Emitter seit #1344 (`:8-12`) | Reva-Emitter auf `?code=`, dann Fragment-Handler entfernen; 3 offene Fragen §10 (`:253-264`) |
| `ingest-credentials.md:3` | „PHASEN 1, 2 UND 4 AUSGELIEFERT … Offen ist ausschließlich **Phase 3 — Selbstrotation**" | Admin-Rotation existiert (`api/routes/ingest_credentials.py:197`); Selbstrotation mit Gnadenfenster fehlt |
| `user-events-ws.md:3` | „**AUSGELIEFERT**" | `obligations_changed`/`notes_changed` zurückgestellt (`:147`), nur als Kommentar (`hooks/useUserEvents.ts:98`) |
| `chat-ui-modernization.md:3` | „**largely shipped** — items 1–8 and 10 are live" | Item 9 Sichtbarkeit Haushalt vs. privat (`:4,:227`) + zwei Vorbedingungen (Interaction-Share-Instrumentierung, a11y/Mobile-Baseline) (`:4-5`) |
| `self-learning-admin-console.md:3` | „SHIPPED (v2.10 …); deferred P2/P3 items live in `TODOS.md`" | P2/P3-Tabelle (`:207-216`): Bulk-Approve, Tool-Health-Charts, Trajektorien-Diff, Shadow-Log entfernen, Cascade-Vote; Shadow-Log-Retention (`:222`) |
| `scanner-ingest.md:3` | „Phases 0–3 BUILT … Not yet verified end-to-end with real paper … **Phases 4–5 open**" — **teilweise veraltet**: Phase 5 (Knopf) „gebaut, AUS als Vorgabe" (`:627,:634-635`; `SCANNER_BUTTON_WATCH`, scanner #12) | Phase 4 (k8s, optional, nur Paketierung) (`:625`); Papier-Ende-zu-Ende: in den öffentlichen Docs kein Beleg — offen bzw. unklar |
| `meeting-kg-and-speaker-identity.md:3` | „APPROVED v2 … **Shipped:** Phase 0, Track D, Track A Increments 1+2 (dark) … **Open:** Track B, Track C" | Track B (Extraktion→KG), Track C (Commitments); 5 offene Fragen/Risiken (`:106-112`); Auto-Match „not planned" (Flag entfernt, `a525675e`) |
| `meeting-transcription.md:3` | „**SHIPPED — LIVE on both instances**" | Pro-Chunk-Checkpoints (Resume nach Absturz) nicht gebaut (`:122-124`); Satelliten-Aufnahme eigene spätere Phase (`:117`) |
| `speaker-enrollment-redesign.md:3` | „Phases 0–3 BUILT (dark by default). Phase 4 (upstream capture / XVF3800) still research." | Software-Referenz-AEC (`:123`), Target-Speaker-Extraction (`:125`) zurückgestellt; duck-on-listen gebaut und im Haushalt an |
| `ble-presence-improvement.md:65` | „SHIPPED + DEPLOYED … Phase 1c shipped later; Phases 1b/3 remain deferred" | Phase 1b AdvertisementMonitor (`:55`), Phase 3 Reichweite (`:148`); 3 offene Fragen (`:162-165`) |
| `output-providers.md:3` | „SHIPPED — live in the household … Remaining: brand-shim + discovery-method removal" | Brand-Tools + Discovery-Methoden bis zur Orchestrierungsmigration (`:229-239`); beratende offene Fragen (`:272-276`) |
| `paperless-llm-metadata.md:3` | „SHIPPED — PR 1–5 … Vision-first extraction (‚PR 2b') not built … kNN tier + Redis-pubsub taxonomy cache stay deferred by design" | `superseded`-Re-Edit-Filter zurückgestellt (`:3`); kNN-Tier (`:97,:1103`) |
| `mcp-self-detection.md:3` | „Phase 1 + 2 + 3 + 4 SHIPPED (Phase 3 item 3, the Plane-B kiosk verdict, is still open)" | Kiosk-Urteil für die Plane-B-Ingest-MCPs (`:395-398`); transparentes Wait-and-Retry bewusst nicht gebaut (`:349-350`) |
| `a733-satellite-display.md:3` | „software port DONE, **hardware not yet attached** (2026-08-10)" | Hardware anschließen, Pin-/Overlay-Werte; seit 2026-08-10 kein Commit |
| `browser-voice-auth-on-instances.md:3` | „code + infra prepared, **`FEATURE_VOICE` NOT flipped**" — stimmt (`../x-ren/k8s/configmap.yaml:332` = `"false"`) | Flag umlegen; D4 Sprecher-Opt-in (DSGVO zuerst), D7(b) Origin-Prüfung (`:130-131`); D7-Token-Redaktion erledigt (#1452). `:133` „the household is auth-off" **veraltet** |
| `pdf-split.md:3` | „PR1 + PR2 + PR3 implemented. Dark by default" — **veraltet**: auf beiden Instanzen an (`k8s/configmap.yaml:595`, `../x-ren/k8s/configmap.yaml:431`) | „Children-list on the archived parent card: deferred polish" (`:330-331`) |
| `notes-atom.md:3` | „**SHIPPED** (4B.1–4B.3 + dense embedding)" | eigener `note`-Zweig im Wissen-Drawer zurückgestellt (`:159`; `WissenDetailDrawer.tsx:117-213` kennt kein `note`); note→beliebige KG-Entität auf v2 (`:7`) |

### Ohne offene Punkte — 12
`a733-satellite-camera.md` (live), `broadcast-announcement.md` (implementiert), `chat-branching.md` (Phase 1+2, beidseitig an), `meeting-minutes.md` (ausgeliefert, im Haushalt seit 2026-09-21 an), `scheduled-tasks.md` (alle Phasen; der Vermerk „Phase-1 follow-up still open" `:21` ist **veraltet** — `internal.paperless_dedupe` ist dünner Aufrufer von `mcp.paperless.dedupe_documents` seit `cce88ed9` #1138; das Dokument benennt den Widerspruch selbst `:474-477`).

Aus der alten Tabelle hierher gewandert:
- `atoms-granularity.md:3` — entschieden und umgesetzt (`pc20260423_atoms_per_document`); auch der früher fehlende Vektorindex existiert (`idx_document_chunks_embedding_hnsw`).
- `ocr-engine-eval.md:3` — Evaluierung lief 2026-07-23 (148 Dokumente), Ergebnis Tesseract als Standard (#1029/#1033/#1034).
- `satellite-audio-combine-pipeline.md:3` — „DESIGN → **BUILT**" (`b0ef9001`); §9 Q1 (`:150`) nur noch Verifikationsnotiz.
- `chat-artifacts-sandbox.md` — Lane A ausgeliefert; Lane B „**not built and not planned** (decision 2026-09-20)", Platzhalter-Flag entfernt (`:758`, `a525675e`); die Kopfzeile `:3` „DESIGN PASS ONLY" ist überholt.
- `command-center.md` — SUPERSEDED, 2026-07 stillgelegt; offene Fragen gegenstandslos.

Neu und ohne offene Punkte:
- `chat-upload-pdf-split.md:3` — „gebaut (Variante A)".
- `scanner-button-destination-and-delivery.md:3` — „umgesetzt"; Knopfwächter AUS als Vorgabe ist eine Hardwaregrenze des S1500, kein offener Posten.

---

## 3. CLAUDE.md / `.claude/rules/` — 7 von 13 Stellen „deferred / dark / nicht verdrahtet" gelten weiter

> „früher" = Zeile in `CLAUDE.md` VOR der Aufteilung vom 2026-09-20 (`git show f8a33cc6:CLAUDE.md`). Seit `a6fabb92` (#1292) liegen die Inhalte in `.claude/rules/*.md` und `docs/`. Geprüft 2026-11-09 gegen Rules, `docs/`, Code und beide ConfigMaps.

### Gilt weiter (7)

| Punkt | Stand 2026-11-09 | Heutige Fundstelle | früher |
|---|---|---|---|
| Lane B (Freiform-HTML/SVG im Sandbox-iframe) | **verschärft:** „not built and not planned"; Platzhalter-Flag entfernt (`a525675e`, #1294); ein Bau bräuchte eigenes Flag + Security-Review | `.claude/rules/chat-ui.md:17-18`, `docs/design/chat-artifacts-sandbox.md` | `CLAUDE.md:75` |
| Meeting-Auto-Match | **verschärft:** „not built and not planned" (2026-09-20); `meeting_auto_match_enabled` entfernt | `.claude/rules/meetings.md:15-16`, `docs/design/meeting-kg-and-speaker-identity.md:3,50` | `CLAUDE.md:81` |
| §2 KG+Speaker-Redesign, Tracks „phased and dark" | Phase 0, Track D, Track A Inkr. 1+2 ausgeliefert; Track A dark (`meeting_fingerprints_enabled` in keiner ConfigMap). **Track B und C offen** | `docs/design/meeting-kg-and-speaker-identity.md:3,65` | `CLAUDE.md:85` |
| SSO-Handoff dark; Legacy-Fragment-Handler bis zum Reva-Cutover | `SSO_HANDOFF_ENABLED` dark; Handler in `src/frontend/src/utils/ssoFragment.ts:35-43` (aus `main.tsx:7,42`). Neu, ebenfalls dark: OAuth-Redirect-Provider mit serverseitigem State + PKCE (#1501) | `.claude/rules/auth.md:28,60-75`; TODOS `:164-166` | `CLAUDE.md:211`, `:223` |
| JWT-Cookie-Restschuld | **teilweise:** Voice-WS-Migration erledigt (#1128). Offen: `/api/ws/token`-Faucet (jetzt mit Pflicht `scope:ws`, #1343) + Bearer-Interceptor; `SECRET_KEY` weiter ohne Split (Rotation über `SECRET_KEY_PREVIOUS` möglich). Haushalt auth-on **ohne** `AUTH_COOKIE_ENABLED` | `.claude/rules/auth.md:27-32`; TODOS `:46`, `:169` | `CLAUDE.md:227`, `:300` |
| Notes: `note`-Zweig im Wissen-Drawer | unverändert zurückgestellt; generischer Fallback greift | `.claude/rules/notes-wissen.md:46-47` | `CLAUDE.md:326` |
| Dokumentsuche PR2 (föderiert über Paperless) | unverändert „deferred" | `docs/SECOND_BRAIN.md:268`, `docs/FEATURES.md:94`; TODOS `:129` | `CLAUDE.md:338` |

### Erledigt (6)

| Punkt | Beleg | früher |
|---|---|---|
| Kiosk: `peer_status_changed`-Delta | gebaut `60eefb47` (#969, 2026-07-13 — der Vermerk war schon 2026-09-18 veraltet); der Wall-Clock-Backstop bleibt als zweite Sicherung (`.claude/rules/kiosk.md:32-35`) | `CLAUDE.md:75` |
| Command Center „slated for decommission" | entfernt; `/kiosk` ist die einzige Oberfläche (`.claude/rules/kiosk.md:9-11`, `docs/design/command-center.md` SUPERSEDED) | `CLAUDE.md:75` |
| Graph-Expansion: Agenten-String-Pfad auf den fusionierten Pfad | #874 / PR #1196 (TODOS `:578`, gestrichen) | `CLAUDE.md:75` |
| Meeting-Minutes im Haushalt `false` | `MEETING_MINUTES_ENABLED: "true"` beidseitig (`k8s/configmap.yaml:637`, `../x-ren/k8s/configmap.yaml:238`) | `CLAUDE.md:83` |
| Memory→KG-Bridge „opt-in/dark" | Config-Standard weiter dark (`.claude/rules/memory-extraction.md:18`), auf **beiden** Instanzen an (`k8s/configmap.yaml:570`, `../x-ren/k8s/configmap.yaml:299`) | `CLAUDE.md:366` |
| OTA-Signatur: Flotte re-provisionieren, dann fail-closed | fail-closed seit 2026-08-23 (`src/satellite/provisioning/group_vars/satellites.yml:59`, `SATELLITE_OTA_REQUIRE_SIGNATURE: "true"` `k8s/configmap.yaml:313`, `.claude/rules/satellite-trust-ota.md:23`). Rest: laut `satellites.yml:53-57` übernehmen wohnzimmer + arbeitszimmer die Einstellung erst bei der nächsten Re-Provisionierung — ob das geschehen ist, zeigt das Repo nicht; `satellites.yml:47-48` kommentiert noch den alten Stand | `CLAUDE.md` §„Signed OTA packages" |

---

## 4. Dunkel geschaltete Funktionsschalter

**Methode (Stand 2026-11-09):** `src/backend/utils/config.py` → alle `bool = False`-Vorgaben (**101 Flags**; per `grep` und
unabhängig per AST-Zerlegung gezählt, beide 101), abgeglichen gegen die **LIVE**-ConfigMap `renfield-env`, die das
Backend-Deployment per `envFrom` einbindet (Kontext `renfield-private`, Namensräume `renfield` und `renfield-xidra`),
plus die Literal-`env:`-Einträge des Deployments (dort steht genau ein Schalter: `WISSEN_WORKSPACE_ENABLED=true` im Haushalt,
in der ConfigMap ebenfalls `true`). Das Secret `renfield-env-private` wurde auf Schlüsselnamen geprüft — es enthält
keinen `config.py`-Schalter. Live an: Haushalt **70** von 101, xidra **58**. Die Git-Kopien `k8s/configmap.yaml` und
`../x-ren/k8s/configmap.yaml` stimmen bei allen Schaltern mit dem Live-Stand überein. `../x-ren/k8s/renfield-env.configmap.yaml`
(Quelle der Vorgängererhebung) ist **veraltet** (letzter Commit 2026-09-17) und wird nicht mehr ausgerollt.
Grenzen der Methode: Schalter außerhalb von `config.py` sind nicht gezählt — `PRESENCE_ENABLED`/`MEDIA_FOLLOW_ENABLED`
(`ha_glue/utils/config.py`), die `*_MCP_ENABLED`/`JELLYFIN_ENABLED`/`RADIO_ENABLED`-Schalter aus `mcp_servers.yaml` und
`FEATURE_*` (`bool | None`).

### 4a. Dunkel im Haushalt, aber auf xidra AN (3, Stand 2026-11-09)
`AUTH_COOKIE_ENABLED` (`config.py:1513`), `FOLDER_INGEST_SIMBA_ENABLED` (`:1042`), `PROJECTS_ENABLED` (`:745`).

Unverändert gegenüber der Messung vom 2026-09-27; erneut gegen die LIVE-ConfigMaps beider Instanzen gemessen.

🛑 **`AUTH_COOKIE_ENABLED` bleibt ein offener Befund.** Im Haushalt ist die Anmeldung an (seit 2026-09-23, #1324), der
Cookie-Schalter aber weiterhin nicht gesetzt (Standard `False`, `config.py:1513`) — das Sitzungstoken liegt dort in
`localStorage` und ist für jedes Skript auf der Seite lesbar, während xidra mit `AUTH_COOKIE_ENABLED=true` eine
HttpOnly-Sitzung führt. Das Muster „unkritisch, solange Flag F aus ist" mit abgelaufenem Verfallsdatum; gehört in die
Cutover-Restliste, nicht in eine Aufzählung dunkler Funktionen.

### 4b. Nie eingeschaltet — auf KEINER Instanz (28)
Das ist die Liste fertiger, aber nie aktivierter Funktionen.

| Flag | config.py | Kurz (laut Doku) |
|---|---|---|
| `AGENT_PRESELECT_PROMPT_DUMP` | `:400` | protokolliert den VOLLEN Vorauswahl-Prompt samt ungekürzter Nutzernachricht; Diagnose-Schalter, im Haushalt bewusst `"false"` (`k8s/configmap.yaml:421`) |
| `CALENDAR_ENABLED` | `:187` | Calendar-MCP — Vorbedingung des Fristen-Kalender-Syncs |
| `OBLIGATION_CALENDAR_SYNC_ENABLED` | `:2011` | Fristen→Kalender-Reconciler, braucht `CALENDAR_ENABLED`; blockiert auf Credentials |
| `CARD_EMIT_INLINE` | `:395` | Inline-Card-Emission (Umschalten erst nach Reva-Rollout) |
| `FEDERATION_PENDING_USE_REDIS` | `:1352` | Pending-Federation über Redis (nötig bei mehreren Workern) |
| `KG_VALIDITY_FILTER_ENABLED` | `:947` | Gültigkeitsfilter für KG-Kanten (#875 Stufe 1); aus = SQL byte-identisch |
| `LDAP_AUTH_ENABLED` | `:1564` | LDAP-Provider (authn-only) |
| `OAUTH_GOOGLE_ENABLED` / `OAUTH_GITHUB_ENABLED` / `OAUTH_APPLE_ENABLED` | `:1583/:1588/:1593` | Redirect-Provider, „enabling is config-only" |
| `SSO_HANDOFF_ENABLED` | `:1527` | PKCE-Empfänger (`POST /api/auth/sso/exchange`), wartet auf den Reva-Emitter |
| `MCP_HEALTH_RATE_LIMIT_SIGNAL_ENABLED` | `:1953` | 429-Signal für die MCP-Gesundheit (Phase 3) |
| `MEETING_FINGERPRINTS_ENABLED` | `:783` | Track-A-Increment-1 (cross-meeting anonyme Fingerprints), ausgeliefert dark |
| `MEETING_FINGERPRINT_AUTONAME` | `:796` | automatische Benennung — „Keep OFF until real-meeting calibration" |
| `MEETING_KEEP_AUDIO` | `:799` | Audio nach Abschluss behalten |
| `MEMORY_CONTRADICTION_RESOLUTION` | `:848` | LLM-basierte Widerspruchsauflösung |
| `MEMORY_EXTRACTION_V2_AUTHORITATIVE` | `:854` | Phase-B-Flip (Phase A `MEMORY_EXTRACTION_V2_SHADOW` läuft im Haushalt); blockiert die Trajektorien-Diff-Ansicht. Subsume-Tor seit #1313 (2026-09-23) im v2-Pfad. |
| `MEMORY_SUBSUME_TO_KG` | `:986` | Phase 3-subsume; im Haushalt seit dem Cutover 2026-09-23 **bewusst** `"false"` (`k8s/configmap.yaml:571`) — der Startwächter verweigert `true` zusammen mit `AUTH_ENABLED=true` |
| `NOTIFICATION_POLLER_ENABLED` | `:1866` | MCP-Notification-Polling |
| `PAPERLESS_INDEX_HEAL_ENABLED` | `:1256` | explizit `"false"` in beiden ConfigMaps (`k8s/configmap.yaml:590`, `../x-ren/k8s/configmap.yaml:430`) |
| `PAPERLESS_INDEX_HEAL_ALLOW_WORKFLOWS` | `:1274` | zweite Stufe, auf keiner Instanz gesetzt |
| `PDF_SPLIT_BLANK_FOLD_ENABLED` | `:1123` | leere Seiten dem vorangehenden Dokument zuschlagen (Scanprüfung 2026-10-31) |
| `PROACTIVE_ENRICHMENT_ENABLED` | `:1851` | Anreicherung proaktiver Meldungen |
| `PROACTIVE_FEEDBACK_LEARNING_ENABLED` | `:1859` | Feedback-Lernen |
| `PROACTIVE_URGENCY_AUTO_ENABLED` | `:1850` | automatische Dringlichkeit |
| `RAG_FORCE_OCR` | `:590` | Voll-OCR erzwingen (bewusst aus) |
| `SPEAKER_INPROCESS_EMBEDDINGS_ENABLED` | `:229` | In-Process-Embeddings (nur Entwicklung ohne voice-server) |
| `SPEAKER_QUALITY_GATING_ENABLED` | `:240` | Phase 0 der Sprecher-Neuausrichtung, dark ausgeliefert |

### Seit 2026-09-18 eingeschaltet (aus der alten 4b)
- `FEDERATION_IDENTITY_LINKS_ENABLED` (`:1361`) — an auf **beiden** Instanzen.
- `WS_REQUIRE_SCOPED_QUERY_TOKEN` (`:1644`) — an auf **beiden** Instanzen (#1343).
- `VOICE_AUTH_ENABLED` (`:1601`) — an im **Haushalt** als Stimm-Zweitfaktor (Rule `voice-second-factor.md`), auf xidra nicht gesetzt.
- `MCP_RATE_LIMIT_BACKOFF_ENABLED` (`:1961`) — an im **Haushalt**, auf xidra nicht gesetzt.

Fünf weitere Einträge der alten Liste sind **entfernt**, nicht eingeschaltet (kein Leser, #1294, `a525675e`):
`ARTIFACTS_HTML_SANDBOX_ENABLED`, `REQUIRE_EMAIL_VERIFICATION`, `WAKE_WORD_ENABLED`, `MEETING_AUTO_MATCH_ENABLED`,
`TRAJECTORY_REDACT_PII`. Neu in 4b: `AGENT_PRESELECT_PROMPT_DUMP`, `KG_VALIDITY_FILTER_ENABLED`, `PDF_SPLIT_BLANK_FOLD_ENABLED`
(seither hinzugekommen) und `MEMORY_SUBSUME_TO_KG` (im Haushalt zum Cutover abgeschaltet).

Zusätzlich **aktiv abgeschaltet** (Default true bzw. bewusst `"false"` gesetzt):
`RAG_RERANK_ENABLED` auf beiden (`k8s/configmap.yaml:407`, `../x-ren/k8s/configmap.yaml:256` — Reranking lief nie),
`ALLOW_REGISTRATION` auf beiden (`k8s/configmap.yaml:77`, `../x-ren/k8s/configmap.yaml:32`),
`SAMSUNG_MCP_ENABLED` auf beiden (`k8s/configmap.yaml:456`), `LLM_OPENAI_FOR_INTENT` auf beiden (`:269`),
im Haushalt `MEMORY_SUBSUME_TO_KG` (`:571`, Startwächter) und `AGENT_PRESELECT_PROMPT_DUMP` (`:421`),
und auf xidra: `HA/N8N/SAMSUNG/DLNA_MCP_ENABLED`, `PRESENCE_ENABLED`, `MEDIA_FOLLOW_ENABLED`, `RADIO_ENABLED`, `JELLYFIN_ENABLED`,
`FEATURE_SMART_HOME/CAMERAS/SATELLITES/VOICE`, die 4 Speaker-Flags (`SPEAKER_RECOGNITION_ENABLED`, `SPEAKER_AUTO_ENROLL`,
`SPEAKER_CONTINUOUS_LEARNING`, `SPEAKER_VOCAB_CAPTURE_ENABLED`)
(`../x-ren/k8s/configmap.yaml:148-151,318-325,329-332`).

---

## 5. Sonstige Dokumente

### `docs/TECHNICAL_DEBT.md` — 5 offene Posten (Stand 2026-09-20/-27; Übersicht `:13-21`: 5 offen / 0 kritisch / 31 behoben)
1. **I1 Harbor push/pull vom Heim-Netz langsam** — WAN-Hairpin über die Public-IP (~72 Mbit/s), kein MTU-Problem; weiter offen — `:23`, `:487-514`.
2. **#12 `procedural_skills.status` → Partial-Indexe** — „**Geparkt** (2026-09-20) — kein Arbeitspaket, solange kein Seq-Scan gemessen ist" — `:168-183`.
3. **#3 Große API-Route-Dateien** — `routes/knowledge.py` über der Split-Schwelle (~1500), „eigenes Arbeitspaket (M)"; Doku nennt 1665 Zeilen, **heute 1940**. `routes/speakers.py` 875 (Doku 865), „beobachten" — `:85-93`.
4. **Frontend #6 Große Komponenten** — `useDeviceConnection.ts` in Sub-Hooks zerlegen; Doku 637 Zeilen, **heute 727**. `SpeakersPage.tsx` 961 — `:364-372`.
5. **Frontend #7 Fehlende Error Boundaries** — weiter nur die zentrale (`App.tsx:411`) plus feature-lokal `ArtifactErrorBoundary`; je Top-Level-Route fehlen sie — `:374-376`.

**Erledigt seit 2026-09-18:** #13 Backfill-vor-Index-Drop (Konvention in `.claude/rules/migrations.md`, `:185-187`) · fehlende Tests für Audio-Output, Output-Routing, Frigate (`:665-670`; offen nur „Satellite Hardware", `:671`) · Dependency Updates Minor/Major (`:695`, `:699`) · der Testzahlen-Widerspruch (Tabelle neu gemessen 2026-09-27, `:625-628`).
**Weiter offen:** „Requirements pinnen" — 🔄 „Voll-Lock + monatliche Aktualisierung (BL-0447), in Arbeit" (`:687`); `src/backend/requirements.txt` hat 2 exakte `==`-Pins gegen 50 `>=`/`~=`, kein Lockfile.

### `src/satellite/TECHNICAL_DEBT.md` — 4 offene Future-TODOs
Audio-Preprocessing aufs Backend (`:64`) · **Boot-Window-WS-Handshake-Timeout** (`:72-88`; Diagnoseplan mit `tcpdump` steht, Workaround `systemctl restart`. Ob der Server-Eintrag in `/etc/hosts` samt cloud-init-Vorlage (#1517) den mDNS-Mechanismus erledigt, ist nicht verifiziert) · Echo Cancellation (`:99`) · 4-Mikrofon-Beamforming (`:112`).
**Erledigt:** Opus-Kompression (C1, `:94`); Wake-Word-Training (`src/satellite/wakeword-training/`, `:119`).

### Weitere `docs/*.md`
**Gilt weiterhin:**
- `docs/RAG_PARITY_PLAN.md:1-3,149` — **Status: PARKED** seit 2026-04-19, unverändert.
- `docs/voice-pipeline-plan.md:109` — **B-4 Backend-Audio-Preprocessing-Offload: STILL DEFERRED**; neu `:117`: offene Frage deutsches Vollband-TTS (44,1/48 kHz) oder geklonte Stimme.
- `docs/VOICE_PIPELINE_DESIGN.md:44` „Non-goals (explicitly deferred)", `:489-496` Risiken (AEC N2; Reconnect-and-resume auf Phase B.next), `:571`/`:812` **hostPath → PVC auf Phase B.next verschoben** (voice-server-Manifest liegt nicht in diesem Repo — nicht prüfbar).
- `docs/B5_XTTS_EVAL.md:30` — Produktionskorpus-Stichprobe (D3) **deferred**, blockiert auf manueller Anonymisierung.
- `docs/FOLDER_INGEST.md:191`, `docs/FEATURES.md:49` — aufgeschobener `created_date`/OCR-PATCH bleibt abgesichert (dokumentierter Restposten).
- `docs/HANDOVER_graph_expansion.md:96` — Chunk/Memory→Entity-Pivots auf Phase 3 verschoben.

**Erledigt / überholt:**
- `docs/FEDERATION_PAIRING.md:121-166` — der „still deferred"-Teil (Consent-Handshake, F-ID-2) ist „**Built and shipped 2026-10-27**" (`:162-166`).
- `docs/SATELLITE_ACOUSTIC_COMMISSIONING.md` — Esszimmer nicht mehr „parked, needs new hardware": läuft v5, seit 2026-11-06 bare-metal per Ansible (`:353`).
- `docs/KUBERNETES_DEPLOYMENT.md:163-164` — Cluster-Umbenennung entschieden: „no rename back … `-r1` is the name".
- `docs/VOICE_PIPELINE_DESIGN.md:785` — Speaches nur noch historischer Fallback, der voice-server ist live.

**Neu seit 2026-09-18:**
- `docs/CIRCLES.md:288-291` — **offene Eigentümerentscheidung:** ein Schreib-Sharee, der in eine geteilte KB ablegt, erzeugt ein Dokument mit dem KB-Eigentümer als Atom-Eigentümer.
- `docs/SECOND_BRAIN.md:268` — „**PR2 (deferred):** federated document search across Paperless".
- `docs/ENVIRONMENT_VARIABLES.md:1892-1898` — mehrnutzerfähiges Subsume „gebaut, aber noch nicht freigegeben … nach dem Cutover bewusst wieder einschalten".
- `docs/ENVIRONMENT_VARIABLES.md:1081-1083`, `docs/POSTMORTEM_PG_OUTAGE_2026-09-12.md:482-486` — „Offen geblieben": A3b Netpol (nur bei `default-deny`) und **A3c Beobachter außerhalb des Clusters** (fallen beide Instanzen gleichzeitig aus, schweigt die gegenseitige Prüfung).
- `docs/GPU_TOPOLOGY.md:558-563` — „**Offen, nur physisch zu klären:** Dual-Slot-Platz, Netzteilreserve, Luftstrom, IOMMU-Gruppierung"; „**Weiter offen:**" onnxruntime-Wheel nicht per `cuobjdump` geprüft, Einbettungsdurchsatz und Whisper fp16/int8 auf Volta ungemessen.

**Neu, aber veraltet (Marker widerspricht dem Code):**
- `docs/POSTMORTEM_PG_OUTAGE_2026-09-12.md:487` „Ph3 MCP-Self-Detection — designed, nicht gebaut" — gebaut (`docs/design/mcp-self-detection.md:3`); historischer Schnappschuss.
- `docs/ENVIRONMENT_VARIABLES.md:644-645` „`continued` … noch nicht backend-verdrahtet" — verdrahtet (`services/conversation_handoff.py:220`, `ha_glue/api/websocket/satellite_handler.py:1309`, `ead6e004`).
- `docs/runbooks/cookie-auth-flag-flip-xidra.md:17-19,:117` „awaits the deferred voice-WS migration" — erledigt in `a4aa022a` (#1128).

### Ausdrücklich ohne Ergebnis
- `docs/GPU_TOPOLOGY.md`: der frühere Vermerk „nur als Memory geführt" ist überholt — die offenen Punkte stehen jetzt in der Datei (`:558-563`).
- `docs/runbooks/` enthält jetzt **zwei** Dokumente; `auth-on-cutover-household.md` trägt nur Vorflug-Marker aus der Zeit vor dem Cutover (Runbook-Schritte, keine offenen Posten).
- `docs/private/` nicht inhaltlich ausgewertet (nur Dateinamen).
- `tasks/` siehe Teil C.

---

# Teil B — GitHub-Issues

Stand: **2026-11-09**, reine Bestandsaufnahme (read-only, nichts geschlossen).
Quelle: `gh issue list --state open --limit 300`, `gh pr list --state open`, und je Issue
`git log main --grep '#<n>'` — jede Zuordnung „erledigt" ist am Commit nachgelesen, nicht am Titel.

- **Offene Issues: 69** (2026-09-18: 41). Seither **geschlossen: 22**, **neu eröffnet: 37**,
  **gemergte PRs: 204**.
- **Davon auf `main` erledigt, aber noch offen: 35.** Das ist die wichtigste Zahl dieses Teils:
  über die Hälfte der offenen Issues ist kein Rückstand, sondern ein ausstehender Schließvorgang.
  Schließen ist Sache des Eigentümers — hier wird nur festgestellt.
- **Teilweise geliefert / auf den Rest zugeschnitten: 5.** **Offen ohne Lieferung: 29.**
- **Offene Pull Requests: 1** — #1332 „docs(kg): Entwurf — das Modell entscheidet die Vorlage, nie die Faltung".

🛑 **Ein Issue ohne Verweis im Commit ist nicht automatisch offen.** #1382 wurde in #1433 nebenbei
behoben (beide Migrationstests nehmen jetzt die gemeinsame `pg_url`-Fixture), ohne dass der Commit
das Issue nennt. Die `--grep`-Zuordnung allein hätte es als offen geführt; die Gegenprobe am Code
(`tests/backend/test_pc20260924_restore_indexes.py:55-68`) hat es gefunden. Umgekehrt nennt nicht
jeder Commit mit `#<n>` eine Lösung — viele tragen nur „Refs".

## Alterskennzeichnung (Legende)

| Symbol | Bedeutung | Schwelle (letzte Aktualisierung) |
|---|---|---|
| 🟢 | in den letzten 30 Tagen aktualisiert | ab 2026-10-10 |
| 🟡 | 30–90 Tage still | 2026-08-11 bis 2026-10-09 |
| 🔴 | länger als 90 Tage unberührt | vor 2026-08-11 |

Verteilung: **🟢 26 · 🟡 22 · 🔴 21**. Die 🔴-Posten sind fast vollständig die Ideensammlung aus
Januar/Februar 2026; keiner davon wurde seit der letzten Erhebung angefasst.

---

## Gesamttabelle (nach Nummer)

| # | Titel | Labels | Erstellt | Aktualisiert | Komm. | Alter | Stand |
|---|---|---|---|---|---|---|---|
| 21 | Time and situation dependent audio volume | – | 2026-01-24 | 2026-01-24 | 0 | 🔴 | offen |
| 79 | feat: Push-Benachrichtigungen (Web Push API) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 | offen |
| 80 | feat: Gesichtserkennung (Face Recognition) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 | offen |
| 81 | feat: Horizontal Scaling (Multi-Instance Backend) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 | offen |
| 82 | feat: Grafana Dashboards für Monitoring | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 | offen |
| 84 | feat: Haptic Feedback für Mobile PWA | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 | offen |
| 85 | feat: Client Libraries (Python & TypeScript SDK) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 | offen |
| 121 | Improve error message when Ollama is not running | help wanted, good first issue, backend | 2026-02-11 | 2026-02-11 | 0 | 🔴 | ✅ #1505 |
| 122 | Write integration test for Weather MCP server | help wanted, good first issue, testing, mcp | 2026-02-11 | 2026-02-11 | 3 | 🔴 | ✅ #1505 |
| 124 | Add French language support (i18n) | help wanted, good first issue, frontend | 2026-02-11 | 2026-02-11 | 0 | 🔴 | offen |
| 125 | Add loading skeleton to chat message list | help wanted, good first issue, frontend | 2026-02-11 | 2026-02-16 | 2 | 🔴 | ✅ #1505 |
| 260 | Routines / Scenes — Voice-Triggered Workflow Chains | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 261 | Shopping List / Shared Lists | enhancement, backend, frontend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 262 | Natural Language Automation Builder | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 263 | Kiosk: Kalender- und Uhr-Kachel + Kachelauswahl je Gerät | enhancement, backend, frontend | 2026-02-24 | 2026-09-27 | 3 | 🟡 | offen |
| 264 | Voice Timers & Room-Aware Reminders | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 265 | Skill Marketplace — Community MCP Servers & Workflow Templates | enhancement, backend, frontend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 266 | Contextual Music Mood | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 267 | Proactive Room Briefings — Context-Aware Greetings | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 268 | Voice-Controlled Cooking Assistant | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 | offen |
| 270 | Paketverfolgung: Sendungsnummer aus E-Mail erkennen + präsenzgesteuerte Zustell-Ansage | enhancement, backend | 2026-02-24 | 2026-09-27 | 1 | 🟡 | offen |
| 342 | Phase 1: platform ↔ ha-glue extraction — Rest W3 Migration-Cutover (Q1 2027) | – | 2026-04-14 | 2026-09-27 | 1 | 🟡 | offen |
| 871 | Test renfield_en + renfield_it wake-word models (native EN / IT speaker needed) | – | 2026-07-01 | 2026-07-01 | 0 | 🔴 | offen |
| 875 | KG bi-temporal edges: expire-not-delete on contradicted kg_relations (valid_from/valid_to) | enhancement, backend | 2026-07-01 | 2026-11-03 | 3 | 🟢 | ◐ teilweise |
| 876 | Cross-user / household KG entity canonicalization (v2 named-circles) | enhancement, backend | 2026-07-01 | 2026-09-27 | 4 | 🟡 | offen |
| 877 | Populate kg_entities.external_id via an offline entity linker | enhancement, backend | 2026-07-01 | 2026-07-01 | 0 | 🔴 | offen |
| 1206 | Vision-Modell wird bei jeder Anfrage verdrängt — geteilte 16-GB-Karte ist überbucht | enhancement, backend | 2026-09-04 | 2026-09-27 | 3 | 🟡 | ◐ teilweise |
| 1210 | OTA kann keine neue Abhängigkeit ausliefern — Allowlist stammt aus dem laufenden Code (Henne-Ei) | backend, satellite | 2026-09-04 | 2026-09-04 | 0 | 🟡 | ✅ #1487 |
| 1211 | sat-wohnzimmer ist taub: WM8960 probe schlägt fehl (-110), kein Aufnahmegerät — Gerät wirkt im Dashboard gesund | satellite | 2026-09-04 | 2026-09-04 | 0 | 🟡 | ✅ #1514, #1515, #1516 |
| 1215 | feat(scanner): USB document scanner ingest with configurable 1..n routing targets | – | 2026-09-08 | 2026-09-18 | 2 | 🟡 | ◐ teilweise |
| 1218 | feat(ingest): per-integration ingest credentials with self-rotation | – | 2026-09-08 | 2026-09-18 | 4 | 🟡 | ◐ teilweise |
| 1240 | feat(kg): Kontaktpunkte — Telefon, Mobil, Fax, Mail, Web an Personen und Firmen | – | 2026-09-13 | 2026-09-13 | 0 | 🟡 | offen |
| 1279 | Satelliten-WS: ungeprüfte Feldtypen können die gesamte Admin-Liste auf 500 ziehen | backend | 2026-09-19 | 2026-09-19 | 0 | 🟡 | ✅ #1475 |
| 1280 | OTA-Fortschrittsmeldungen tragen keine Lauf-Kennung — ein Nachzügler kann einen neuen Lauf beenden | backend | 2026-09-19 | 2026-09-19 | 0 | 🟡 | ✅ #1475 |
| 1314 | KG: dieselbe Organisation über Nutzergrenzen ist EINE Entität | – | 2026-09-23 | 2026-09-24 | 2 | 🟡 | offen |
| 1315 | Modell-Kandidat: Ternary Bonsai 2 27B — zurückgestellt, hängt an llama.cpp PR #29077 | – | 2026-09-23 | 2026-09-23 | 0 | 🟡 | offen |
| 1329 | Reconciler verschmilzt near-identische Namen mit generischer Beschreibung automatisch — zwei Release-Versionen in Prod | bug | 2026-09-24 | 2026-09-24 | 0 | 🟡 | ✅ #1486, #1504 |
| 1343 | Security: WS_REQUIRE_SCOPED_QUERY_TOKEN von Soak auf Enforce heben (#1116 A) | security, backend | 2026-09-27 | 2026-09-27 | 0 | 🟡 | ✅ #1484 |
| 1344 | Auth: OAuth-Redirect-Provider — state serverseitig binden + PKCE, bevor ein Anbieter aktiviert wird (#1116 C) | security, backend | 2026-09-27 | 2026-09-27 | 0 | 🟡 | ✅ #1501 |
| 1345 | Auth: db-Passwort-Provider abschaltbar machen — Voraussetzung für LDAP als Autorität (#1116 D) | security, backend | 2026-09-27 | 2026-09-27 | 0 | 🟡 | ✅ #1483 |
| 1349 | Kiosk/Prüfschlange: mehrere vorsichtige Vorschläge reißen sich den Fokus gegenseitig weg (autoFocus je Karte) | enhancement, frontend | 2026-09-27 | 2026-09-27 | 0 | 🟡 | ✅ #1505 |
| 1359 | [Bug]: Docker Compose schlägt bei frischem Checkout fehl | bug | 2026-09-29 | 2026-09-29 | 0 | 🟡 | ✅ #1481 |
| 1374 | Schnitt räumt die Abschnitte des Originals ab, nicht seine Fakten und Graphbeiträge | backend | 2026-10-04 | 2026-11-03 | 2 | 🟢 | ✅ #1375; Rest per Entscheidung 2026-11-03 nach #875 |
| 1381 | agent: summary_parse_failed bei gültigen Antworten — Rohtext unsichtbar, {"answer"} ohne action wird verworfen | – | 2026-10-07 | 2026-10-07 | 0 | 🟡 | ✅ #1476, #1502 |
| 1382 | tests: zwei Migrationstests ignorieren die DSN-Normalisierung → psycopg2-Fehler bei postgresql://-URL | – | 2026-10-07 | 2026-10-07 | 0 | 🟡 | ✅ #1433 (ohne Issue-Verweis) |
| 1386 | 92 leere Sprachdurchläufe in 7 Tagen enden lautlos — Fehlweckruf und nicht verstandene Rede sind nicht unterscheidbar | backend | 2026-10-10 | 2026-10-10 | 1 | 🟢 | ✅ #1476, #1480 |
| 1387 | Die Mikrofon-Abnahme im Satelliten-Playbook läuft mit ignore_errors — ein Lauf ohne Mikrofon meldet Erfolg | satellite | 2026-10-11 | 2026-10-11 | 0 | 🟢 | ✅ #1503 |
| 1390 | Die Fertigmeldung eines Knopf-Scans wird 24 h lang mit 409 abgelehnt — ein Knopfdruck hat keinen Anfragenden | backend | 2026-10-11 | 2026-10-11 | 0 | 🟢 | ✅ #1479 |
| 1396 | Wohnzimmer: Inventar und host_vars kennen noch das alte 2-Mic-HAT, verbaut ist ein XVF3800 über USB | satellite | 2026-10-11 | 2026-10-11 | 0 | 🟢 | ✅ Inventar + host_vars am 2026-10-13 auf `xvf3800-usb` |
| 1397 | _ambient_inspect.py liefert bei USB-Mikrofonen keine Pegel-Herkunft — die Abnahme-Doku verlangt sie | satellite | 2026-10-11 | 2026-10-11 | 0 | 🟢 | ✅ #1503 |
| 1398 | Der Esszimmer-Pod kann keine Raumaufnahme bekommen — er steht nicht im Ansible-Inventar, und sein Negativmaterial fehlt ganz | satellite | 2026-10-11 | 2026-10-11 | 0 | 🟢 | ◐ teilweise |
| 1399 | deploy-production.sh --help funktioniert nicht: RENFIELD_REGISTRY wird vor der Argumentauswertung erzwungen | backend | 2026-10-11 | 2026-10-11 | 0 | 🟢 | ✅ #1474 |
| 1400 | satellite: Unterbrechen während der Antwort (Barge-in) | – | 2026-10-13 | 2026-10-13 | 0 | 🟢 | offen |
| 1401 | satellite: Sprechzug-Ende inhaltlich erkennen statt nach fester Stille | – | 2026-10-13 | 2026-10-13 | 0 | 🟢 | offen |
| 1402 | llm: LoRA-Adapter pro Anfrage auf dem bestehenden llama-server evaluieren | – | 2026-10-13 | 2026-10-13 | 0 | 🟢 | offen |
| 1446 | A single document grant lists the whole KB with the grant's level as KB permission | – | 2026-10-31 | 2026-10-31 | 0 | 🟢 | ✅ #1472 |
| 1447 | context.extracted_vars is written but never read | – | 2026-10-31 | 2026-10-31 | 0 | 🟢 | ✅ #1477 |
| 1448 | Hollow-compaction warning misses a renamed list path | – | 2026-10-31 | 2026-10-31 | 0 | 🟢 | ✅ #1474 |
| 1459 | chat_uploads records no uploader: a thread member cannot file their own attachment into their KB | – | 2026-11-01 | 2026-11-01 | 0 | 🟢 | ✅ #1473 |
| 1460 | Moving a document between knowledge bases does not update its atom owner or tier | – | 2026-11-01 | 2026-11-01 | 0 | 🟢 | ✅ #1471 |
| 1461 | Deleting a document leaves its kb_document atom (and its grants) behind | – | 2026-11-01 | 2026-11-01 | 0 | 🟢 | ✅ #1471 |
| 1462 | A write grantee on a shared KB can move a document into their own KB and then read it as owner | – | 2026-11-01 | 2026-11-01 | 0 | 🟢 | ✅ #1471 |
| 1463 | can_access_atom ignores KB shares for fact atoms while the SQL fact filter honours them | – | 2026-11-01 | 2026-11-01 | 0 | 🟢 | ✅ #1472 |
| 1469 | REST knowledge routes and retrieval decide document access by different rules | – | 2026-11-02 | 2026-11-02 | 0 | 🟢 | ✅ #1500 |
| 1485 | Chat: eine anderswo gelöschte Sitzung wird unter derselben session_id wiederbelebt | – | 2026-11-03 | 2026-11-03 | 0 | 🟢 | ✅ #1502 |
| 1499 | Admin merge-duplicates clusters entities across users and circle tiers | backend | 2026-11-06 | 2026-11-06 | 0 | 🟢 | ✅ #1518 |
| 1507 | A scan that finishes after its chat was deleted reaches nobody | – | 2026-11-08 | 2026-11-08 | 0 | 🟢 | offen |
| 1508 | Agent starts scan_document twice for one request | – | 2026-11-08 | 2026-11-08 | 1 | 🟢 | ✅ #1520 |
| 1509 | MCP self-heal reconnects search every tick while the upstream engines block it | – | 2026-11-08 | 2026-11-08 | 0 | 🟢 | ✅ #1519 |

---

## B1. Auf `main` erledigt, Issue noch offen (35)

Jede Zeile ist am Commit nachgelesen. Sammel-PRs (#1505, #1476, #1503, #1474, #1472, #1471) erledigen
mehrere Issues; der jeweilige Teil steht im Commit-Rumpf mit „Refs #<n>".

| Thema | Issues | Geliefert in |
|---|---|---|
| Wissensbasis-Zugriff (Freigaben, Verschieben, Löschen, Urheber) | #1446, #1459, #1460, #1461, #1462, #1463, #1469 | #1471, #1472, #1473, #1500 |
| KG-Bereinigung und Abgleicher | #1329, #1499 | #1486 + #1504, #1518 |
| PDF-Split | #1374 | #1375; Restfrage per Entscheidung 2026-11-03 nach #875 verlegt |
| Auth (Teilbefunde aus dem Audit #1116) | #1343, #1344, #1345 | #1484, #1501 (gebaut, dunkel), #1483 |
| Chat / Agent | #1381, #1447, #1485, #1508 | #1476 + #1502, #1477, #1502, #1520 |
| Sprachpfad / Satelliten | #1211, #1386, #1387, #1396, #1397 | #1514–#1516, #1476 + #1480, #1503, Inventar 2026-10-13 |
| Satelliten-OTA | #1210, #1279, #1280 | #1487, #1475 |
| Scanner | #1390 | #1479 |
| MCP-Gesundheit | #1509 | #1519 |
| Werkzeuge / Tests / Betrieb | #1359, #1382, #1399, #1448 | #1481, #1433, #1474 |
| Einstiegsaufgaben + Prüfschlange | #121, #122, #125, #1349 | #1505 |

## B2. Teilweise geliefert — auf den Rest zuschneiden statt schließen (5)

| # | Titel | Geliefert | Offen |
|---|---|---|---|
| 875 | KG bi-temporal edges | Stufe 1 Gültigkeitsintervall (#1376, 2026-10-07) | Belegnachweis je KG-Beitrag als Voraussetzung (Entscheidung 2026-11-03, aus #1374); weitere Stufen ohne Go |
| 1206 | Vision-Modell wird verdrängt | Option E: Vision über das multimodale Hauptmodell (#1207) | Zielbild Option A (eigene Karte) |
| 1215 | USB-Dokumentenscanner | Phasen 0–3, Auftragsmodell, Knopf-Scan (#1479), Zustellung | Papier-Ende-zu-Ende auf einer Live-Instanz |
| 1218 | Ingest-Zugangsdaten je Integration | Phasen 1, 2, 4 live | Phase 3 Selbstrotation (sieben Punkte, Issue-Kommentar 2026-09-18) |
| 1398 | Esszimmer: Inventar + Negativmaterial | im Ansible-Inventar seit 2026-11-06 (bare-metal) | Raumaufnahme: das Issue sagt „fehlt ganz", `docs/SATELLITE_ACOUSTIC_COMMISSIONING.md:353` sagt „yes (v3)" — Widerspruch, am Material zu klären |

## B3. Offen ohne Lieferung (29), nach Thema

| Gruppe | Issues |
|---|---|
| A. Sprachpfad / Satelliten | #21 Lautstärke nach Tageszeit · #871 EN/IT-Weckwort-Test (braucht Muttersprachler) · #1400 Barge-in · #1401 Sprechzug-Ende inhaltlich erkennen |
| B. Vision | #80 Gesichtserkennung (#12, die leere Dublette, ist geschlossen) |
| C. Wissen / KG / Ingest | #876 haushaltsweite Kanonikalisierung · #877 `external_id`-Linker · #1240 Kontaktpunkte (Design #1241) · #1314 dieselbe Organisation über Nutzergrenzen (Design #1320) · #1507 Scan-Ergebnis nach gelöschtem Chat (Entscheidung ausstehend) |
| E. Infrastruktur / Modelle | #81 horizontale Skalierung · #82 Grafana-Dashboards · #342 Plattform↔ha-glue-Extraktion · #1315 Ternary Bonsai 2 27B (hängt an llama.cpp) · #1402 LoRA-Adapter je Anfrage |
| F. Chat-UI / PWA | #79 Web Push · #84 Haptik · #124 Französisch · #263 Dashboard-Widgets |
| G. Assistenz-Fähigkeiten | #260 Routinen · #261 Einkaufslisten · #262 Automationen per Sprache · #264 Timer (Teil-Kandidat, s. u.) · #265 Skill-Marktplatz · #266 Musikstimmung · #267 Raum-Begrüßung · #268 Kochassistent · #270 Paketverfolgung (Kandidat, s. u.) |
| H. Ökosystem | #85 Client-Bibliotheken |

## Seit 2026-09-18 geschlossen (22)

#11, #12, #13, #23, #83, #127, #1113, #1116, #1209, #1269, #1277, #1284, #1360, #1367, #1368, #1372, #1373,
#1385, #1388, #1391, #1392, #1468. Darunter die vier dünnen Vision-Issues (#11/#12/#13/#23), der stärkste
Schließkandidat der letzten Erhebung (#83 Alerting) und das Sammel-Issue #1116, dessen Teilbefunde als
#1343–#1345 weiterliefen.

---

## Querverweise Issue ↔ Dokumentation (nur die nicht erledigten)

| # | Nennende Dateien |
|---|---|
| 80 | `TODOS.md`, `docs/private/feature-ideen.md` |
| 124 | `docs/private/VISIBILITY_PLAN.md` |
| 264, 270, 342, 1215 | `TODOS.md` |
| 875 | `TODOS.md`, `docs/ENVIRONMENT_VARIABLES.md`, `docs/design/kg-bitemporal-edges.md`, `docs/design/kg-contact-points.md`, `docs/design/kg-cross-user-canonicalization.md`, `docs/design/spikes/kg-coreference-vs-shared-ownership.md`, `.claude/rules/kg-memory.md`, `.claude/rules/pdf-split.md` |
| 876 | `TODOS.md`, `docs/design/structured-memory.md`, `docs/design/household-auth-on-cutover.md`, `docs/design/kg-cross-user-canonicalization.md`, `docs/design/spikes/kg-coreference-vs-shared-ownership.md`, `.claude/rules/kg-memory.md` |
| 877 | `TODOS.md`, `docs/design/kg-cross-user-canonicalization.md`, `docs/design/spikes/kg-coreference-vs-shared-ownership.md` |
| 1206 | `docs/ENVIRONMENT_VARIABLES.md` |
| 1218 | `TODOS.md`, `docs/design/ingest-credentials.md` |
| 1240 | `TODOS.md`, `docs/design/kg-contact-points.md` |
| 1314 | `docs/design/shared-organisation-entities.md` |

Ohne jeden Dokumentverweis: #21, #79, #81, #82, #84, #85, #260–#263, #265–#268, #871, #1315, #1398, #1400–#1402, #1507.

## Kandidaten, die nach Titel erledigt aussehen

Nur Kandidaten, nicht am Code verifiziert:

| # | Warum der Verdacht |
|---|---|
| 270 | Paketverfolgung läuft als Integration (`renfield-mcp-tracking`); offen wäre höchstens die proaktive Ankündigung bei Heimkehr. |
| 264 | Erinnerungen samt präsenzgesteuerter Zustellung existieren; offen wären benannte Timer und „welche Timer laufen?". |

---

# Teil C — `tasks/`

Erhebung **2026-11-09**. **48 Einträge** (43 `.md`, 1 `.html`, 1 `.wav`, 1 `.py`, 1 Unterordner, 1 `__pycache__`).
Gezählt wurden Kästchen `- [ ]` / `- [x]`.

| | 2026-11-09 | 2026-09-18 |
|---|---:|---:|
| Rohe unerledigte Kästchen | **394** | 493 |
| davon **überholt** — Arbeit geliefert, Kästchen nie abgehakt | **28** | 145 |
| davon fremdes Repo (`reva-plan.md`) | 23 | 23 |
| **echter Renfield-Rückstand** | **343** | 325 |
| erledigte Kästchen | 314 | 118 |

**Einschränkung:** Versioniert sind genau die drei vorgesehenen Dateien (`audit-findings-plan.md`,
`e11-react-query-handover.md`, `todo.md`); der Rest liegt per `.gitignore` bewusst lokal. Inhalte werden
hier **nicht wiedergegeben**, nur gezählt und eingeordnet.

Der Rückgang der überholten Kästchen (145 → 28) ist der Nachzug vom 2026-09-20: 14 Pläne wurden damals
auf den Lieferstand gebracht.

## Verteilung des echten Rückstands

| Datei | offen | Einordnung |
|---|---:|---|
| `mobile-receipt-capture-plan.md` | **227** | unverändert, Phase 0 offen |
| `chat-image-analysis-plan.md` | **55** | **neu** (2026-11-08): Bildanalyse im Web-Chat, entschieden, nichts gebaut, Schalter `CHAT_IMAGE_VISION_ENABLED` |
| `camera-vision-expansion-plan.md` | 25 | nie begonnen |
| `health-history-concept.md` | 8 | Gesundheits-Vertikale, nichts umgesetzt |
| `ingest-credentials-plan.md` | 7 | nur Phase 3 (Selbstrotation) — deckt sich mit #1218 |
| `scanner-ingest-plan.md` | 6 | Betriebsrest (Neustartprüfung, Weißabgleich, DHCP-Reservierung, Papier-E2E, Rollout auf eine weitere Instanz) |
| `scanner-measure-then-repair-plan.md` | 6 | Sensorabruf-Härtung im Scanner-Repo; Lage seit dem Abschalten des Knopfwächters offen |
| `scanner-device-ownership-plan.md` | 4 | Gerätemakler, Stufen 0–3 |
| `document-tier-control-plan.md` | 2 | Fristen-Popover, Omnisearch-Rücksprung |
| `satellite-iot-vlan-plan.md` | 2 | Arbeitszimmer/Gästezimmer (aus), alte Leases räumen |
| `e11-react-query-handover.md` | 1 | Pairing-Modals noch auf `apiClient` |

## Überholt (28)

- `satellite-iot-vlan-plan.md` — 18 von 20: VLAN 90 ist seit 2026-11-05 live, die Satelliten stehen auf `192.168.90.x`; die Datei hat nur ihren Kopf nachgezogen.
- `scanner-ingest-plan.md` — 4: die Knopf-Tore und die Abfrageschleife sind gebaut (`scan-document-button-plan.md` vollständig) und der Knopfwächter danach bewusst abgeschaltet.
- `satellite-turn-end-reason-plan.md` — 5: der Grund des Durchlauf-Endes wird benutzt (#1389, #1476).
- `orangepi-baremetal-plan.md` — 1: die Doku-Zeile ist in `CLAUDE.md` und der Satelliten-Regel nachgezogen.

## Posten, die NUR hier stehen

Weiterhin ohne Issue und ohne `TODOS.md`-Eintrag: Dokument-Tier-Steuerung (Rest), Kamera-/Vision-Erweiterung,
Gesundheits-Vertikale, Gerätemakler für den Scanner. **Neu:** die Bildanalyse im Web-Chat — sie steht als
Plan hier und als uncommitteter Eintrag in `TODOS.md`, aber in keinem Issue.

## `lessons.md`

Kein Rückstand, sondern gesammelte Lehren: **24 Einträge**, letzter 2026-11-06 (2026-09-18: vier, letzter
2026-09-12). Die Datei wird also wieder geführt; Kästchen trägt sie keine und zählt deshalb oben nicht mit.
