# Changelog

Alle markanten Änderungen an Renfield, seit Release `v1.2.0`. Format lehnt sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/) an; Versionierung folgt [SemVer](https://semver.org/lang/de/).

---

## [v2.0.0] — 2026-04-21

Erste Major-Version seit `v1.0.0`. Der Sprung reflektiert drei generationelle Architektur-Schritte — **Circles / Second Brain**, **Federation v2**, **Async Worker-Split** — sowie die Umstellung auf **k8s** als Produktions-Topologie.

### ⚠ Aufwärtskompatibilität

- **Circles-Migration**: Bestehende Daten in `document_chunks`, `kg_entities`, `kg_relations`, `conversation_memories` erhalten über die Alembic-Migrationen aus Lane B automatisch `atom_id`- und `circle_tier`-Spalten. Default-Tier ist `2` (household) für Dokumente, `1` (trusted) für Chat-Memories. Single-User-Installationen (`AUTH_ENABLED=false`) sehen keine Verhaltensänderung — der Tier-Filter ist kurzgeschlossen.
- **Chat-Upload ist nun asynchron**: `POST /api/chat_upload` liefert sofort mit `status=pending` und einer `upload_id`; Frontend pollt `GET /api/chat_upload/{id}` auf `status=completed`. Synchrone Upload-Clients müssen auf Polling umgestellt werden.
- **Agent-visible Paperless-Tools**: `mcp.paperless.upload_document` wurde aus der Agent-Tool-Liste entfernt. Für den Upload angehängter Dokumente über den Chat existiert das neue `internal.forward_attachment_to_paperless` (keine Code-Änderung notwendig in nutzerseitigem Prompt — der Agent wählt das Tool automatisch).
- **Konversations-Memory respektiert Circles**: `ConversationMemoryService.retrieve()` filtert nun nach Tier-Reichweite. Aufrufer **müssen** `user_id=asker_id` übergeben; `None` im auth-enabled Modus reduziert auf Tier 4 allein.

### Hinzugefügt

#### Circles & Second Brain (neu)

- **Circles v1** — fünfstufige Zugriffsleiter (self, trusted, household, extended, public) pro Eigentümer, 4-Zweig-Zugriffsregel (OWNER ∨ PUBLIC ∨ EXPLICIT GRANT ∨ TIER-REACH). Lanes A/B/C in [#401](https://github.com/ebongard/renfield/pull/401), [#402](https://github.com/ebongard/renfield/pull/402), [#403](https://github.com/ebongard/renfield/pull/403).
- **Atoms-Registry** — polymorphe Identitätsschicht über `document_chunks`, `kg_entities`, `kg_relations`, `conversation_memories`. Denormalisierte `circle_tier`- und `atom_id`-Spalten auf den Quell-Tabellen für SQL-Filter-Performance.
- **Cross-Source-Suche** via Reciprocal Rank Fusion — `/api/atoms`, `/brain`-Page.
- **Review-Queue** — `/brain/review` zeigt neu klassifizierbare Atome mit menschenlesbaren Labels ([#427](https://github.com/ebongard/renfield/pull/427)).
- **KB-Share-Explosion** — `kb_shares_service` expandiert KB-Level-Shares in Per-Chunk-Grants.
- **Explicit Grants** — Notion/Drive-Ausnahmen über `atom_explicit_grants`.
- **Frontend-Seiten** — `/brain`, `/brain/review`, `/settings/circles`, `/settings/circles/peers`.
- Dokumentation: [`docs/CIRCLES.md`](docs/CIRCLES.md), [`docs/SECOND_BRAIN.md`](docs/SECOND_BRAIN.md).

#### Federation v2 — Multi-Peer

- **F1 MCP-Streaming-Surface** — Wire-Protokoll für streamende MCP-Server ([#406](https://github.com/ebongard/renfield/pull/406), [#407](https://github.com/ebongard/renfield/pull/407)).
- **F2 Pairing** — Ed25519-Identität + `peer_users` ([#408](https://github.com/ebongard/renfield/pull/408)).
- **F3 query_brain** — Responder-Backend ([#410](https://github.com/ebongard/renfield/pull/410)), Asker `RemoteBrainMCPClient` ([#411](https://github.com/ebongard/renfield/pull/411)), Agent-Loop-Integration mit Ollama-Synthese ([#412](https://github.com/ebongard/renfield/pull/412)).
- **F4 UX** — Peers-Seite + Revoke ([#413](https://github.com/ebongard/renfield/pull/413)), Pairing-QR-Modals ([#414](https://github.com/ebongard/renfield/pull/414)), Live-Progress-Relay *„frage Moms Brain…"* ([#415](https://github.com/ebongard/renfield/pull/415)), Audit-Feed unter `/brain/audit` ([#416](https://github.com/ebongard/renfield/pull/416)).
- **F5 Robustheit** — Depth + Cycle Detection ([#417](https://github.com/ebongard/renfield/pull/417)), Per-Peer + Per-Asker Rate-Limits ([#418](https://github.com/ebongard/renfield/pull/418)), Redis-backed Pending-Request-Store ([#419](https://github.com/ebongard/renfield/pull/419)), TLS-Fingerprint-Pinning ([#420](https://github.com/ebongard/renfield/pull/420)), TOFU Auto-Pinning beim Pairing ([#421](https://github.com/ebongard/renfield/pull/421)).
- Dokumentation: [`docs/FEDERATION_MULTI_PEER.md`](docs/FEDERATION_MULTI_PEER.md).

#### Asynchrone Document-Ingestion — Worker Split

- **PR A** — Infrastruktur für Async-Ingestion mit Status-Polling ([#388](https://github.com/ebongard/renfield/pull/388)).
- **PR B** — RAGService in `extractor` / `ingestor` aufgeteilt.
- **PR C1/C2** — Upload-Cutover, Polling-Frontend, A11y, HTTP-Semantik ([#391](https://github.com/ebongard/renfield/pull/391), [#393](https://github.com/ebongard/renfield/pull/393)).
- Eigener `document-worker`-Deployment (siehe [`docs/DOCUMENT_WORKER_SPLIT.md`](docs/DOCUMENT_WORKER_SPLIT.md)).

#### Kubernetes-Produktion

- **Private GPU-Cluster** als Ziel-Deploy ([#386](https://github.com/ebongard/renfield/pull/386)) — Manifeste in `k8s/`, Blackwell-GPU-Nodes (RTX 5070 Ti / 5060 Ti), Traefik-Ingress, Harbor-artiges Private Registry.
- Dokumentation: [`docs/KUBERNETES_DEPLOYMENT.md`](docs/KUBERNETES_DEPLOYMENT.md).

#### Agent & Routing

- **Orchestrator + Adaptive Cards** — parallele Sub-Agent-Koordination ([#374](https://github.com/ebongard/renfield/pull/374), [#384](https://github.com/ebongard/renfield/pull/384)).
- **Sub-Intent Dispatch** — feingranulare Routing-Entscheidungen via Hook ([#307](https://github.com/ebongard/renfield/pull/307), [#384](https://github.com/ebongard/renfield/pull/384)).
- **Context-aware Routing** — Entity-Pre-Routing + Keyword-Boosting ([#368](https://github.com/ebongard/renfield/pull/368)).
- **Parallel Tool Execution** — Multi-Tool-Calls in einem Agent-Step ([#328](https://github.com/ebongard/renfield/pull/328)).
- **Routing-Trace-Dashboard** — Admin-UI + `post_routing`-Hook ([#370](https://github.com/ebongard/renfield/pull/370)).
- **Token-Budget-Enforcement** + Tool-Preselection + Output-Guard ([#312](https://github.com/ebongard/renfield/pull/312)).
- **Routine-Agent** — Good-Night / Good-Morning-Sequenzen ([#271](https://github.com/ebongard/renfield/pull/271)).
- **Stale-Tool-Error-Marker** — `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` verhindert, dass historische Fehler Re-Execution blockieren ([#430](https://github.com/ebongard/renfield/pull/430)).
- **Internal-Tool `forward_attachment_to_paperless`** — der Agent leitet angehängte Dateien an Paperless weiter, ohne jemals base64 zu sehen ([#433](https://github.com/ebongard/renfield/pull/433)).

#### Auth & Multi-Tenancy

- **Pluggable Authentication** via Hook-System ([#334](https://github.com/ebongard/renfield/pull/334)), `ProtectedRoute` für Chat ([#335](https://github.com/ebongard/renfield/pull/335)).
- **Voice Authentication** per Sprechererkennung (optional).
- **White-Label-Branding** via `VITE_APP_NAME` + `VITE_APP_LOGO_URL` ([#378](https://github.com/ebongard/renfield/pull/378), [#379](https://github.com/ebongard/renfield/pull/379)).

#### RAG-Qualität

- **Contextual Retrieval** + Reranking + Parent-Child-Chunking + Eval-Pipeline ([#324](https://github.com/ebongard/renfield/pull/324)).
- **Knowledge-Graph-Scopes** — konfigurierbare Entitätstypen ([#318](https://github.com/ebongard/renfield/pull/318)).

#### Memory

- **Episodic Lifecycle** — Confidence Decay, Trigger-Pattern, konfigurierbares Extraktions-Modell ([#331](https://github.com/ebongard/renfield/pull/331)).
- **Always-Inject Essential Memories** — wichtige Fakten landen unabhängig von Similarity-Score im Kontext ([#251](https://github.com/ebongard/renfield/pull/251)).
- **Per-User Personality Style** ([#276](https://github.com/ebongard/renfield/pull/276)).

#### Satellites

- **Visual Queries** — Satelliten-Kamera + Vision-LLM für Fragen zum Bild vor Ort.
- **XVF3800** USB-Array + Enviro pHAT ([#310](https://github.com/ebongard/renfield/pull/310)).
- **Whisplay HAT**-Support.
- **Konfigurierbare IDLE-LED-Farbe** pro Satellit.
- **Neue Satelliten**: Esszimmer ([#292](https://github.com/ebongard/renfield/pull/292)), Arbeitszimmer, BensZimmer.
- **Action-Success-Metadata** in Konversationshistorie — verhindert Fehler-Nachgeplapper in Follow-ups ([#431](https://github.com/ebongard/renfield/pull/431), [#432](https://github.com/ebongard/renfield/pull/432)).

#### Media

- **Media Follow Me** — Wiedergabe folgt dem Nutzer zwischen Räumen ([#240](https://github.com/ebongard/renfield/pull/240)).
- **TuneIn-Radio-Integration** ([#237](https://github.com/ebongard/renfield/pull/237)).
- **Genre-Suchhints** in Agent-Prompts ([#235](https://github.com/ebongard/renfield/pull/235)).
- **Room-Owner-Dropdown** in Admin-UI ([#240](https://github.com/ebongard/renfield/pull/240)).

#### Hook-System

- **`pre_agent_context`** + **`pre_save_message`**-Hooks, erweiterte History-Window ([#302](https://github.com/ebongard/renfield/pull/302)).
- **`execute_tool`**-Hook für Plugin-Tool-Dispatch.
- **`token_budget_info`** + **`token_usage_info`** ContextVars für Plugins ([#409](https://github.com/ebongard/renfield/pull/409)).

#### Mobile & PWA

- **iOS-Capacitor-Wrapper** mit PWA-Icons für iPhone-App ([#329](https://github.com/ebongard/renfield/pull/329)).

#### Admin

- **Conversation Summary** — LLM-basierte Zusammenfassung + `context_vars` ([#304](https://github.com/ebongard/renfield/pull/304)).
- **Admin-Maintenance-Page** — Knowledge-Graph-Qualität, Duplikat-Erkennung, Bulk-Cleanup.

#### Plugin-Infrastruktur

- **Alembic Plugin-Metadata-Discovery** ([#363](https://github.com/ebongard/renfield/pull/363)).
- **ha_glue-aware env.py** für Autogenerate ([#357](https://github.com/ebongard/renfield/pull/357)).

### Behoben

- **Paperless-Upload-Chain** — URL-Suffix (`/api/api/`, [#429](https://github.com/ebongard/renfield/pull/429)), MIME-Type (`application/octet-stream` → echte Typen, `renfield-mcp-paperless#3`), Base64-Validation (`renfield-mcp-paperless#4`), Agent-Halluzination-Vermeidung ([#433](https://github.com/ebongard/renfield/pull/433)).
- **Lifecycle AsyncSessionLocal-Shadow** — Import-Reihenfolge in `_init_mcp` ([#428](https://github.com/ebongard/renfield/pull/428)).
- **KG useEffect-Dependency-Typo** — `scopeFilter` → `tierFilter` ([#426](https://github.com/ebongard/renfield/pull/426)).
- **Circles Render-Fix** — `ConfirmDialogComponent` als Element, nicht Komponente ([#425](https://github.com/ebongard/renfield/pull/425)).
- **Auth-Disabled Guards** auf verbleibende Circle/Atom-Routen ([#424](https://github.com/ebongard/renfield/pull/424)).
- Weitere ~45 Fixes; Details im Git-Log.

### Sicherheit

- **Input-Guard**, **MCP-Kompaktierung**, **Memory-Defense** — Reva-Backport Prio 1 ([#311](https://github.com/ebongard/renfield/pull/311)).
- **TLS-Cert-Pinning** für Federation-Peers.
- **Session-Scoped Attachment-Lookup** — verhindert Cross-Session-Zugriff auf fremde Chat-Uploads ([#433](https://github.com/ebongard/renfield/pull/433) follow-up).

### Dokumentation

- Neu: [`docs/CIRCLES.md`](docs/CIRCLES.md), [`docs/SECOND_BRAIN.md`](docs/SECOND_BRAIN.md), [`docs/FEDERATION_MULTI_PEER.md`](docs/FEDERATION_MULTI_PEER.md), [`docs/KUBERNETES_DEPLOYMENT.md`](docs/KUBERNETES_DEPLOYMENT.md), [`docs/DOCUMENT_WORKER_SPLIT.md`](docs/DOCUMENT_WORKER_SPLIT.md).

---

## [v1.2.0] und früher

Keine CHANGELOG-Einträge vor `v2.0.0`. Vollständige Commit-Historie: [`git log v1.0.0..v1.2.0`](https://github.com/ebongard/renfield/compare/v1.0.0...v1.2.0).

---

[v2.0.0]: https://github.com/ebongard/renfield/compare/v1.2.0...v2.0.0
