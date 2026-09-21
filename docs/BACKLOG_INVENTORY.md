# Rückstands-Inventar

**Erhebung: 2026-09-18.** Vollständige Auflistung aller offenen Posten aus beiden
Quellen — GitHub-Issues und Dokumentation. Diese Datei ist das **Nachschlagewerk**;
die *Rangfolge* steht in `TODOS.md` → „Priorisierte Gesamtsicht" und nennt bewusst
nur die Spitzenposten je Stufe. Wer wissen will, was insgesamt offen ist, liest hier.

Warum es beides gibt: Die Rangfolge beantwortet „was zuerst", das Inventar
beantwortet „was überhaupt". Eine Rangfolge für eine Aussage über den Gesamtumfang
zu halten, führt in die Irre — 25 genannte Spitzenposten gegenüber weit über 150
tatsächlichen.

## Umfang auf einen Blick

| Quelle | Umfang |
|---|---|
| Offene GitHub-Issues | **41** (28 davon seit über 90 Tagen unberührt) |
| `TODOS.md` — offene Abschnitte | **39** (27 in P2, 12 in P3) |
| `TODOS.md` — hervorgehobene Einzelposten | **119** |
| Design-Dokumente mit offenem Status | **24 von 35** |
| `CLAUDE.md` — zurückgestellt/dunkel | **12 Stellen** |
| Nie eingeschaltete Funktionsschalter | **33** |
| `TECHNICAL_DEBT.md` (Backend + Satellit) | **9 offene Posten** |

## Grenzen dieser Erhebung — ehrlich benannt

- **Stichtag.** Erhoben am Vormittag des 2026-09-18. Seither geschlossen: #1269
  (`check_output`, per #1270). Behoben, aber unten noch als offen geführt: das
  Browser-Mikrofon im Haushalt (verwaister Ingress, siehe `TODOS.md:56`).
- **Ein veralteter Stand im Doku-Teil.** Die Doku-Erhebung lief gegen eine
  Arbeitskopie, die drei Commits zurücklag. Folge: Der Vermerk „`docs/GPU_TOPOLOGY.md`
  existiert nicht" ist **falsch** — die Datei existiert seit #1265. Alle übrigen
  Dateien waren vorhanden, die Zahl der Design-Dokumente stimmte beidseitig.
- **`tasks/` ist nicht enthalten.** 31 Dateien, davon 5 versioniert; der Rest ist
  per `.gitignore` bewusst lokal. Eine eigene Erhebung läuft.
- **Vertraulichkeit.** Inhalte aus `docs/private/` werden hier nicht wiedergegeben.
  Verwiesen wird nur auf Dateinamen, wie an anderen Stellen im Repo auch.

---

# Teil A — Dokumentation

Reine Erfassung, keine Bewertung/Priorisierung. Fundstellen als `datei:zeile`.
Durchgestrichene (`~~…~~`) TODOS-Einträge sind **nicht** aufgenommen; sie sind am Ende separat gezählt.

Ausgewertete Quellen: `TODOS.md`, `docs/design/*.md` (37 Dokumente), `CLAUDE.md`,
`src/backend/utils/config.py` ↔ `k8s/configmap.yaml` ↔ `../x-ren/k8s/renfield-env.configmap.yaml`,
`docs/TECHNICAL_DEBT.md`, `src/satellite/TECHNICAL_DEBT.md`, übrige `docs/*.md`.

---

## 1. TODOS.md — 88 offene Einträge

### P0 — Active / blocking: 0 Einträge
`TODOS.md:32` — „(no active blockers — all prior P0 items resolved and merged)". Ausdrücklich leer.

### P1 — Next substantive batch (3 offen, 2 durchgestrichen)

| # | Eintrag | Inhalt | Trigger / Status | Fundstelle |
|---|---|---|---|---|
| 1 | #10 Presence / Media-Follow Raumwechsel-Latenz | RSSI-EWMA-Filter live zur Feldvalidierung; Watch-List F1–F5 (chattiger Nachbar erodiert Raum; Open-Plan erreicht Enter-Margin nicht; Voice-gesetzter Raum ohne BLE; Streuwert bei Erstzuweisung; fehlende Multi-Satelliten-Korroboration) | „LANDING ACTIVE for real-world validation"; Tuning rein über Env, kein Redeploy; größerer Wurf (mmWave+BLE-Fusion) zurückgestellt | `TODOS.md:41` |
| 2 | #6 Provisioning config drift | Restarbeit: Benszimmer-host_var-Enrollment-Token (offline), Inventory → mDNS + DHCP-Reservierungen (Router-seitig) | Benszimmer muss online sein; Ops, kein PR | `TODOS.md:42` |
| 3 | 🔐 Full security audit — login & user management | Dachthema über SSO-Cutover + JWT/Session-Schulden; Scope siehe P2-Block | Trigger: „sobald Notes + Projekt-Timeline (Business-Phase 4) fertig" (Operator-Wunsch 2026-07-20) | `TODOS.md:44` |

Zusätzlich im Kopf vermerkt, **bewusst nicht implementiert** (Tracker-Einträge, kein Shortcut):
`#1116` Login-Audit-Residuen, `#342` Plattform↔ha-glue-Extraktion (4 Wochen, Q3-2026 OSS),
`#876` Cross-User/Household-KG-Kanonikalisierung (blockiert auf named-circles v2),
`#875` bi-temporale `valid_at`/`invalid_at`-Kanten (braucht ausdrückliches Go + eigenes Review),
`#877` `kg_entities.external_id` Offline-Linker (braucht Wikidata/GND-Pipeline),
`#1121` F6 Redis-Auth/NetworkPolicies im Reva-Cluster (separate Infra/Repo) — `TODOS.md:14`.

### P2 — Scheduled follow-ups (62 offen)

**🎙️ Browser-Voice auf xidra** (Runbook `docs/design/browser-voice-auth-on-instances.md`)
| 4 | Sprechererkennung-Opt-in auf xidra (D4) | 4 Speaker-Flags auf xidra hart `false`; kontrolliertes Opt-in braucht **DSGVO-Assessment zuerst** (ECAPA = biometrisches Datum, Art. 9): Einwilligung, Zweckbindung, Löschpfad, kein Auto-Enrol von Gästen | „LATER — not now" | `TODOS.md:54` |
| 5 | Voice-Server-Härtung (D7) | (a) Access-Log schreibt `/ws/voice?token=…` mit — Token redigieren; (b) Per-Client-Origins in der Registry-Zeile, vom Voice-Server selbst erzwungen | nächstes Voice-Server-Release | `TODOS.md:55` |
| 6 | F7 — Haushalts-Browser-Voice vermutlich seit ~Juli kaputt | Ingress `renfield/voice-server` zeigt auf Service, den es seit dem Shared-Voice-Server-Cutover nicht mehr gibt (verifiziert 2026-09-15). Satelliten unbetroffen, nur Browser-Mikro | „record only, not fixed here" | `TODOS.md:56` |

**🧬 KB-Near-Duplicate (#1170) — Post-v2.24.0**
| 7 | Text-Signal-Präzision | Mean-Chunk-Embedding verwechselt „gleiches Layout/Vendor" mit „gleiches Dokument"; besser: Blocking-Key (Issuer/Datum/Seitenzahl) vor dem Cosinus oder schärfere Doc-Signatur | nur wenn 0.995 zu verrauscht (Reject-Rate auf `/brain/review` beobachten) | `TODOS.md:60` |
| 8 | `max_docs=2` Triplikat-Kompromiss | Ein Dokument in genau 3 Kopien wird nicht gefunden | „revisit if a real 3× duplicate is reported" | `TODOS.md:61` |
| 9 | `content_embedding`-Backfill auf neuen Instanzen | `bin/backfill_document_content_embeddings.py --commit` + Read-only-Schwellen-Sweep vor jedem Flag-Flip | beim Aktivieren auf einer weiteren Instanz | `TODOS.md:62` |
| 10 | PR2 — föderierte Dokumentsuche | über Paperless + (xidra) Simba; aus #1156 zurückgestellt | — | `TODOS.md:63` |

**🎯 Meeting-Transkriptions-QUALITÄT (Baseline 2026-07-22)**
| 11 | `meeting_whisper_model=large-v3-turbo` aktivieren | Gemessen −3.5 bis −5.3 WER-Punkte für ~1.2× Laufzeit; **reine Konfigänderung**, Empfehlung xidra zuerst | „ACTIVATION IS CONFIG-ONLY" | `TODOS.md:67` |
| 12 | Overlap-Deletions senken | (a) echtes Pro-Sprecher-Audio vom XVF3800, (b) diarisierungsgeführtes Pro-Turn-ASR, (c) Whisper-VAD/`no_speech`/`condition_on_previous_text`-Tuning | — | `TODOS.md:68` |
| 13 | cpWER als Produktmetrik-Gate halten | während Track-A-Diarisierungsänderungen | mit Track A | `TODOS.md:69` |
| 14 | Deutsche Meeting-Qualität ist UNGEMESSEN | AMI deckt nur EN ab; deutsches Referenzset (Tuda-De / Common-Voice-DE / konsentierte interne Aufnahme) nötig; Lesesprach-Floor 5.3 % ist kein Meeting-Wert | xidra hat EN+DE-Kunden | `TODOS.md:70` |

**🗂️ Meetings-UX**
| 15 | Track-D-Folgearbeit | Auto-Match / stärkere Confirm-Erinnerungen | eigene Tracks | `TODOS.md:78` |

**🔐 Security-Audit Login (durchgeführt 2026-07-21, Remediation gemerged)**
| 16 | M1 — `#access_token=`-Fragment-Handler | bestätigter Session-Fixation-Sink; Löschen erst nach SSO-Cutover möglich (lebender Konsument der Reva-OIDC-Rückleitung) | = SSO-Cutover | `TODOS.md:82` |
| 17 | M2-Frontend — Browser-WS holt kurzlebiges Token | macht Socket-Open asynchron → WS-Hook-Tests umbauen + Browser-E2E; `wsToken.ts` liegt vorbereitet | — | `TODOS.md:82` |
| 18 | Low/Info-Posten | Open-Registration-Default, unauth Taxonomie-/MCP-Enumeration auf `/api/roles/permissions/all` + `/api/auth/permissions`, username-basierte Lockout-DoS, WS-Origin-Prüfung | — | `TODOS.md:82` |
| 19 | ⚠️ MERGE/DEPLOY-GATE — Reva-(OIDC-)Login verifizieren | (a) ist Reva-OIDC auf xidra überhaupt an, (b) Browser-E2E der echten Reva-Anmeldung, (c) Reva-Repo-Check auf `create_access_token` | Operator-Aufgabe, nicht laufzeit-bestätigt | `TODOS.md:82` |
| 20 | M5 Last-Admin-Guards sind TOCTOU-anfällig | zwei gleichzeitige Demotions können 0 Admins hinterlassen; Fix = `pg_advisory_xact_lock` oder `SELECT … FOR UPDATE` | niedrige Wahrscheinlichkeit, wiederherstellbar | `TODOS.md:82` |
| 21 | Ungegatetes Agent-Paperless-Schreiben | `mcp.paperless.update_document` ist in der `documents`-Rolle ohne Permission-Gate sichtbar | Operator-Entscheid 2026-07-26: separierbare Härtung | `TODOS.md:88` |

**SSO-Token-Handoff — Cutover**
| 22 | Cutover in 3 Schritten | (1) jeden Emitter auf `?code=` migrieren (heute: Reva-OIDC-Callback, repo-übergreifend), (2) `SSO_HANDOFF_ENABLED` einschalten, (3) Fragment-Handler löschen. Zusätzlich un-verdrahtet: `GET /api/auth/sso/start` + „Sign in with…"-PKCE-Start (google/github/apple `enabled=False`) | bis dahin liegt der Fix inert | `TODOS.md:92` |
| 23 | BLOCKIERENDE offene Frage (§10.1) | Wo läuft der Reva-OIDC-Callback relativ zu Renfields Redis? Entscheidet, ob der Emitter direkt in den Code-Store schreibt oder über `…/sso/finish` | braucht Operator-Entscheid | `TODOS.md:93` |

**JWT/Cookie-Restschuld**
| 24 | `/api/ws/token`-Faucet + Bearer-Interceptor abschaffen | sobald Reva cookie-nativ ist | `TODOS.md:97` |
| 25 | Browser-Voice auf einer auth-on-Instanz freischalten | dann vollständiges Live-`scope:voice`-E2E | `TODOS.md:97` |

**Föderation**
| 26 | `remote_user_id`-Kollision im Tier-Membership-Arm | bei ≥2 Peers unter einem Owner: `MultipleResultsFound` bzw. falscher Peer-Grant; Fix = lokaler Surrogat-Identitätsraum pro Peer | bissig erst ab 2 Peers | `TODOS.md:100-103` |
| 27 | Personenbezogene Föderation (Cross-Instance-Identity-Mapping) | DESIGN fertig, Bau zurückgestellt: `federation_user_links`, signiertes `querier_ref`, Responder-Mapped/Fallback-Zweig | **PREREQ (P0): Personal-Instanz auf auth-on** | `TODOS.md:105-109` |

**Browser-Wakeword**
| 28 | Post-Deploy-Browser-E2E ausstehend | WASM-Mikro-Pfad ist nicht unit-verifizierbar — „Renfield" in de+en an einem echten Browser sagen | nach Frontend-Deploy | `TODOS.md:115` |

**Chat-UI-Modernisierung — Rest der Roadmap**
| 29 | (9) shared-private | setzt haushaltsgeteilte Unterhaltungen voraus, die es nicht gibt (T3) | — | `TODOS.md:146` |
| 30 | (10) Gen-UI-Restposten | Cover/Jalousien-Positionssteuerung; reichere Freiform-Widgets hängen an Lane B | — | `TODOS.md:145` |
| 31 | Lane B (Freiform-HTML/SVG im Sandbox-iframe) | bewusst zurückgestellt (YAGNI + eigenes Security-Review), `ARTIFACTS_HTML_SANDBOX_ENABLED` ist ein unverdrahteter Platzhalter | — | `TODOS.md:137`, `docs/design/chat-artifacts-sandbox.md:31,444,605` |
| 32 | Artefakt-Persistenz ist „last-frame-wins" pro `id` | ein künftiger *streamender* Produzent braucht serverseitiges Append | — | `TODOS.md:147` |
| 33 | PRÄMISSEN-VORBEHALT | Web-`/chat`-Anteil vs. Voice/Satellit wurde nie gemessen; gilt jetzt für die Entscheidung, ob (1) Branching und T3 den Aufwand wert sind | — | `TODOS.md:149` |

**Einzelthemen P2**
| 34 | BT-Scan: deterministische Geräteanzahl pro Raum | Backend soll Zählungen vorberechnen statt sie das kleine Modell addieren zu lassen (25 genannt, 18 aufgezählt) | kosmetisch; eigenständig, `BT_SCAN_ENABLED` | `TODOS.md:151-167` |
| 35 | Steuererklärungs-Sammler — schlanke P1-Version bauen | T1 Enum+Prompt, T2 Extractor+Migration+GIN, T3 Retrieval-Filter + No-Leak-Regressionstest, T4 Dossier-Prompt, T5 Flag `STEUER_ASSISTANT_ENABLED`; **keine Summenbildung** | „[P1-ready]" | `TODOS.md:169-188` |
| 36 | [P2] Docling+poppler-Union-Extractor | Docling verschluckt rechtsbündige Beträge → Summen könnten still zu niedrig sein; blockiert vertrauenswürdige Summen | hängt an poppler/pymupdf im Backend-Image | `TODOS.md:189` |
| 37 | [P2] §35a Zahlungsjahr-Präzision (Zuflussprinzip) | — | `TODOS.md:190` |
| 38 | [P2] Kategorie-Override über Re-Extraktion retten | `documents.tax_categories` wird bei Re-Ingest neu erzeugt; Muster: `tier_overridden`-Sticky-Bit | wenn manuelle Korrektur eingebaut wird | `TODOS.md:191` |
| 39 | [P2] Deterministisches `/api/steuer/{year}` + Steuer-Linse | — | `TODOS.md:192` |
| 40 | [P2] Pro-Kategorie-Abzugsbetrag-Extraktion | §35a Lohnanteil, Kassen-Erstattung netto | — | `TODOS.md:193` |
| 41 | [P2] Gespeicherter Situations-Fragebogen | — | `TODOS.md:194` |
| 42 | [P3] CSV/PDF-Dossier-Export + ELSTER/ERiC-Feld-Mapping | — | `TODOS.md:195` |
| 43 | Fristen-Kalender-Sync in PRODUKTION einschalten | Code + Scheduled Task da, aber auf **jeder** Instanz aus — Calendar-MCP ist nicht im Pod verdrahtet; Checkliste in `docs/OBLIGATION_CALENDAR_SYNC.md` | braucht echte Kalender-Credentials vom Operator | `TODOS.md:197-199` |
| 44 | Paperless PR4: Multi-Replica `pg_try_advisory_lock` | heutiges `asyncio.Lock` deckt nur einen Prozess | >1 Backend-Replica | `TODOS.md:203` |
| 45 | Paperless PR4: Multi-User-Attribution in ui_sweep-Zeilen | braucht MCP-`owner`-Exposition + Paperless↔Renfield-User-Mapping | — | `TODOS.md:204` |
| 46 | Paperless PR 4b: No-Re-Edit-Filter (`superseded`) | Spalte existiert bereits | „wenn ui_sweep-Rauschen real auftritt" | `TODOS.md:207-210` |
| 47 | Satellit: Audio-Preprocessing (Noise Reduction) aufs Backend | Hoch; Alternative XVF3800-Hardware-AEC | — | `TODOS.md:214`, `src/satellite/TECHNICAL_DEBT.md` |
| 48 | Satellit: Echo Cancellation | Mittel (WebRTC-APM oder XVF3800) | — | `TODOS.md:215` |
| 49 | Satellit: 4-Mikrofon-Beamforming; eigenes Wakeword-Training | Niedrig | — | `TODOS.md:216` |
| 50 | Satellit: Multi-Core/GIL | ~2.5 Kerne ungenutzt; RAM ist der Blocker, IPC auf dem Echtzeitpfad | erst bei schwererer On-Device-Last **und** mehr RAM | `TODOS.md:217` |
| 51 | Presence-Nacharbeiten | (a) Esszimmer↔Arbeitszimmer-Grenze nach Rückbau der Testposition erneut messen, ggf. Margin auf 5–6; (b) `feat/presence-debug-sightings` nach Review mergen | — | `TODOS.md:238` |
| 52 | [P3] 55" Interactive Signage Flip spielt kein TTS | eigener Quirk (404 in `_confirm_playback_started`) | separat zu untersuchen | `TODOS.md:251` |
| 53 | [P3] dlna-mcp Default-Audio-MIME `audio/flac` | falsch für Nicht-FLAC; andere Caller (Jellyfin) treffen den Default | — | `TODOS.md:253` |
| 54 | [Tech debt] TTS an Renderer bewusst unverschlüsselt über http | dokumentierter Security/Privacy-Downgrade, damit er nicht später als Versehen „gefixt" wird | Trigger: LAN nicht mehr vertrauenswürdig / Auslieferung verlässt das Netz | `TODOS.md:254` |
| 55 | Browser-TTS-Barge-in schneidet nach Satz 1 ab | betrifft nur den Browser-Wiedergabepfad; Richtungen: Mikro dämpfen, höhere Barge-in-Schwelle, oder ein einzelner voller Clip statt Satz-Chunks | — | `TODOS.md:256-257` |
| 56 | Explizites „mobile/never-route"-Flag pro Gerät | heute aus IP-Raum-Registrierung abgeleitet | wenn ein beweglich registriertes Gerät auftaucht | `TODOS.md:259-260` |
| 57 | Tool-Health-Charts | Trendlinien/Heatmap | ≥30 Tage `tool_outcome_stats`; **gemessen 2026-07-05: 3 Zeilen, Trigger ungefeuert** | `TODOS.md:271,277` |
| 58 | Trajektorien-v1/v2-Diff-Ansicht | `/admin/memory-v2-shadow` | gated bis `memory_extraction_v2_authoritative=True`; **Gate unerfüllt** | `TODOS.md:272,277` |
| 59 | Bulk-Approve/Multi-Select in der Skills-Inbox | | ≥2 Wochen Burn-in; **gemessen: 5 Skills, 0 Drafts** | `TODOS.md:273,277` |
| 60 | Playwright `--host-resolver-rules`-Config für CI | Chromium ignoriert `/etc/hosts` für `renfield.local`; Schema unbestätigt | CI ist bewusst funktionslos → geringer Wert | `TODOS.md:274,277`, `docs/design/self-learning-admin-console.md:188` |
| 61 | Partial-Indexe für `procedural_skills.status` | B-Tree über 4-Werte-Spalte; ~1 h Arbeit | ~30 Tage Burn-in; **Prämisse fehlt (5 Zeilen)** | `TODOS.md:275,277`, `docs/TECHNICAL_DEBT.md:162-175` |
| 62 | Wurzelbefund: Self-Learning ist datenhungrig, weil die Sprecher→User-Identität ungenutzt ist | 38 Sprecher, alle „Unbekannt", nur 1 von 3 Usern verknüpft — **operative** Aufgabe, kein Bau | FK-Bug ist behoben | `TODOS.md:277` |
| 63 | Speaker Phase 0.5 | `audio_duration_s` auf den Chat-WS-Frame fädeln, damit das Dauer-Gate auch den PRIMÄR-Pfad abdeckt | — | `TODOS.md:282` |
| 64 | Speaker operativ | 3 Haushaltsmitglieder über `/speakers` einlernen, danach `bin/purge_unknown_speakers.py --commit` (41 verschmutzte Profile) | Deploy Phase 1+2 zuerst | `TODOS.md:285` |
| 65 | Speaker Phase-3-Flip (blockiert) | einlernen ≥2, `bin/calibrate_speaker_threshold.py`, Schwelle+Margin setzen, dann Flag; **kein Code offen** | prod hat heute 1 enrolltes Profil | `TODOS.md:288` |
| 66 | Speaker Phase 4 (optional) | Far-Field-Adaption, AS-Norm, Upstream-Capture | wenn Separation marginal bleibt | `TODOS.md:289` |
| 67 | OCR-Engine-Evaluation/-Wechsel | Harness existiert, Evaluation nie gelaufen; Prämisse aktualisiert (schon docling, nicht easyocr) → als „docling vs tesseract/cloud" neu fassen | skalengetrieben: ≥ hunderte gemeldete Qualitätsfehler | `TODOS.md:296`, `docs/design/ocr-engine-eval.md:3` |
| 68 | `/brain` zeigt Fakt und Quell-Chunk doppelt | bewusst „akzeptieren + beobachten" | wenn es sich verrauscht anfühlt | `TODOS.md:312` |
| 69 | pg_trgm-Index für Identifier-Suche | ILIKE ist unindiziert (Leading Wildcard) | bei messbarer Latenz auf großem Korpus | `TODOS.md:313` |
| 70 | Billiger Vorfilter vor dem LLM-Fristen-Pass | heute ein LLM-Call pro Dokument | wenn Ingest-Latenz/Queue-Tiefe messbar wird | `TODOS.md:314` |
| 71 | Echter Cross-Encoder-Reranker | `RAG_RERANK_ENABLED=false`, weil das Default-Modell nicht in der Ollama-Registry ist **und** `_rerank` ein Bi-Encoder-Cosinus ist → Reranking lief nie | wenn Top-K-Ordnung messbar schmerzt | `TODOS.md:317`, `k8s/configmap.yaml:273` |
| 72 | Erkannte Sprache auf `documents` speichern | statt sie überall neu zu detektieren; Sprachsatz ggf. erweitern | — | `TODOS.md:318` |
| 73 | KG-Provenienz pro Dokument (Referenzzählung) | die naive „delete by source_ref"-Lösung ist unsicher; echte Restposten: `mention_count`-Inflation + veraltete Relationen | „re-file as such if it ever matters" | `TODOS.md:319` |
| 74 | Fristen-Kalender: Duplikat-Fenster ohne Idempotenz-Key | Crash zwischen `create_event` und Ledger-Commit; Events sind terminiert statt ganztägig | P2 | `TODOS.md:309` |
| 75 | Output-Provider: Marken-Shims + Discovery-Methoden entfernen | blockiert, weil die agentenseitige Medien-Auflösungs-Migration (§5) nie gebaut wurde — Shims tragen Jellyfin/DLNA-Content-Orchestrierung; danach `agent_roles.yaml` (ConfigMap!) anpassen | — | `TODOS.md:321-332`, `docs/design/output-providers.md:310` |
| 76 | `/design-consultation` laufen lassen, um DESIGN.md zu formalisieren | Eintrag steht unverändert im Backlog (eine `DESIGN.md` wird inzwischen in `CLAUDE.md` als Source of Truth geführt — Abgleich nötig) | „BEFORE next major frontend surface" | `TODOS.md:336-362` |
| 77 | `docs/STRATEGY.md` fertigschreiben | Skelett steht, **9 `[FOUNDER FILL-IN]`-Platzhalter** offen (strategische Überzeugung, 5-Jahres-Ideal, Invalidierungsschwellen) | nur vom Gründer beantwortbar | `TODOS.md:364-393` |

### P3 — Conditional / on signal (14 offen)

| # | Eintrag | Inhalt | Trigger | Fundstelle |
|---|---|---|---|---|
| 78 | GUI-Contribution-Model | Contribution-Registry (deklarative Slots) als Default für Feature-UI; Micro-Frontends verworfen. **Vorbedingung:** Off-Token-Drift beheben (`ChartArtifact`-Hex, `DeviceControl` `amber-500`, `ArtifactShell` ohne `.card`) | nächstes Feature, das UI in 5+ Shell-Dateien verdrahten müsste | `TODOS.md:399-400` |
| 79 | Satelliten-Meeting-Aufnahme | „Renfield, starte Meeting-Aufnahme" → Upload-Pfad; neue WS-Nachrichten, Pi-Storage/Streaming, LED-Aufnahmeanzeige, Consent-UX (rechtlich tragend) | §2 gebaut + Spike zeigt XVF3800-Audio ist diarisierbar | `TODOS.md:422-441` |
| 80 | `would_have_injected`-Shadow-Log entfernen | reine Instrumentierungs-Rücknahme | Owner prüft die Metrik ≥14 Tage nach v2.10 | `TODOS.md:461-468` |
| 81 | Haushalts-Kaskadenabstimmung für geteilte Skills | B kann A's household-Tier-Draft nicht freigeben | ≥10 household-Tier-Auto-Skills ODER explizites Nutzerfeedback | `TODOS.md:470-477` |
| 82 | ~~Agenten-String-Pfad auf den fusionierten Pfad heben~~ | **AUFGELÖST 2026-09-18.** Der Eintrag war sachlich falsch, nicht nur veraltet: `kg_retrieval.py:391,:407` ruft `expand_fused` innerhalb von `get_relevant_context`. Im Index gestrichen. (Die hier genannte Zeile `:488` war zudem verschoben — der Eintrag stand bei `:559`.) | — | erledigt |
| 83 | Subsume-Recall-Verlust (Phase 3) | verbleibender Residualfall: ein Turn mit zwei Fakten zum gleichen Subjekt verliert den Zustands-Fakt; echte Pro-Fakt-Lösung nicht gebaut | Flag ist aktiv (`MEMORY_SUBSUME_TO_KG=true` im Haushalt) | `TODOS.md:491-504` |
| 84 | „Nur-Single-User"-Vorbehalt für Subsume | namensbasiertes Capture, Cross-User-Subjektauflösung + Tier-Reichweite ungelöst | vor jeder Multi-User-Nutzung | `TODOS.md:504` |
| 85 | Person-OCR-Varianten-Dedup ist rein review-gegated | Reconciler-Same-Name-Gate erkennt keine abweichenden *Schreibweisen* | Duplikatspersonen aus Dokumentextraktion beobachten | `TODOS.md:526-528` |
| 86 | Paperless-kNN-Tier (Pre-LLM-Voter) | | **Gate: alle drei** — v1 3+ Monate live mit 200+ Dokumenten · Stage-1-LLM-Latenz ist p50-Engpass (>5 s) · Korrekturrate niedrig genug | `TODOS.md:536-539` |
| 87 | v2.5 KG-Retrieval-Upgrade | KG-1 Multi-Hop (~3 W), KG-2 Edge-Type-Ranking (~2 W), KG-3 Community-Detection+Summaries (~6-8 W), KG-4 Inverse/Transitiv (~2 W), KG-5 strukturelle Query-Primitive (~3-4 W); MVP KG-1+2+4 ≈ 6-7 Wochen; entparkt `docs/RAG_PARITY_PLAN.md` | v2-Föderation muss zuerst liefern | `TODOS.md:541-573` |
| 88 | `itsm`-MCP auf Build-Host reaktivieren | ConfigMap-Flip + Rollout-Restart | USU-Kundenservice wieder erreichbar | `TODOS.md:613-619` |
| 89 | Auto-Archiv-Politik für die Brain-Review-Queue | v1 archiviert nie; wahrscheinliche v1.5-Antwort: 14 Tage + Queue-Health-Indikator | 4-8 Wochen v1-Nutzungssignal | `TODOS.md:621-644` |
| 90 | In-Memory-Cache für Sprecher-Centroide + Wakeword-Templates | „measure first" | A1-Wakeword-Verifikation muss zuerst liefern + messbare Kosten | `TODOS.md:648-658` |
| 91 | Mobile-Receipt-Capture: 6 zurückgestellte Posten | E6 Apple-Custom-Distribution vs TestFlight · E7 iOS-Share-Sheet-Extension · E8 Zero-Install-Fallback-Doku · E9 Batch-Capture-Session · Split-ZIP-Export · App-Icon/Branding | M2 bzw. Phase 1.5a ausgeliefert | `TODOS.md:662-676` |

*(Nicht als Arbeitspaket gezählt: der reine Info-Eintrag „Meetily" `TODOS.md:402-420`.)*

**Als erledigt durchgestrichen (nicht in der Liste): 21 Einträge** — u. a. Broadcast-Announcement (`:38`), #12 KB-Maintenance (`:40`, doppelt auch `:120`), Graph-Expansion-Relation-Tier-Filter (`:111`), Browser-Wakeword-Mehrfach-Keywords (`:114`), Deploy-Skript-Quoting (`:117`), JWT-HttpOnly-Cookie (`:96`), C1 Opus (`:219`), Reconciler-Race-Härtung (`:479`), Phase-4-Graph-Expansion (`:485`), Paperless PR5 (`:533`), MCPManager-Streaming (`:575`), Notes-Design-Doc (`:584`), OCR-Cleanup-Skript (`:294`), Low-Quality-OCR-Admin-UX (`:295`), Schicht-A-Leseschicht/Notifier/GUI (`:301-311`), `amount_currency` (`:316`), `lang`-Plumbing (`:318`), KG-Hook-Akkumulation (`:319`, als obsolet geschlossen), i18n-Follow-up (`:265`), E1-E18-Audit-Posten (`:264-266`).

---

## 2. docs/design/*.md — 37 Dokumente, davon 24 mit offenem Status

### Beschlossen, aber NICHT gebaut (Design fertig, kein Code)
| Dokument | Statuszeile | Offene Stufen / Fragen |
|---|---|---|
| `kg-bitemporal-edges.md:4` | „Review-Entscheidungen getroffen 2026-09-13 (§12) — **kein Go zum Bauen**; Stufe 1 braucht ein ausdrückliches Go" | Stufe 3 „bewusst zurückgestellt" bis Stufe 2 ansteht (`:357`, `:388`) |
| `kg-contact-points.md:4` | „**kein Go zum Bauen**; Stufe 1 braucht ein ausdrückliches Go" | Standardregion kommt aus der Instanzkonfiguration (`:125`) |
| `kg-cross-user-canonicalization.md:3` | „DESIGN DRAFT — build REDIRECTED by eng review (2026-09-02)"; Structured-Memory-Phase-5-Posten | Risiken R1-R3 + offene Fragen O-1 (Person in zwei Haushalten) `:449,:474` |
| `federation-identity-mapping.md:3` | „DESIGN — not implemented. Spike/design only, no product code yet." | 4 offene Fragen `:120-126`; ephemerer Identity-Key + toter Peer nach Pairing `:131-134`; Pair-Time-Erreichbarkeitsprobe P3 `:174`; „NOT in scope (deferred)" `:176` |
| `gui-contribution-model.md:3` | „Design / proposal (not yet built)" | 3 offene Fragen (Registrierungsmechanismus, Read-only-Vokabular, Contract-Version-Policy) `:196-201` |
| `non-verbal-communication.md:3` | „PROPOSED / DESIGN — not implemented, not scheduled" | Head B (Körpersprache) zurückgestellt `:468,:482,:491`; MediaPipe-NPU-Port zurückgestellt `:516-519`; Risiken/offene Fragen `:273-287` |
| `satellite-audio-combine-pipeline.md:3` | „DESIGN (2026-07-07). Nothing built." | 3 offene Fragen, davon 1 gelöst `:148-153` |
| `voice-identity-wakeword-verification.md:3` | „DESIGN, REVIEWED — 13 decisions locked. **Nothing built.**" | A1-Falsifikationspfad → Reroute auf C2/C3 + SUPERSEDED-partial-Banner `:39` |
| `mobile-receipt-capture.md:3` | „DESIGN, revision 7 … **Nothing implemented.** Phase 0 offen" | Web-Capture (Option c) zurückgestellt R6-1 `:394,:506,:2504`; Paperless-Custom-Fields sind Neuarbeit `:2236`; offener Rest: Steuerberater-Checkliste + Phase-0-Checks |
| `atoms-granularity.md:3` | „Entwurf, pending Entscheidung" | Offene Fragen `:351-359`, davon eine ungelöst: **kein HNSW/IVFFlat auf `document_chunks.embedding`** (`:357`, „separates Issue") |
| `chat-ui-modernization.md:3` | „Design / backlog — survey + prioritized roadmap, not yet scheduled" | Tier-Fortschritt siehe TODOS-Ledger |
| `self-learning-admin-console.md:3` | „scoped, not started" (historisch; Konsole ist seither ausgeliefert) | Charts zu P2 verschoben `:128`; Playwright-DNS-Untersuchung `:188`; „NOT in scope (explicitly deferred)" `:207` |
| `user-events-ws.md:3` | „Design (approval-gated, not yet built). 2026-08-31" — **veraltet**, laut `CLAUDE.md:169` ausgeliefert | v1 nur Dokumente; `obligations_changed`/`notes_changed` zurückgestellt `:147` |
| `sso-token-handoff-hardening.md:3` | „DESIGN — not yet implemented" — **veraltet** (Empfänger ist ausgeliefert, dark) | 3 offene Fragen `:240-250`, darunter die blockierende §10.1 |
| `ingest-credentials.md:3` | „DESIGN (2026-09-08). Not implemented." — **veraltet** laut Memory (Ph1/2/4 live, Ph3 Selbstrotation offen) | — |

### Teilweise gebaut / mit zurückgestellten Stufen
| Dokument | Statuszeile | Offen |
|---|---|---|
| `scanner-ingest.md:3` | „Phases 0–3 BUILT (2026-09-08) … **not yet verified end-to-end against a live instance. Phases 4–5 open.**" | E2E-Verifikation + Phasen 4-5; Flag `SCANNER_INGEST_ENABLED` dark |
| `meeting-kg-and-speaker-identity.md:3` | „APPROVED v2 … Phase 0 in progress" | Track A Schritt (1) enrolled-speaker Auto-Match zurückgestellt (`meeting_auto_match_enabled`) `:65`; 5 offene Fragen/Risiken `:106-112`; Tracks B/C/D dark |
| `meeting-transcription.md:3` | „REVIEWED, spike-gated" | Auto-Match „not built" `:102`; >2 h-Meetings „not built for a weekly workload" `:121` |
| `speaker-enrollment-redesign.md:3` | „Phasen 0–3 BUILT (dark by default). **Phase 4 (Upstream-Capture/XVF3800) still research.**" | Software-Referenz-AEC `:122` und Target-Speaker-Extraction `:124` zurückgestellt; Review-Bucket war „deferred" `:83`, per `:134` wieder in Scope |
| `ble-presence-improvement.md:64` | „SHIPPED + DEPLOYED" | Phase 1b AdvertisementMonitor `:55`, Phase 1c Backend-RSSI-Smoothing `:60`, Phase 3 optionale Reichweite `:147` — alle DEFERRED; 3 offene Fragen `:161-164` |
| `chat-artifacts-sandbox.md:3` | „Design / decision-lock" (Lane A ausgeliefert) | Lane B zurückgestellt `:31,:170,:341,:444,:605,:699`; offene Fragen §7 `:488` sind per §11 `:569` beantwortet |
| `output-providers.md:3` | „Reviewed — decisions locked, ready to implement" | „Still-open questions (advisory)" `:272`; destruktives `DROP COLUMN` + Shim-Entfernung auf Folge-PR verschoben `:310` |
| `paperless-llm-metadata.md:3` | „Design proposal, revision 3.2 — implementation in progress" | kNN-Tier zurückgestellt `:97,:1095`; offene Fragen `:985`; Cache-mit-Pubsub-Invalidierung als „deferred note" `:1005`; PR-4b-`superseded` `:874` |
| `ocr-engine-eval.md:3` | „HARNESS SHIPPED, **evaluation not yet run**" | die eigentliche Messung |
| `mcp-self-detection.md:3` | „Phase 1+2+3+4 SHIPPED" | „Not built: a transparent rate-limit…" `:340`; ein weiterer „not built"-Posten `:389` (telemetrie-ausgeschlossen) |
| `a733-satellite-display.md:3` | „software port DONE, **hardware not yet attached**" | Hardware-Anschluss |
| `browser-voice-auth-on-instances.md:3` | „code + infra prepared, **`FEATURE_VOICE` NOT flipped**" (2026-09-15) | Flag-Flip + Runbook-Reihenfolge |
| `pdf-split.md:270` | PR1-PR3 gebaut | „parent card: deferred polish" |
| `notes-atom.md:158` | Design locked, Notes ausgeliefert | zurückgestellt: eigener `note`-Zweig im Wissen-Detail-Drawer |
| `command-center.md:3` | „**SUPERSEDED** (decommissioned 2026-07)" | offene Fragen `:256-270` sind mit der Stilllegung gegenstandslos |

### Ohne offene Punkte
`a733-satellite-camera.md` (live), `broadcast-announcement.md` (implementiert), `chat-branching.md` (dark-Flag, Phase 1+2 ausgeliefert), `meeting-minutes.md` (ausgeliefert, dark), `scheduled-tasks.md` (alle Phasen implementiert).

---

## 3. CLAUDE.md — 12 Stellen mit „deferred / dark / nicht verdrahtet"

> Die Zeilenangaben `CLAUDE.md:<n>` in diesem Dokument beziehen sich auf den Stand VOR der Aufteilung vom 2026-09-20
> (`git show f8a33cc6:CLAUDE.md`). Die Inhalte liegen seither in `.claude/rules/*.md` und `docs/design/*.md`.

| Punkt | Fundstelle |
|---|---|
| Lane B (Freiform-HTML/SVG im Sandbox-iframe) **deferred**, `ARTIFACTS_HTML_SANDBOX_ENABLED` Platzhalter, „not wired"; braucht die erzwingende Baseline-CSP in `nginx.conf` | `CLAUDE.md:75` |
| Kiosk: `peer_status_changed`-Delta zurückgestellt (Föderations-Peers behalten bis dahin einen Wall-Clock-Frische-Backstop) | `CLAUDE.md:75` |
| Command Center „slated for decommission" (inzwischen stillgelegt — Textrest) | `CLAUDE.md:75` |
| Graph-Expansion-Follow-up: Agenten-String-Pfad `get_relevant_context` auf den fusionierten Pfad (→ TODOS) | `CLAUDE.md:75` (Abschnitt Structured Memory) |
| Meeting-Auto-Match **DEFERRED**, `meeting_auto_match_enabled` dark (Spike-Trennungs-Gate „insufficient data") | `CLAUDE.md:81` |
| Meeting-Minutes „dark by default", auf dem Haushalt weiterhin `false` | `CLAUDE.md:83` |
| §2 KG+Speaker-Redesign: Tracks A/B/C „phased and dark" | `CLAUDE.md:85` |
| SSO-Handoff dark; Legacy-Fragment-Handler bleibt bis zum Reva-Cutover | `CLAUDE.md:211`, `:223` |
| JWT-Cookie: zurückgestellt = SSO-Fragment-Entfernung/`?code=`-Emitter, `/api/ws/token`-Faucet + Bearer-Interceptor, Voice-WS-Migration; **nicht angefasst:** `SECRET_KEY` (dreifach genutzt, kein Split/keine Rotation) | `CLAUDE.md:227`, `:300` |
| Notes: `note`-Zweig im Wissen-Drawer zurückgestellt (generischer Fallback greift) | `CLAUDE.md:326` |
| Dokumentsuche: **PR2 (deferred)** — föderierte Dokumentsuche über Paperless | `CLAUDE.md:338` |
| Memory→KG-Bridge „opt-in/dark by default" (Flag ist im Haushalt inzwischen an) | `CLAUDE.md:366` |
| OTA-Signatur-Rollout: Flotten-Re-Provisionierung fehlt, dann `require_signature` fail-closed schalten | `CLAUDE.md` §„Signed OTA packages" (Rollout-Status 2026-08-22) |

---

## 4. Dunkel geschaltete Funktionsschalter

**Methode:** `src/backend/utils/config.py` → alle `bool = False`-Vorgaben (**100 Flags**), abgeglichen gegen
`k8s/configmap.yaml` (Haushalt, 77 auf `"true"`) und `../x-ren/k8s/renfield-env.configmap.yaml` (xidra, 62 auf `"true"`).
Grenzen der Methode: Secrets/`.env`-Overrides und per `kubectl` gesetzte Werte sind nicht erfasst; xidra-Manifeste
liegen außerhalb dieses Repos.

### 4a. Dunkel im Haushalt, aber auf xidra AN (5)
`AUTH_ENABLED`, `AUTH_COOKIE_ENABLED`, `FOLDER_INGEST_SIMBA_ENABLED`, `MEETING_MINUTES_ENABLED`, `PROJECTS_ENABLED`
— d. h. Haushalt läuft auth-off, ohne Cookie-Session, ohne Simba, ohne Protokoll-Panel, ohne Projekte.

### 4b. Nie eingeschaltet — auf KEINER Instanz (33)
Das ist die Liste fertiger, aber nie aktivierter Funktionen.

| Flag | config.py | Kurz (laut Doku) |
|---|---|---|
| `ARTIFACTS_HTML_SANDBOX_ENABLED` | `:754` | Lane-B-Platzhalter, bewusst unverdrahtet |
| `CALENDAR_ENABLED` | `:168` | Calendar-MCP — Vorbedingung des Fristen-Kalender-Syncs |
| `OBLIGATION_CALENDAR_SYNC_ENABLED` | `:1829` | Fristen→Kalender-Reconciler, blockiert auf Credentials |
| `CARD_EMIT_INLINE` | `:376` | Inline-Card-Emission |
| `FEDERATION_IDENTITY_LINKS_ENABLED` | `:1292` | Cross-Instance-Personen-Mapping (F-ID-1, dark gemergt) |
| `FEDERATION_PENDING_USE_REDIS` | `:1283` | Pending-Federation über Redis |
| `LDAP_AUTH_ENABLED` | `:1477` | LDAP-Provider (authn-only) |
| `OAUTH_GOOGLE_ENABLED` / `OAUTH_GITHUB_ENABLED` / `OAUTH_APPLE_ENABLED` | `:1488/:1493/:1498` | Redirect-Provider, „enabling is config-only" |
| `REQUIRE_EMAIL_VERIFICATION` | `:1453` | Kommentar im Code: „Not implemented yet" |
| `SSO_HANDOFF_ENABLED` | `:1445` | PKCE-Empfänger, wartet auf den Reva-Emitter |
| `VOICE_AUTH_ENABLED` | `:1506` | Voice-Authentifizierung |
| `WS_REQUIRE_SCOPED_QUERY_TOKEN` | `:1543` | erzwingt `scope:ws`-Tokens |
| `WAKE_WORD_ENABLED` | `:256` | Browser-Wakeword (Opt-in) |
| `MCP_HEALTH_RATE_LIMIT_SIGNAL_ENABLED` | `:1771` | 429-Signal für die MCP-Gesundheit (Phase 3) |
| `MCP_RATE_LIMIT_BACKOFF_ENABLED` | `:1779` | eigenes Backoff gegen 60/min-MCPs |
| `MEETING_AUTO_MATCH_ENABLED` | `:715` | „DEFERRED/dark (noch nicht gebaut)" — `docs/ENVIRONMENT_VARIABLES.md:1559` |
| `MEETING_FINGERPRINTS_ENABLED` | `:726` | Track-A-Increment-1 (cross-meeting anonyme Fingerprints), ausgeliefert dark |
| `MEETING_FINGERPRINT_AUTONAME` | `:739` | automatische Benennung |
| `MEETING_KEEP_AUDIO` | `:742` | Audio nach Abschluss behalten |
| `MEMORY_CONTRADICTION_RESOLUTION` | `:795` | LLM-basierte Widerspruchsauflösung |
| `MEMORY_EXTRACTION_V2_AUTHORITATIVE` | `:801` | Phase-B-Flip; blockiert die Trajektorien-Diff-Ansicht |
| `NOTIFICATION_POLLER_ENABLED` | `:1684` | MCP-Notification-Polling |
| `PAPERLESS_INDEX_HEAL_ENABLED` | `:1187` | explizit `"false"` in beiden ConfigMaps (`k8s/configmap.yaml:416`) |
| `PAPERLESS_INDEX_HEAL_ALLOW_WORKFLOWS` | `:1205` | dito, zweite Stufe |
| `PROACTIVE_ENRICHMENT_ENABLED` | `:1675` | Anreicherung proaktiver Meldungen |
| `PROACTIVE_FEEDBACK_LEARNING_ENABLED` | `:1677` | Feedback-Lernen |
| `PROACTIVE_URGENCY_AUTO_ENABLED` | `:1674` | automatische Dringlichkeit |
| `RAG_FORCE_OCR` | `:537` | Voll-OCR erzwingen (bewusst aus) |
| `SPEAKER_INPROCESS_EMBEDDINGS_ENABLED` | `:210` | In-Process-Embeddings |
| `SPEAKER_QUALITY_GATING_ENABLED` | `:221` | Phase 0 der Sprecher-Neuausrichtung, dark ausgeliefert |
| `TRAJECTORY_REDACT_PII` | `:862` | Phase 4: PII-Scrubbing in `redacted_payload` |

Zusätzlich **aktiv abgeschaltet** (Default true bzw. bewusst `"false"` gesetzt):
`RAG_RERANK_ENABLED` (`k8s/configmap.yaml:273` — Reranking lief nie, s. Punkt 71),
`SAMSUNG_MCP_ENABLED` (`:309`), `LLM_OPENAI_FOR_INTENT` (`:187`),
und auf xidra: `DLNA/HA/N8N/JELLYFIN/RADIO_MCP`, `PRESENCE_ENABLED`, `MEDIA_FOLLOW_ENABLED`,
`FEATURE_VOICE/CAMERAS/SATELLITES/SMART_HOME`, die 4 Speaker-Flags, `ALLOW_REGISTRATION`
(`../x-ren/k8s/renfield-env.configmap.yaml:23,36,46-49,59,63,96,107,133,137,172-175`).

---

## 5. Sonstige Dokumente

### `docs/TECHNICAL_DEBT.md` — 4 offene Posten (Stand 2026-07-23; Übersicht nennt 25 offen / 0 kritisch, `:11-19`)
1. **I1 Harbor push/pull vom Heim-Netz langsam** — WAN-Hairpin über die Public-IP (~72 Mbit/s), kein MTU-Problem. Fix: interner TLS-Endpoint + Split-Horizon-DNS; heute Workaround `docker save | ctr import` — `:19`, `:466-493`.
2. **#12 `procedural_skills.status` → Partial-Indexe** (dupliziert `TODOS.md:275`) — `:162-175`.
3. **#13 Alembic-Backfill-UPDATEs ohne vorherigen Index-Drop** — Konvention dokumentieren (alte Indexe zuerst droppen, dann Backfill, dann neue Indexe); `pc20260527` nicht nachträglich umschreiben — `:179-192`.
4. **Große API-Route-Dateien** „OK, beobachten" (`routes/speakers.py` 650 Zeilen) — `:81-88`.
Frontend-Restposten: `useDeviceConnection.ts` (637 Zeilen) in Sub-Hooks zerlegen — `:345-353`; **fehlende Error Boundaries** pro Top-Level-Route — `:355-357`.
Fehlende Tests: `services/audio_output_service.py`, `services/output_routing_service.py`, `integrations/frigate.py` (nur Mock-Tests), Satellite-Hardware — `:612-618`.
Empfehlungsliste noch unerledigt: „Requirements pinnen" (`:634`), „Dependency Updates (Minor)" (`:642`), „Major Dependency Updates" (`:646`).
Hinweis: die Test-Coverage-Tabelle (`:606-610`, Backend 1642 Tests) widerspricht `CLAUDE.md` („3.400+"); Dokument ist an dieser Stelle veraltet.

### `src/satellite/TECHNICAL_DEBT.md` — 5 offene Future-TODOs
Audio-Preprocessing aufs Backend (High) · **Boot-Window-WS-Handshake-Timeout** (rotes LED-Blinken ~11 min nach Reboot; drei plausible Mechanismen, Diagnoseplan mit `tcpdump`-Service, Workaround `systemctl restart`) · Opus-Kompression (laut TODOS inzwischen als C1 gebaut) · Echo Cancellation · 4-Mikrofon-Beamforming · Wake-Word-Training.

### Weitere `docs/*.md`
- `docs/RAG_PARITY_PLAN.md:1-3,149` — **Status: PARKED** seit 2026-04-19, „pending worker-split dogfooding period"; wird durch v2.5 entparkt.
- `docs/voice-pipeline-plan.md:109` — **B-4 Backend-Audio-Preprocessing-Offload: STILL DEFERRED** (Opus-Soft-Block ist aufgehoben).
- `docs/VOICE_PIPELINE_DESIGN.md:44` „Non-goals (explicitly deferred)", `:489` „Known acute risks (not deferred — track explicitly)", `:571`/`:812` **hostPath → PVC-Migration auf Phase B.next verschoben**, `:785` Speaches bleibt als Fallback geparkt.
- `docs/FEDERATION_PAIRING.md:104-126` — „What the tier does (and what's still deferred)": ein Link kann heute nur per Admin-DB-Schreibzugriff entstehen → zurückgestellt.
- `docs/SATELLITE_ACOUSTIC_COMMISSIONING.md:268` — Esszimmer (Orange Pi/XVF3800): **„parked, needs new hardware"**.
- `docs/B5_XTTS_EVAL.md:30` — Produktionskorpus-Stichprobe (D3 zweite Quelle) **deferred**, Operator-Workflow blockiert auf manueller Anonymisierung.
- `docs/KUBERNETES_DEPLOYMENT.md:157` — Rückbenennung des Clusters nach der 2026-09-12-Recovery zurückgestellt („a rename is a restore").
- `docs/FOLDER_INGEST.md:174`, `docs/FEATURES.md:47` — der deferred `created_date`/OCR-PATCH nach dem Paperless-Consume bleibt abgesichert (Restposten, dokumentiert).
- `docs/HANDOVER_graph_expansion.md:96` — Chunk/Memory→Entity-Pivots auf Phase 3 verschoben.

### Ausdrücklich ohne Ergebnis
- ~~`docs/GPU_TOPOLOGY.md` existiert nicht~~ **FALSCH, siehe Grenzen oben** — die Datei existiert seit #1265; die Erhebung lief gegen eine veraltete Arbeitskopie. Ihre offenen Punkte (physische Prüfungen an `pve4`, zwei ungemessene Werte) gehören zum Bestand — weder unter `docs/` noch anderswo im Repo; die Formulierung „nur physisch zu klären" kommt in keiner Markdown-Datei vor. Die GPU-Topologie ist nur als Memory (`reference_cluster_gpu_topology.md`) geführt.
- `docs/runbooks/` enthält genau ein Dokument (`cookie-auth-flag-flip-xidra.md`) ohne offene „ausstehend"-Marker.
- `tasks/*.md` (33 Planungsdateien inkl. `tasks/todo.md`, `tasks/lessons.md`) wurde **nicht** ausgewertet — nicht Teil der genannten Quellen; enthält erfahrungsgemäß weitere Restposten.


---

# Teil B — GitHub-Issues

Stand: **2026-09-18**, reine Bestandsaufnahme (read-only, nichts geändert).
Quelle: `gh issue list --state open --limit 400`, `gh pr list --state open`.

- **Offene Issues: 41**
- **Offene Pull Requests: 0** — ausdrücklich bestätigt: `gh pr list --state open` liefert eine leere Liste. Der jüngste gemergte PR ist #1267 (2026-09-18).

## Alterskennzeichnung (Legende)

| Symbol | Bedeutung | Schwelle (letzte Aktualisierung) |
|---|---|---|
| 🟢 | in den letzten 30 Tagen aktualisiert | ab 2026-08-19 |
| 🟡 | 30–90 Tage still | 2026-06-20 bis 2026-08-18 |
| 🔴 | länger als 90 Tage unberührt | vor 2026-06-20 |

Verteilung: **🟢 9 · 🟡 4 · 🔴 28**. Die große Mehrheit des Rückstands stammt aus Januar/Februar 2026 und wurde seither nicht angefasst.

---

## Gesamttabelle (nach Nummer)

| # | Titel | Labels | Erstellt | Aktualisiert | Komm. | Alter |
|---|---|---|---|---|---|---|
| 11 | computer vision — real-time video processing | – | 2026-01-21 | 2026-01-24 | 0 | 🔴 |
| 12 | Face authentication | – | 2026-01-21 | 2026-01-24 | 0 | 🔴 |
| 13 | gesture control | – | 2026-01-21 | 2026-01-24 | 0 | 🔴 |
| 21 | Time and situation dependent audio volume | – | 2026-01-24 | 2026-01-24 | 0 | 🔴 |
| 23 | Video / Computer Screen output | – | 2026-01-24 | 2026-01-25 | 0 | 🔴 |
| 79 | feat: Push-Benachrichtigungen (Web Push API) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 |
| 80 | feat: Gesichtserkennung (Face Recognition) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 |
| 81 | feat: Horizontal Scaling (Multi-Instance Backend) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 |
| 82 | feat: Grafana Dashboards für Monitoring | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 |
| 83 | feat: Alerting (Monitoring-Benachrichtigungen) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 |
| 84 | feat: Haptic Feedback für Mobile PWA | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 |
| 85 | feat: Client Libraries (Python & TypeScript SDK) | enhancement | 2026-02-07 | 2026-02-07 | 0 | 🔴 |
| 121 | Improve error message when Ollama is not running | help wanted, good first issue, backend | 2026-02-11 | 2026-02-11 | 0 | 🔴 |
| 122 | Write integration test for Weather MCP server | help wanted, good first issue, testing, mcp | 2026-02-11 | 2026-02-11 | 3 | 🔴 |
| 124 | Add French language support (i18n) | help wanted, good first issue, frontend | 2026-02-11 | 2026-02-11 | 0 | 🔴 |
| 125 | Add loading skeleton to chat message list | help wanted, good first issue, frontend | 2026-02-11 | 2026-02-16 | 2 | 🔴 |
| 127 | Add satellite hardware assembly guide with photos | documentation, help wanted, good first issue, satellite | 2026-02-11 | 2026-02-11 | 0 | 🔴 |
| 260 | Routines / Scenes — Voice-Triggered Workflow Chains | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 261 | Shopping List / Shared Lists | enhancement, backend, frontend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 262 | Natural Language Automation Builder | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 263 | Dashboard Widgets for Web Panels | enhancement, backend, frontend | 2026-02-24 | 2026-02-27 | 1 | 🔴 |
| 264 | Voice Timers & Room-Aware Reminders | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 265 | Skill Marketplace — Community MCP Servers & Workflow Templates | enhancement, backend, frontend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 266 | Contextual Music Mood | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 267 | Proactive Room Briefings — Context-Aware Greetings | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 268 | Voice-Controlled Cooking Assistant | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 270 | Package Tracking from Emails | enhancement, backend | 2026-02-24 | 2026-02-24 | 0 | 🔴 |
| 342 | Phase 1: platform ↔ ha-glue extraction (Q3 2026 open-source) | – | 2026-04-14 | 2026-04-14 | 0 | 🔴 |
| 871 | Test renfield_en + renfield_it wake-word models (native EN / IT speaker needed) | – | 2026-07-01 | 2026-07-01 | 0 | 🟡 |
| 875 | KG bi-temporal edges: expire-not-delete on contradicted kg_relations | enhancement, backend | 2026-07-01 | 2026-07-01 | 0 | 🟡 |
| 876 | Cross-user / household KG entity canonicalization (v2 named-circles) | enhancement, backend | 2026-07-01 | 2026-07-01 | 0 | 🟡 |
| 877 | Populate kg_entities.external_id via an offline entity linker | enhancement, backend | 2026-07-01 | 2026-07-01 | 0 | 🟡 |
| 1113 | Voice satellite architecture — would love to exchange notes | – | 2026-08-22 | 2026-08-22 | 0 | 🟢 |
| 1116 | Login/User-Mgmt-Security-Audit 2026-08-23: dokumentierte Rest-Findings | security, backend | 2026-08-23 | 2026-09-03 | 2 | 🟢 |
| 1206 | Vision-Modell wird bei jeder Anfrage verdrängt — geteilte 16-GB-Karte überbucht | enhancement, backend | 2026-09-04 | 2026-09-04 | 2 | 🟢 |
| ~~1209~~ | ~~OTA-Rollback-Status terminiert nicht~~ **behoben 2026-09-19** | backend | 2026-09-04 | 2026-09-19 | 0 | ✅ |
| 1210 | OTA kann keine neue Abhängigkeit ausliefern — Allowlist aus laufendem Code (Henne-Ei) | backend, satellite | 2026-09-04 | 2026-09-04 | 0 | 🟢 |
| 1211 | sat-wohnzimmer ist taub: WM8960 probe schlägt fehl (-110), kein Aufnahmegerät | satellite | 2026-09-04 | 2026-09-04 | 0 | 🟢 |
| 1215 | feat(scanner): USB document scanner ingest with configurable 1..n routing targets | – | 2026-09-08 | 2026-09-08 | 1 | 🟢 |
| 1218 | feat(ingest): per-integration ingest credentials with self-rotation | – | 2026-09-08 | 2026-09-08 | 3 | 🟢 |
| 1240 | feat(kg): Kontaktpunkte — Telefon, Mobil, Fax, Mail, Web an Personen und Firmen | – | 2026-09-13 | 2026-09-13 | 0 | 🟢 |

---

## Gruppiert nach Thema

Die Gruppierung ist aus Titel, Rumpf und Labels abgeleitet; Issues mit zwei Heimaten sind bei der dominanten Gruppe geführt und bei der zweiten als Querverweis erwähnt.

### A. Sprachpfad / Satelliten / Audio (7)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 21 | Time and situation dependent audio volume | 🔴 | Tageszeit-/Umgebungs-abhängige Lautstärke. Berührt das vorhandene Day/Night-Awareness-Subsystem (`daypart_service`) und die LED-Dimmung, die dieselbe Mechanik schon nutzen. |
| 871 | Test renfield_en + renfield_it wake-word models | 🟡 | Reine Verifikationsaufgabe, braucht Muttersprachler EN/IT. Modelle sind offline validiert, DE live. |
| 1113 | Voice satellite architecture — exchange notes | 🟢 | **Kein Arbeitsauftrag**, externe Community-Anfrage (Fragen zu Wakeword-Latenz, Sync, ReSpeaker). Bisher unbeantwortet. |
| ~~1209~~ | ~~OTA-Rollback-Status terminiert nicht~~ | ✅ | **Behoben 2026-09-19.** Drei Lücken: Endstufen wurden nicht erkannt, die Ursache wurde überschrieben, und `cleanup_stale` hatte keinen Aufrufer. Der Geräte-Kehraus (#1277) wurde nicht nachgerüstet, sondern als toter Code entfernt (BL-0502): Web-Geräte verlassen die Liste allein mit dem Socket-Schluss. |
| 1210 | OTA kann keine neue Abhängigkeit ausliefern (Henne-Ei) | 🟢 | `SAFE_PACKAGES` lebt im laufenden Code statt im Paket. Siehe „bereits erledigt?" — PR #1208 hat nur das Symptom (opuslib) behoben. |
| 1211 | sat-wohnzimmer ist taub (WM8960 probe -110) | 🟢 | Hardware-/Treiberbefund; Gerät meldet sich im Dashboard trotzdem gesund — enthält damit auch einen Monitoring-Blindfleck. |
| 127 | Satellite hardware assembly guide with photos | 🔴 | Doku, Label `satellite` + `good first issue`. Auch unter „Dokumentation" geführt. |

### B. Vision / Kamera / Multimodal (6)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 11 | computer vision — real-time video processing | 🔴 | Rumpf ist ein Wort: „MediaPipe". Kaum spezifiziert. |
| 12 | Face authentication | 🔴 | Leerer Rumpf. Überschneidet sich inhaltlich mit #80. |
| 13 | gesture control | 🔴 | Leerer Rumpf. |
| 23 | Video / Computer Screen output | 🔴 | Leerer Rumpf. |
| 80 | feat: Gesichtserkennung (Face Recognition) | 🔴 | Ausformuliert (Frigate-Ereignisse → Personen benennen). Fachlicher Zwilling von #12. |
| 1206 | Vision-Modell wird bei jeder Anfrage verdrängt | 🟢 | Betriebsproblem (VRAM auf geteilter 16-GB-Karte). Zwei ausführliche eigene Kommentare: Option E als Zwischenlösung umgesetzt, Option A (vierte GPU) bleibt Zielbild. Gehört fachlich auch zu „Infrastruktur". |

### C. Wissensbasis / KG / RAG / Ingest (6)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 875 | KG bi-temporal edges (valid_at/invalid_at) | 🟡 | Structured-Memory Phase 5, „nicht ohne ausdrückliche Freigabe starten". |
| 876 | Cross-user / household KG entity canonicalization | 🟡 | Structured-Memory Phase 5. |
| 877 | kg_entities.external_id via offline entity linker | 🟡 | Structured-Memory Phase 5. |
| 1215 | USB document scanner ingest, 1..n routing | 🟢 | Teilgeliefert (siehe unten). |
| 1218 | Per-integration ingest credentials with self-rotation | 🟢 | Teilgeliefert (siehe unten). |
| 1240 | Kontaktpunkte an Personen und Firmen | 🟢 | Design gemergt (PR #1241), Umsetzung offen. |

### D. Sicherheit / Auth (1)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 1116 | Login/User-Mgmt-Security-Audit: dokumentierte Rest-Findings | 🟢 | Sammel-Issue aus dem 5-Auditoren-Review. Kommentar 2026-09-03: Befund A per PR #1204 umgesetzt, Befund B bereits geschlossen; drei versuchte Quick-Fixes wurden nach `/code-review` ausdrücklich verworfen. Rest-Findings (u. a. CSP, SSO-Cutover, Auth-on-Flip) laut Kommentar noch offen. |

### E. Infrastruktur / k8s / Monitoring / Open-Source-Grenze (4)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 81 | Horizontal Scaling (Multi-Instance Backend) | 🔴 | In-Memory-State (WS-Registry, Device-Registry, Satellite-Sessions) nach Redis auslagern. |
| 82 | Grafana Dashboards für Monitoring | 🔴 | `/metrics` existiert (zuletzt erweitert per PR #1202), vorgefertigte Dashboards nicht. |
| 83 | Alerting (Monitoring-Benachrichtigungen) | 🔴 | **Starker Schließungskandidat**, siehe unten. |
| 342 | Phase 1: platform ↔ ha-glue extraction (Q3 2026 open-source) | 🔴 | Eltern-Tracking-Issue, verweist auf reva#133 und drei Architektur-Dokumente. |

### F. Chat-UI / Frontend / PWA (5)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 79 | Push-Benachrichtigungen (Web Push API) | 🔴 | Ergänzung zu den vorhandenen WS-Proactive-Notifications für geschlossenen Browser. |
| 84 | Haptic Feedback für Mobile PWA | 🔴 | Vibration API. |
| 124 | Add French language support (i18n) | 🔴 | `good first issue`; verweist auf `docs/MULTILANGUAGE.md`. |
| 125 | Add loading skeleton to chat message list | 🔴 | `good first issue`; ein externer Beitragender hat 2026-02-13 zugesagt, seither still. Der Issue-Text nennt noch `.jsx` — die Codebasis ist inzwischen vollständig TypeScript. |
| 263 | Dashboard Widgets for Web Panels | 🔴 | Ein externer Beitragender hat 2026-02-27 Interesse angemeldet, seither still. Berührt die inzwischen gelieferten Gen-UI-Widgets und den Kiosk. |

### G. Assistenz-Fähigkeiten / Integrationen (9)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 260 | Routines / Scenes — Voice-Triggered Workflow Chains | 🔴 | |
| 261 | Shopping List / Shared Lists | 🔴 | |
| 262 | Natural Language Automation Builder | 🔴 | HA-Automationen und n8n-Workflows im Gespräch erzeugen. |
| 264 | Voice Timers & Room-Aware Reminders | 🔴 | Teilweise überholt, siehe Kandidatenliste. |
| 265 | Skill Marketplace — Community MCP Servers & Workflow Templates | 🔴 | |
| 266 | Contextual Music Mood | 🔴 | |
| 267 | Proactive Room Briefings — Context-Aware Greetings | 🔴 | Setzt auf Presence-Hooks auf, die es inzwischen gibt. |
| 268 | Voice-Controlled Cooking Assistant | 🔴 | |
| 270 | Package Tracking from Emails | 🔴 | Vermutlich überholt, siehe Kandidatenliste. |

### H. Dokumentation / Einstiegsaufgaben / Ökosystem (4)

| # | Titel | Alter | Anmerkung |
|---|---|---|---|
| 85 | Client Libraries (Python & TypeScript SDK) | 🔴 | |
| 121 | Improve error message when Ollama is not running | 🔴 | `good first issue`, unangetastet. |
| 122 | Write integration test for Weather MCP server | 🔴 | Ein externer Beitragender hat 2026-02-11 zugesagt („I'll get started on it"), seither kein Ergebnis. |
| 127 | Satellite hardware assembly guide with photos | 🔴 | Auch unter Gruppe A geführt. |

---

## Querverweise Issue ↔ Dokumentation

### Issues, die im Rumpf auf ein Dokument verweisen

| # | Verwiesenes Dokument | Existiert im Repo? |
|---|---|---|
| 124 | `docs/MULTILANGUAGE.md` | ja |
| 127 | `docs/SATELLITE_BUILD_GUIDE.md` | **nein** — das Dokument ist das *Ergebnis* des Issues |
| 342 | `docs/architecture/renfield-open-source-{launch,readiness,platform-boundary}.md` | ja |
| 875 | `docs/HANDOVER_graph_expansion.md` | ja |
| 876 | `docs/CIRCLES.md` | ja |
| 1215 | `docs/design/scanner-ingest.md` | ja |
| 1218 | `docs/design/ingest-credentials.md` | ja |

### Dokumente, die eine offene Issue-Nummer nennen

| # | Nennende Dateien |
|---|---|
| 11 | `TODOS.md`, `docs/ENVIRONMENT_VARIABLES.md`, `docs/design/self-learning-admin-console.md` |
| 12 | `TODOS.md`, `docs/private/feature-ideen.md` |
| 13, 80 | `docs/private/feature-ideen.md` |
| 121, 122, 124, 125, 127 | `docs/private/VISIBILITY_PLAN.md` (die fünf Einstiegsaufgaben sind Teil des Sichtbarkeitsplans) |
| 342 | `TODOS.md` |
| 875 | `TODOS.md`, `docs/design/kg-bitemporal-edges.md`, `docs/design/kg-contact-points.md`, `docs/design/kg-cross-user-canonicalization.md` |
| 876 | `TODOS.md`, `docs/design/kg-cross-user-canonicalization.md` |
| 877 | `TODOS.md`, `docs/design/kg-cross-user-canonicalization.md` |
| 1116 | `TODOS.md`, `docs/ENVIRONMENT_VARIABLES.md`, `docs/runbooks/cookie-auth-flag-flip-xidra.md` |
| 1206 | `docs/ENVIRONMENT_VARIABLES.md` |
| 1240 | `docs/design/kg-contact-points.md` |

**Beobachtung zur Beidseitigkeit:** Für vier Issues gibt es ein eigenes, bereits gemergtes Design-Dokument, ohne dass das Issue selbst darauf verweist — #875 → `docs/design/kg-bitemporal-edges.md` (PR #1213), #876 → `docs/design/kg-cross-user-canonicalization.md` (PR #1199/#1200), #1240 → `docs/design/kg-contact-points.md` (PR #1241). Bei diesen dreien ist also der Entwurf fertig und die Umsetzung offen — beim Zusammenführen mit der zweiten Quelle relevant.

Umgekehrt ohne Gegenstück: die 28 alten Issues aus Januar/Februar (#11–#127, #260–#270) tauchen in `docs/design/` nirgends auf; sie leben nur in `docs/private/feature-ideen.md` bzw. `docs/private/VISIBILITY_PLAN.md`.

---

## Kandidaten, die nach Titel bereits erledigt aussehen

Nur Kandidaten, nichts geschlossen. Die Einschätzung stützt sich auf Titel/Rumpf gegen die gemergten PRs der letzten Wochen und `CLAUDE.md` — sie ist **nicht** im Code verifiziert.

| # | Titel | Warum der Verdacht |
|---|---|---|
| 83 | feat: Alerting (Monitoring-Benachrichtigungen) | **Stärkster Kandidat.** Genau die Lücke („Metriken und Benachrichtigungen sind nicht verbunden") wurde im September geschlossen: PR #1230 (Entwurf), #1231 (Alarm bei scheiternden Aufgaben + Wächter für die Nachbarinstanz), #1232 (Funktionssonden), #1233 (gegenseitige Erreichbarkeitsprüfung), plus MCP-Selbsterkennung #1249/#1253. Das Issue schlug Alertmanager vor; geliefert wurde ein eigener Pfad über `services/ops_alert.py` — inhaltlich erledigt, technisch anders gelöst. |
| 270 | Package Tracking from Emails | `CLAUDE.md` führt „Parcel Tracking" als Integration (`renfield-mcp-tracking`, Direktanbindung DHL/DP/UPS/FedEx) und Email-Auto-Ingest (IMAP IDLE) als geliefert. Die proaktive Ankündigung bei Heimkehr ist ggf. der einzige Rest. |
| 264 | Voice Timers & Room-Aware Reminders | `internal.create_reminder` (#1146), der Reminder-Checker und die präsenzgesteuerte Zustellung existieren; Erinnerungen werden im aktuellen Raum ausgespielt. Offen bliebe höchstens „benannte Timer" und „welche Timer laufen?". Eher Teil-Kandidat als klarer Schluss. |
| 1218 | Per-integration ingest credentials | Eigene Kommentare: Phasen 1, 2 und 4 sind **live** auf dem Haushalt (2026-09-08, Tag `2026-09-08-ingest-credentials`). Offen ist ausdrücklich nur Phase 3 (Selbstrotation) — Kandidat für Zuschnitt auf den Rest statt zum Schließen. |
| 1215 | USB document scanner ingest | Phase 0 und Design gemergt (#1216), Phase-1-MCP gebaut (`ebongard/renfield-mcp-scanner`), dazu #1223–#1227, #1243, #1246. Offen laut #1226 die Phase 5 (Scan-Knopf). Ebenfalls eher Zuschnitt als Schluss. |
| 12 / 80 | Face authentication / Gesichtserkennung | Keine Lieferung, aber **fachliche Dublette** — #12 hat einen leeren Rumpf, #80 ist ausformuliert. Kandidat, #12 zugunsten von #80 zu schließen. |
| 1210 | OTA-Allowlist Henne-Ei | **Nicht** erledigt, aber leicht zu verwechseln: PR #1208 hat `opuslib` in die Allowlist aufgenommen — das ist das Symptom, nicht die Ursache. Das Issue beschreibt ausdrücklich das strukturelle Problem und bleibt gültig. |

Weiterhin klar offen und ausdrücklich **keine** Kandidaten: #82 (Metriken wurden erweitert, Dashboards nicht geliefert), #875/#876/#1240 (nur der Entwurf ist fertig), #1116 (Rest-Findings laut eigenem Kommentar offen), #1206 (Zwischenlösung aktiv, Zielbild ausstehend).



---

# Teil C — `tasks/`

Erhebung 2026-09-18. **33 Dateien** (31 `.md`, 1 `.html`, 1 `.wav`). Gezählt wurden
unerledigte Kästchen `- [ ]`.

| | Anzahl |
|---|---:|
| Rohe unerledigte Kästchen | **493** |
| davon **überholt** — Arbeit geliefert, Kästchen nie abgehakt | **145** |
| **echter Rückstand** | **348** |
| zusätzlich erzählerisch, ohne Kästchen | ca. **15** |
| erledigte Kästchen | 118 |

**Einschränkung:** Nur 5 der 31 `.md` sind versioniert. Die übrigen 26 liegen per
`.gitignore` bewusst lokal; ihr Inhalt wird hier **nicht wiedergegeben**, nur gezählt.
Dieser Teil ist deshalb eine Mengenangabe, kein Nachschlagewerk wie Teil A und B.

## Verteilung des echten Rückstands

| Datei | offen | Einordnung |
|---|---:|---|
| `mobile-receipt-capture-plan.md` | **227** | Status „PROPOSED", nichts umgesetzt — allein zwei Drittel des Rückstands |
| `scanner-ingest-plan.md` | 36 | Phase 0 im Wesentlichen erledigt; Phasen 1–5 offen, teils extern bereits gebaut |
| `camera-vision-expansion-plan.md` | 25 | nie begonnen (im Code gegengeprüft: keine Vision-Intents, kein Capture-Dienst) |
| `reva-plan.md` | 23 | betrifft ein **fremdes** Repo — kein Renfield-Rückstand |
| `audit-reocr-plan.md` | 12 | Schicht 1 geliefert (`utils/ocr_quality.py`), Schichten 2–4 offen |
| `document-tier-control-plan.md` | 10 | Backend geliefert, Oberfläche offen |
| `health-history-concept.md` | 8 | Gesundheits-Vertikale, nichts umgesetzt; die allgemeine Schicht A dagegen schon |
| `ingest-credentials-plan.md` | 7 | nur noch Phase 3 (Selbstrotation) |
| übrige | ~0 | siehe „überholt" |

## Der eigentliche Befund: 14 Dateien sind lautlos veraltet

145 der 493 Kästchen stehen in Dateien, deren Thema **geliefert** ist — die Datei wurde nur
nie nachgezogen. Das verzerrt jede Zählung, die Kästchen für bare Münze nimmt.

Deutlichstes Beispiel: `todo.md` mit 16 offenen Kästchen ist vollständig auf `main`,
gelandet über **#1243** und **#1246**; der in der Datei genannte Commit fehlt nur, weil
squash-gemergt wurde. Weitere: `email-ingest-plan.md` behauptet „nothing implemented",
während der Watcher produktiv läuft · `orangepi-satellite-k8s-plan.md` behauptet
„uncommitted", obwohl das Manifest eingecheckt ist · `kiosk-active-subsystem-plan.md` ist
sogar über seine eigenen „Deferred"-Punkte hinaus geliefert.

## Sechs Posten stehen NUR hier

Diese fehlen in Issues **und** in `TODOS.md` — sie gehen bei jeder anderen Zählung verloren:

1. Dokument-Tier-Steuerung, Scheibe 2 + 3 (Sichtbarkeit auf der Dokumentkarte, 10 Punkte)
2. Audit-ReOCR, Schichten 2/3a/3b/4 (12)
3. Kamera-/Vision-Erweiterung A/D/B1/B2 + Gate E (25)
4. Gesundheits-Vertikale T1–T8 samt vier ungelösten Fragen (12)
5. KG-Quellenspan-Provenienz, Neu-Extraktion, drei Politur-Punkte (~7)
6. Barge-in §16: Software-AEC und LLM-Abbruch stromaufwärts (2, ausdrücklich nie übertragen)

## Regelabweichung bei der Versionierung

`.gitignore` ignoriert `tasks/*` und nimmt **drei** Dateien ausdrücklich wieder auf.
Zum Zeitpunkt der Erhebung waren aber **fünf** versioniert. Einmal getrackt, greift
`.gitignore` nicht mehr — deshalb kann eine solche Abweichung beliebig lange unbemerkt
bleiben.

**Aufgelöst am 2026-09-18** (#1274 + Folge-PR): Die zwei überzähligen Dateien wurden mit
`git rm --cached` aus dem Index genommen; sie bleiben auf der Platte und werden jetzt von
der bestehenden Regel erfasst. Versioniert sind wieder genau die drei vorgesehenen.
Merke für später: Vor einem Commit in `tasks/` prüfen, was tatsächlich getrackt ist —
`.gitignore` allein beantwortet das nicht.

## `lessons.md`

Kein Rückstand, sondern gesammelte Lehren: vier Einträge, letzter 2026-09-12, jeweils mit
ableitbarer Regel. Auffällig im Abgleich mit der Projekt-Regel „nach jeder Korrektur einen
Eintrag": Vier Einträge über gut einen Monat stehen einem deutlich größeren Bestand an
Korrektur-Notizen andernorts gegenüber. Die Datei ist ein Auszug, kein vollständiges
Protokoll.
