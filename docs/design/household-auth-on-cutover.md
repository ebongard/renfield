# Haushalt auf auth-on — Entwurf (BL-0400)

**Backlog:** BL-0400 (BL-0139 darin aufgegangen) mit den eingebrachten Posten BL-0141 (Sichtbarkeit von Unterhaltungen), BL-0255 (Subsume mehrnutzerfähig), BL-0302/#876 (Spike, erledigt), BL-0305 (F-ID-2 Double-Login)
**Status:** ENTWURF 2026-09-21 — **die Mehrnutzerfähigkeit des Haushalts ist gewollt; dieser Entwurf plant den Weg, nicht das Ob.** Kein Go zum Bauen, kein Go zum Umschalten, bis der Eigentümer D-1 … D-10 (§13) entschieden hat; danach Runbook + Bauposten (§14).
**Datum:** 2026-09-21
**Autor:** Claude Fable 5.1
**Datengrundlage:** Code auf `main` `57306315`; Zählwerte der Haushalts-Datenbank vom 2026-09-21 (nur Aggregate, keine Inhalte), adversarisch nachgeprüft. Die Zahlen bemessen den Umzug; sie begründen weder das Ob noch den Nutzen. Andere Instanzen können eine andere Verteilung haben und sind hier kein Argument.

---

## 0. Ergebnis in einem Absatz

**Der Schalter ist klein, der Datenbesitz ist das Projekt.** Unter `AUTH_ENABLED=false` schreibt der Haushalt alles auf *einen* Rückfall-Eigentümer (Nutzer 1) mit Stufe 0, lässt getippte Unterhaltungen ohne Eigentümer und hält die Zugriffsfilter aus. Legt man den Schalter um, ohne die Daten vorher neu zu verorten, sehen alle Mitglieder außer dem Admin-Konto nichts mehr, alle Satelliten trennen sich, das Kiosk-Display geht aus, der Browser-`device_action` ohne Identität wird abgelehnt — während **der Satellitenpfad für unerkannte Sprecher absichtlich offen bleibt** (§6.1, eine Entscheidung, die der Eigentümer treffen muss). **Weg:** Konten und Sprecher-Verknüpfungen *vor* dem Umschalten anlegen; den Bestand als **Haushaltswissen (Stufe 2)** neu verorten; für Satelliten eine dauerhafte Geräte-Zugangsberechtigung und ein Gerätekonto **ohne Gedächtnis** bauen (beides fehlt heute im Code); eine Kiosk-Rolle einführen; dann die sechs ConfigMap-Schlüssel gemeinsam setzen — mit sofortigem Rückweg (`AUTH_ENABLED=false` stellt die heutigen Lesepfade byte-identisch wieder her). Der Cookie-Modus folgt als zweiter Schritt nach dem bestehenden Runbook.

## 1. Ausgangslage — was umzieht (Zählwerte 2026-09-21)

**Identität:** 5 Nutzer (2 in Rolle 1, 3 in Rolle 2), **1 mit verknüpftem Sprecher** (Nutzer 2); 1 Sprecher, 6 Sprecher-Einbettungen. `circles`: 1 Zeile (Eigentümer 1, Erfassungsvorgabe `{"tier": 0}`). `circle_memberships`: **1 Zeile** — Kopplungs-Rest (`(circle_owner_id=1, member_user_id=1, tier 2)`, von `pairing_service._upsert_circle_membership` mit der *entfernten* Nutzer-ID eines früheren Peers geschrieben, `services/pairing_service.py:328-333`), kein Haushaltsmitglied. `atom_explicit_grants`: 0. `federation_user_links`: 0.

**Wissen:** 16 775 Atome gesamt (4 811 `kg_node`, 4 231 `kg_edge`, 7 251 `document_fact`, 401 `kb_document`, 81 `conversation_memory`). Davon **16 389 Eigentümer 1 / Stufe 0**, 56 Eigentümer 1 auf handgesetzten Stufen (2 × Stufe 1, 19 × 2, 35 × 4), **330 Eigentümer 2 / Stufe 0** (56 `kg_node`, 66 `kg_edge`, 198 `document_fact`, 10 `kb_document` — sprecher-attribuierte Züge des einen verknüpften Nutzers). `kg_relations`: 4 231, davon **38 mit `user_id NULL`**. `documents`: 380 (377 mit `atom_id`), davon **373 in den Wissensbasen 4–9 ohne `owner_id`**; 24 `kb_document`-Atome ohne lebendes Dokument (Registry-Drift). `notes` 0, `meetings` 0.

**Unterhaltungen:** 209, davon **206 mit `user_id NULL`** (2 850 von 2 970 Nachrichten); 45 tragen eine `speaker_id`, 3 eine `user_id`.

**Sonst:** `tool_outcome_stats` 3 (Eigentümer 2), `agent_trajectories` 14 (NULL), `procedural_skills` 5 Seeds (NULL, Stufe 4), `notifications` 10 (Ziel Nutzer 1), `reminders` 0.

Die eine Aussage, die daraus folgt: **aus den Daten lässt sich nicht ablesen, wem etwas gehört.** Eigentümer 1 ist ein technischer Rückfall (`services/atom_owner.py:33-46`: `SELECT id FROM users ORDER BY id LIMIT 1`, weil `atoms.owner_user_id` NOT NULL ist, `models/database.py:2661`), nicht der Autor. Stufen wurden nie gesetzt, weil es unter auth-off nichts zu setzen gab.

## 2. Was der Schalter ändert

`AUTH_ENABLED` ist der eine Auth-Schalter für REST **und** WebSocket (`WS_AUTH_ENABLED` zurückgezogen; ein widersprechender Rest bricht den Start, `utils/config.py:2115-2121`). Rund 120 Lesestellen prüfen `settings.auth_enabled`; die Klassen:

1. **Wer ist „der Nutzer“ — die REST/WS-Asymmetrie.** REST mit `get_user_or_default` liefert unter auth-off das `admin`-Konto (`services/auth_service.py:515-565`), `get_current_user` liefert `None` (`:363`), `require_permission` lässt alles durch (`:585`). Der WebSocket liefert `{"authenticated": True, "auth_skipped": True}` **ohne** `user_id` (`services/websocket_auth.py:173-174`) → `api/websocket/chat_handler.py:1190` liest `None`. Im Browser-Sprachpfad wird ein erkannter Sprecher mit Konfidenz ≥ `voice_auth_min_confidence` (0,7) über `users.speaker_id` zum Nutzer (`chat_handler.py:1208-1235`, `:141-157`); am Satelliten löst `satellite_handler.py:886-904` die Identität **allein aus dem Sprechernamen** (`sat_user_id`) — die Identität des WS-Tokens wird dort nie gelesen. **Getippte Chat-Züge bekommen nie eine Identität.** Deshalb sind 206 Unterhaltungen eigentümerlos.
2. **Die Circle-Filter schalten scharf.** Alle Retrieval-Pfade kurzschließen heute mit `if not settings.auth_enabled and not enforce_circles` (`rag_retrieval.py:647`, `memory_retrieval.py:528`, `note_retrieval.py:62`, `document_fact_retrieval.py:138`, `kg_retrieval.py:193,246,487`, `graph_expansion.py:49,94`). Danach gilt der Vier-Zweig-Filter (`circle_sql.py:124-195`: Eigentümer ∨ Stufe 4 ∨ expliziter Grant ∨ Tier-Reichweite über `circle_memberships`). Ein Zug **ohne** Identität liest nur Stufe 4 (`memory_retrieval.py:529-530`).
3. **Schreib- und Rechtetore.** `_identity_scoped_write_denied` verweigert Memory-Änderungen ohne `user_id` (`conversation_memory_service.py:1107-1120`); `_find_similar_memories`/`_find_duplicate` liefern leer (`:1907`, `:2262`). Der **Browser-`device_action`** ohne Identität wird abgelehnt (`chat_handler.py:1006-1013`). Die `internal.*`-Tore (`kb_maintenance_tool.py:608,1116`, `system_health_tool.py:197`, `paperless_reextract_tool.py:142`) prüfen `if settings.auth_enabled and user_permissions is not None` — sie greifen für **identifizierte** Züge; unidentifizierte passieren weiterhin (Ausnahme `internal.purge_archive`, `paperless_dedupe_tool.py:83`, `docs/INTERNAL_TOOLS.md:245`). **Der Satellitenpfad bleibt für unerkannte Sprecher offen:** `satellite_handler.py:884-904` übergibt `user_permissions=None`, `services/mcp_client.py:2185-2198` und `ha_glue/bootstrap.py:544` lassen `None` absichtlich durch (#690: „gesprochene Befehle müssen funktionieren“). Auth-on schließt diese Tür nicht — §6.1, D-4.
4. **Bootbedingungen.** Der `SECRET_KEY`-Wächter (`config.py:2045-2082`, ≥ 32 Zeichen) schärft mit `AUTH_ENABLED` **oder** einem realen `RENFIELD_ENV` (`:2051-2053`); alle fünf Backend-Image-Workloads injizieren den Schlüssel bereits (`backend.yaml:163`, `document-worker.yaml:122`, `meeting-worker.yaml:86`, `pdf-split-worker.yaml:77`, `alembic-upgrade-job.yaml:62`). Ein realer `RENFIELD_ENV` schaltet zusätzlich: das `ALLOW_REGISTRATION`-Boot-Tor (#1295, `:2183-2188`; der Haushalt setzt `"false"`, `k8s/configmap.yaml:31`), die `COOKIE_SECURE`-Pflicht im Cookie-Modus (`:2158`) und die Warnung vor `changeme`-Vorgaben (`:2004-2012`). Cookie-Modus verlangt außerdem `CORS_ORIGINS` ≠ `*` (`:2143-2168`).
5. **Frontend.** `AuthContext.tsx:250-260` erfindet unter auth-off einen Pseudo-Admin `id: 0`; `ProtectedRoute.tsx:35-41` leitet danach auf `/login`; der Bootstrap-Admin hat `must_change_password=true` (`auth_service.py:745`) und wird auf `/change-password` gezwungen — der WS-Handshake lehnt einen solchen Nutzer ebenfalls ab (`websocket_auth.py:282-287`); Chat ist bis zur Rotation tot.

Die vollständige Gefahrenliste steht in §12.

## 3. Zielbild

- **Jede Person ein Konto**, Rolle aus dem bestehenden Satz: *Admin*, *Familie* (`kb.shared`, `ha.full`, `cam.view`, `chat.own`, `rooms.read`, `speakers.own`, …), *Gast* (`docs/ACCESS_CONTROL.md:180-196`). Selbstregistrierung bleibt aus; Konten legt der Admin über `POST /api/users` an — ein `bin/`-Skript dafür gibt es nicht (§12 Nr. 17).
- **Identität aus zwei Quellen:** Login (Browser, PWA) und Stimme (Sprecher → `users.speaker_id`, eindeutig, `models/database.py:1197`). Die Sprechererkennung bleibt im Haushalt **an** (Vorgabe `SPEAKER_RECOGNITION_ENABLED=true`, `config.py:205`; kein Schlüssel in der ConfigMap) — sie *ist* die Identität am Satelliten. Das ist eine bewusste Abweichung von der Entscheidung D4 in `docs/design/browser-voice-auth-on-instances.md:36` (Art.-9-Gate); im Haushalt sind die Betroffenen die Mitglieder selbst, die Einwilligung ist Teil der Kontoanlage (D-6).
- **Geteilt wird über Stufen, nicht über Konten:** Haushaltswissen liegt bei einem Eigentümer auf Stufe 2 und ist für Mitglieder über `circle_memberships` erreichbar; Privates bleibt Stufe 0. Das Modell aus `docs/CIRCLES.md` hatte im Haushalt bisher keine zweite Person — jetzt bekommt es sie.
- **Geräte sind keine Personen.** Satelliten und Kiosk erhalten Gerätekonten mit eng geschnittenen Rollen (§6), nie das Admin-Konto.
- **Unterhaltungen bleiben persönlich** (`chat.own`); Haushaltswissen fließt über Erinnerungen und den Wissensgraphen — es sei denn, der Eigentümer will geteilte Unterhaltungen als Funktion (§8.1, D-3).

## 4. Datenbesitz neu verorten

### 4.1 Atome des Bestands

| Klasse | Zeilen | Ohne Eingriff nach dem Umschalten |
|---|---|---|
| Eigentümer 1, Stufe 0 (Rückfall) | 16 389 | nur Admin-Konto |
| Eigentümer 1, handgesetzt Stufe 1 / 2 / 4 | 2 / 19 / 35 | Stufe 1: nur Admin + Stufe-1-Mitglieder; 2: Haushaltsmitglieder; 4: alle |
| Eigentümer 2, Stufe 0 (sprecher-attribuiert) | 330 | nur Nutzer 2 |

Optionen für den Bestand:

| | A — Bestand = Haushaltswissen | B — Bestand bleibt, wie er ist | C — je Quellklasse |
|---|---|---|---|
| Vorgehen | alle Eigentümer-1-Atome mit Stufe 0 auf **Stufe 2** (Eigentümer bleibt 1); jedes Familienmitglied bekommt bei Eigentümer 1 eine Mitgliedschaft Stufe 2 | nichts | z. B. Dokumente/Fakten/KG → 2, Erinnerungen → 0 |
| Sichtbarkeit | die 16 389 Rückfall-Atome: wie heute für alle Mitglieder. **Nicht** wie heute: die 330 Eigentümer-2-Atome (nur Nutzer 2, sofern nicht ebenfalls gehoben — D-2b) und die 2 Stufe-1-Atome (nur Stufe-1-Mitglieder) | nur Admin sieht den Bestand | gemischt |
| Leckrisiko gegenüber heute | keins — Stufe 2 reicht nicht über den Haushalt hinaus (Föderation sieht Stufe 2 nicht, `circle_sql.py:138-141`) | keins, aber Verlust | keins, Verlust je Klasse |
| Rückbau | `--revert` setzt die Stufen zurück, Mitgliedschaften löschen | — | wie A |
| Aufwand (Mensch / CC) | Skript mit `--dry-run`, per-Zeilen-Transaktion, **über `AtomService.update_tier`** (`atom_service.py:250-335`: Kaskade Dokument → Chunks → Fakten, Entität → Relationen; nie direktes UPDATE, `docs/CIRCLES.md` Anti-Patterns) — ~1 Tag / ~1 h | 0 | ~2 Tage / ~2 h |

**Empfehlung A**, mit **D-2b** für die 330 Eigentümer-2-Atome: sie sind die einzigen Zeilen mit belegter Zuordnung zu einer Person; Vorschlag: Nutzer 2 entscheidet selbst (Vorgabe: bleiben Stufe 0 — privat; heben ist jederzeit über `PATCH /api/atoms/{id}` möglich). Die 56 handgesetzten Stufen bleiben unangetastet (Absicht). Die Erfassungsvorgabe (`circles.default_capture_policy`) jedes neuen Kontos wird bei der Anlage gesetzt — Vorschlag Familie 2, Gast 0 (D-2c).

### 4.2 Eigentümerlose Zeilen

- **`kg_relations` mit `user_id NULL` (38):** unter auth-on in keinem Zweig erreichbar → Eigentümer 1 setzen (sie hängen an Eigentümer-1-Entitäten), Stufe mit 4.1.
- **Wissensbasen 4–9 (373 Dokumente):** `knowledge_bases.owner_id NULL`; der Eigentümerzweig für Dokumente greift heute nur über den Atom-Eigentümer-Rückfall (`circle_sql.py:89-98`, `:302-405`). Backfill: `owner_id = 1`. Das betrifft den **Eigentümer**-Zweig; die Sichtbarkeit für Mitglieder läuft über `document_chunks.circle_tier`/`document_facts.circle_tier`, die 4.1 über die Kaskade setzt. Der Backfill schlüsselt auf Dokumente (380, 377 mit `atom_id`), nicht auf Atome (401, davon 24 verwaist).
- **Unterhaltungen (206 NULL):** keine Atome — keine Stufe, kein `atom_id` (`models/database.py:29-91`; `api/routes/chat.py:511-530`). Sieben Lesepfade filtern auf `Conversation.user_id == asker` (`chat.py:292-303, 408-431, 442-455, 475-505, 511-563`; `conversation_service.py:344-353, 477-489`), und der WS **adoptiert** eine eigentümerlose Unterhaltung durch den ersten authentifizierten Öffner (`conversation_service.py:488-489`) — ein Zufallseigentümer. Optionen: (a) alle 206 dem Admin-Konto („Archiv“); (b) NULL lassen (unsichtbar bis zur zufälligen Adoption); (c) die 45 mit `speaker_id` dem verknüpften Nutzer, der Rest dem Admin. **Empfehlung (c)**, und die Adoption vor dem Cutover **abschalten** (eigentümerlos + auth-on → neue Unterhaltung anlegen statt adoptieren).

### 4.3 Mitgliedschaften und der Kopplungs-Rest

Die Zeile `(1,1,tier,2)` vor dem Cutover löschen. Neue Mitgliedschaften: n·(n−1) Zeilen für n Mitglieder, Stufe je Eigentümer — Vorschlag: jedes Familienmitglied bei jedem anderen auf Stufe 2; Gäste bei niemandem oder auf Stufe 3 (D-2d). Es gibt keine automatische Anlage; Schreiber sind allein `POST /api/circles/me/members` (`api/routes/circles.py:211-262`, Eigentümer fügt Mitglied hinzu) und die Kopplung — das Backfill-Skript legt die Zeilen aus einer Mitgliederliste an.

## 5. Identität

- **Sprecher-Verknüpfung für alle Mitglieder** vor dem Cutover: Enrollment (`SPEAKER_CONTROLLED_ENROLLMENT_ENABLED=true`, `configmap.yaml:469`) und `POST /api/users/{id}/link-speaker` (`api/routes/users.py:639-713`, UI `UsersPage.tsx`). Heute 1 von 5 — der Engpass für Sprach-Identität ist die Verknüpfung, nicht der Schalter.
- **Am Satelliten** entsteht Identität allein aus dem erkannten Sprecher (`satellite_handler.py:886-904`). Ohne erkannten Sprecher ist der Zug anonym: Lesen nur Stufe 4, keine Erinnerung (Extraktion nur mit `user_id`, `satellite_handler.py:152-157`, `:1075`), **aber weiterhin Gerätesteuerung** (§2 Nr. 3).
- **`VOICE_AUTH_ENABLED`** (Login per Stimme, `api/routes/auth.py:844-868`) ist ein anderes Feature, im Haushalt aus, für den Cutover nicht nötig.

## 6. Geräte und Dienste

### 6.1 Satelliten — drei Lücken im Code, eine Entscheidung

Nach dem Umschalten schließt `satellite_handler.py:316-319` jede Verbindung ohne gültiges Token (4001). Was der Satellit heute anbieten kann, trägt nicht:

1. **Keine dauerhafte Geräte-Zugangsberechtigung.** `authenticate_websocket` akzeptiert ein Access-JWT (24 h, `websocket_auth.py:221-231`, `config.py:1428`) oder ein Gerätetoken aus `/api/ws/token` (60 min, im Speicher, nach jedem Pod-Neustart weg; unter auth-on nur mit `current_user`, `main.py:514-515`). Der Satellit ruft `/api/ws/token` ohne Zugangsdaten (`satellite.py:712`) oder sendet einen statisch konfigurierten `auth_token` (`:704-706`, `renfield_satellite/config.py:330`, `:481`). Keine Route, kein Skript prägt ein langlebiges Token (alle `create_access_token`-Aufrufer sind Login/Refresh). **P0-Bauposten:** eine Geräte-Zugangsberechtigung — entweder ein langlebiges, scope-begrenztes Geräte-JWT (`scope: "satellite"`, widerrufbar über `token_epoch`), oder die vorhandene H1-Enrollment-PSK (`satellite_enrollment_enabled`, geprüft heute erst im `register`-Frame, `:431-459`) wird **am Handshake** akzeptiert. Empfehlung: die PSK am Handshake — sie existiert, ist je Gerät, wird bereits über die Provisionierung verteilt (Flotteneinstellung mit zwei Quellen, Regel `satellites.md`) und ist nicht personengebunden.
2. **Anonyme Aktuierung bleibt offen.** Ein unerkannter Sprecher steuert Geräte weiterhin — `mcp_client.py:2185-2198`, `bootstrap.py:544`, #690. Das war unter auth-off eine Selbstverständlichkeit (ein Haushalt); unter auth-on ist es eine Entscheidung (**D-4a**): offen lassen (jede Stimme im Haus schaltet Licht — heutiger Nutzen, auch für Gäste) oder schließen (dann muss `None` auf dem Satellitenpfad zu `[]` werden wie bei REST, `api/routes/chat.py:137`, und unerkannte Stimmen können nichts mehr schalten). Empfehlung: **offen lassen für `ha.control`-Klasse, schließen für alles darüber** (`ha.full`-Dienste, Kamera, Schreibwerkzeuge) — also ein Gast-Rechtesatz für den anonymen Satellitenzug statt `None`.
3. **Ein Gerätekonto würde Gedächtnis ansammeln.** Das Extraktions-Tor prüft `user_id`, nicht die Sprechererkennung (`satellite_handler.py:152-157`, `conversation_memory_service.py:1114`). Sobald ein Gerätekonto seine `user_id` in `sat_user_id` einspeist (nötig, damit der anonyme Zug Stufe-2-Wissen liest und Rechte trägt), läuft die Extraktion und das Gerätekonto sammelt Erinnerungen aller Stimmen. **P0-Bauposten:** ein Kennzeichen am Konto (`users.is_device_account`), das die Extraktion (`_spawn_satellite_extraction`, `_identity_scoped_write_denied`) und die Beförderung sauber trennt: Gerätekonto liest bis Stufe 2 und trägt seinen Rechtesatz, schreibt aber **nie** Erinnerungen; ein erkannter Sprecher ersetzt das Gerätekonto im Zug.

Damit lautet **D-4b**: (a) **Gerätekonto „Haushalt“** (Rolle mit `ha.control`/`ha.full` nach D-4a, `kb.shared`, keine Schreibwerkzeuge; Kennzeichen „Gerät“; Mitgliedschaft Stufe 2 bei Eigentümer 1) — anonyme Züge lesen Haushaltswissen, schalten Geräte, erinnern nichts; oder (b) **kein Gerätekonto** — anonyme Züge bleiben `user_id=None`: lesen nur Stufe 4, schalten nach D-4a, erinnern nichts. Empfehlung (a): sie bewahrt „Renfield, wann ist der Müll?“ für jede Stimme im Haus.

### 6.2 Kiosk — eigene Rolle und eigenes Konto

`kiosk_handler.py:421-435` verlangt `Permission.ADMIN`, `App.tsx:111-115` kapselt `/kiosk` in `<AdminRoute>` (= `ProtectedRoute permission="admin"`, `ProtectedRoute.tsx:78-79` — berechtigungs-, nicht rollenbasiert). Ein Wanddisplay mit stehender Admin-Sitzung ist die falsche Antwort. Empfehlung: neue Berechtigung `kiosk.view` in `models/permissions.py` (es gibt keine kiosk-förmige), Rolle **„Kiosk“** (`kiosk.view`, `rooms.read`), Gerätekonto; `kiosk_handler` und die Route prüfen `kiosk.view`. Der Kiosk zeigt inhaltsfreie Nutzlasten (Regel `kiosk.md`) — die Rolle darf entsprechend eng sein. Sitzung: Refresh-Token 30 Tage mit automatischem Refresh; kein „nie ablaufendes“ Token.

### 6.3 Unverändert oder gewollt verändert

HA-Webhook (eigenes Token, `notifications.py:97-121`), `/api/internal/auth/verify` (Voice-Server, eigenes Geheimnis), Ingest-Zugangsdaten `rfi.*` (eigenes Schema; ihre CRUD verlangt dann `settings.manage`, `ingest_credentials.py:150-241`), Föderation (Responder filtert immer, `federation_query_responder.py:313,505`). Meldungen und Scan-Ereignisse werden vom Broadcast zu eigentümergerichtet (`user_events.py:116`, `scanner_jobs.py:404`) — gewollt; `/ws/user` verlangt eine Nutzeridentität (`user_events_handler.py:55-58`; der Kiosk nutzt `/ws/kiosk`, nicht betroffen).

## 7. Konfiguration des Schalters

Der Kommentarblock in `k8s/configmap.yaml:36-44` nennt die sechs Schlüssel bereits; sie werden **gemeinsam** in einem Commit gesetzt:

| Schlüssel | heute | Cutover | Grund |
|---|---|---|---|
| `AUTH_ENABLED` | `false` | `true` | der Schalter; schärft den `SECRET_KEY`-Wächter (der Wächter beweist die Schlüsselstärke beim Start) |
| `RENFIELD_ENV` | `development` | `production` | aktiviert #1295 (`ALLOW_REGISTRATION` muss gesetzt sein), die Cookie-Secure-Pflicht und die `changeme`-Warnung; für den Schlüsselwächter nicht nötig |
| `ALLOW_REGISTRATION` | `false` | `false` (gesetzt lassen) | sonst bricht #1295 den Start |
| `CORS_ORIGINS` | `*` | `https://renfield.local` | `*` schaltet den CSWSH-Schutz des WS ab (`websocket_auth.py:127-129`); Cookie-Modus verweigert `*` |
| `TRUSTED_PROXIES` | `""` | Traefik-Pod-CIDR | sonst Anmeldesperre nur je Nutzername (#1296; `docs/SECURITY.md:131`) |
| `API_RATE_LIMIT_STORAGE_URI` | `memory://` | `redis://redis:6379` | geteilte Limits bei >1 Backend-Pod |

`AUTH_COOKIE_ENABLED` bleibt beim Cutover **aus** und folgt als zweiter Schritt nach `docs/runbooks/cookie-auth-flag-flip-xidra.md` (Vorflug §0 dort gilt unverändert; der Haushalts-Pfad ist git → `kubectl apply -f k8s/configmap.yaml`). `MEMORY_SUBSUME_TO_KG` siehe §8.2.

## 8. Die vier eingebrachten Posten

### 8.1 BL-0141 — Sichtbarkeit von Unterhaltungen: Modellfrage, D-3

Unterhaltungen tragen weder Stufe noch Atom; eine Stufe würde die sieben Lesepfade und die Nachrichtensuche auf `circle_sql` umstellen (§4.2). Das Modell des Zweiten Gehirns teilt **Wissen** (Erinnerungen, Graph, Dokumente), nicht **Verläufe** — ein geteilter Verlauf enthält alles, was darin gesagt wurde, ohne die Stufen-Entscheidung je Fakt. Empfehlung: Unterhaltungen bleiben `chat.own`; wer etwas teilen will, tut es über die Stufe der extrahierten Erinnerung. Will der Eigentümer geteilte Unterhaltungen **als Funktion**, ist das ein eigener Bauposten (Unterhaltung als Atom mit Stufe; Größe L) — D-3.

### 8.2 BL-0255 — Subsume mehrnutzerfähig: vor dem Cutover, sonst Validator

`MEMORY_SUBSUME_TO_KG=true` läuft im Haushalt (`configmap.yaml:419`). Das Signal ist **namensbasiert** (`captured_kg_subjects`, `conversation_memory_service.py:603-606, 700-757`), der Rückfall löst „eigene oder eigentümerlose“ Entitäten ohne Tier-Reichweite auf (`:759-800`, `:741-744`): mit mehreren Nutzern kann ein Fakt verworfen werden, weil der Graph eine gleichnamige Entität eines **anderen** Nutzers kennt (`TODOS.md:576`). Zwei Wege: (a) **vorher mehrnutzerfähig bauen** — Subjekt-Auflösung je Frager durch `kg_entities_circles_filter`, das erfasste Set nach Entitäts-ID *und* Eigentümer geschlüsselt, dasselbe Tor im v2-Pfad (BL-0421: v2 umgeht das Subsume-Tor, `:318-325`); (b) **am Cutover abschalten**. Empfehlung (a) als P0-Posten (M), (b) als Rückfall im Runbook. Zusätzlich ein Validator „`MEMORY_SUBSUME_TO_KG=true` + `AUTH_ENABLED=true` bricht den Start“, bis (a) gemergt ist — Präzedenz `assert_auth_config_consistency` (`config.py:2082`, Tests `test_config_auth_consistency.py`); die private Instanz setzt den Schlüssel nicht (Vorgabe `False`, `config.py:923`), der Validator trifft sie also nicht.

### 8.3 BL-0302 / #876 — erledigt; Linker danach

Spike gemergt (#1302): Co-Referenz statt Shared-Ownership. Für den Haushalt braucht der Cutover **keinen** Linker: nach 4.1 hat der Bestand einen Eigentümer und Mitgliedschaften für alle; getrennte persönliche Knoten derselben Person entstehen erst, wenn Mitglieder je für sich über dieselben Personen sprechen — dann greift der Linker (E-1: nach diesem Entwurf).

### 8.4 BL-0305 — F-ID-2 Double-Login-Consent: durch diesen Cutover freigeschaltet

P0 der personenbezogenen Föderation ist dieser Cutover (`docs/design/federation-identity-mapping.md:52,115,263`). Der Bauplan steht (§9 dort: offer → accept → complete, beide Seiten signiert, keine Halblinks; Routen, Zustandsautomat, UI und Tests fehlen — `api/routes/federation_user_links.py:68-122` ist Admin-CRUD). Festgehalten aus §7 dort: `querier_ref` je `(peer, user)`, nie global; **Ehren eingehender Links ist ein Schalter je Peer und je Richtung** (neue Spalte an `peer_users`), Vorgabe für den Haushalt: eingehend **aus**, ausgehend **an** (D-9). Nach dem Cutover verliert die Peer-Scope-Begründung „Haushalt = ein Nutzer“ (`docs/CIRCLES.md:38`, `docs/SECOND_BRAIN.md:139`) ihre Prämisse; die dort notierte `remote_user_id`-Kollision bei ≥ 2 Peers ist vor dem ersten Personenlink zu schließen (F-ID-3).

## 9. Reihenfolge — jede Stufe für sich rückbaubar

| Stufe | Inhalt | Rückweg |
|---|---|---|
| **P0 Bau (dunkel)** | Geräte-Zugangsberechtigung für Satelliten (§6.1 Nr. 1); Gerätekonto-Kennzeichen + Extraktions-/Beförderungstor (§6.1 Nr. 3); anonymer Satelliten-Rechtesatz nach D-4a (§6.1 Nr. 2); `kiosk.view` + Rolle (§6.2); Adoption eigentümerloser Unterhaltungen aus (§4.2); Subsume mehrnutzerfähig oder Validator (§8.2); `bin/backfill_household_tiers.py` (`--dry-run`, `--revert`, per-Zeilen-Txn, über `AtomService`); Tests §11 | gewöhnliche PRs, alle dunkel |
| **P1 Konten (noch auth-off)** | Konten für alle Mitglieder (`POST /api/users`), Rollen, Erfassungsvorgabe, Sprecher-Enrollment + Verknüpfung; Gerätekonten Haushalt + Kiosk | unter auth-off wirkungslos |
| **P2 Backfill (noch auth-off)** | Dry-Run mit Zählwerten → Freigabe → Commit: Stufen (§4.1, D-2), NULL-Relationen, KB-Eigentümer, Unterhaltungen (§4.2), Kopplungs-Rest, Mitgliedschaften (§4.3) | unter auth-off ohne Wirkung auf Lesepfade; `--revert` |
| **P3 Umschalten** | Satelliten-Zugangsberechtigung ausrollen (Flotte, Provisionierung); ConfigMap-Commit mit den sechs Schlüsseln; Rollout; Admin-Passwortrotation; Abnahme §11 | `AUTH_ENABLED=false` → Lesepfade byte-identisch; ein Satellit mit Zugangsberechtigung verbindet auch unter auth-off, weil der Server den Header dann gar nicht prüft (`websocket_auth.py:173-174`) |
| **P4 Cookie** | `AUTH_COOKIE_ENABLED` nach dem Runbook | Flag zurück, ~1 min |
| **P5 danach** | Subsume (falls in P0 nur der Validator), F-ID-2, Linker (E-1), ggf. geteilte Unterhaltungen (D-3) | eigene Posten |

## 10. Rollback

`AUTH_ENABLED=false` stellt jeden Lesepfad byte-identisch wieder her (§2 Nr. 2). Die Datenänderungen aus P2 sind unter auth-off **inert** (Stufen und Mitgliedschaften werden nicht ausgewertet); `--revert` stellt die Stufen her, falls gewünscht. Konten, Sprecherverknüpfungen, Gerätekonten sind harmlos. Was **nicht** rückbaubar ist: Erinnerungen, die zwischen Umschalten und Rückbau beim richtigen Eigentümer entstanden sind — sie bleiben ihm zugeordnet und werden unter auth-off wieder für alle sichtbar (der heutige Zustand, kein Leck). `RENFIELD_ENV` kann auf `production` bleiben.

## 11. Tests und Abnahme (Pflicht vor P3)

1. **Vier-Zweig-Filter mit ≥ 2 Nutzern auf echtem Postgres** (`renfield_test`): Mitglied sieht Stufe-2-Zeilen des Eigentümers, nicht Stufe 0; Nicht-Mitglied nur Stufe 4; Grant überschreibt; je Retrieval-Verbraucher (RAG, KG, Memory, Notizen, Fakten, Graph-Expansion). Im Haushalt wurde der Mitgliedschaftszweig nie ausgeübt — der Cutover ist seine erste Nutzung dort.
2. **Backfill-Skript:** Dry-Run-Zählwerte = Commit-Zählwerte; Kaskade; `--revert`; Idempotenz.
3. **Gerätekonto:** anonymer Satellitenzug liest Stufe 2, trägt den D-4a-Rechtesatz, schreibt **keine** Erinnerung (Kennzeichen-Tor); erkannter Sprecher ersetzt das Gerätekonto.
4. **Geräte-Zugangsberechtigung:** Satellit verbindet unter auth-on **und** auth-off; Widerruf trennt.
5. **Kiosk-Rolle:** Kiosk-Konto öffnet `/ws/kiosk` und `/kiosk`, Admin-Routen 403.
6. **Adoption aus:** eigentümerlose Unterhaltung wird unter auth-on nicht adoptiert.
7. **Boot-Validatoren:** die sechs Schlüssel zusammen booten; jede Einzelabweichung bricht laut (`test_config_auth_consistency.py`).
8. **Browser-E2E nach P3** (smoke-tester): Login, Passwortrotation, Chat, Wissen, Kiosk, ein Sprachzug am Satelliten mit und ohne erkannten Sprecher — Rückweg geprobt.

## 12. Gefahrenliste (aus dem Code)

1. Start verweigert ohne starken `SECRET_KEY` — `config.py:2051-2081`. 2. Start verweigert, wenn `ALLOW_REGISTRATION` nicht gesetzt — `:2183-2188`. 3. Alle außer Admin verlieren den Bestand — `atom_owner.py:41`, Filter §2. 4. WS-Zeilen tragen `user_id NULL` — `websocket_auth.py:173-174`, `chat_handler.py:1190`. 5. Alle Satelliten trennen sich, und es gibt keine dauerhafte Geräte-Zugangsberechtigung — `satellite_handler.py:316-319`, `main.py:514-515`, `websocket_auth.py:221-231`. 6. Kiosk geht aus — `kiosk_handler.py:421-435`, `App.tsx:111-115`. 7. Unerkannte Sprecher werden still nicht erinnert — `conversation_memory_service.py:1114,1907,2262`. 8. Bootstrap-Admin muss rotieren, WS lehnt bis dahin ab — `auth_service.py:745`, `ProtectedRoute.tsx:46-48`. 9. Anmeldesperre ohne `TRUSTED_PROXIES` nur je Nutzername — `docs/SECURITY.md:131`. 10. `CORS_ORIGINS=*` schaltet den WS-Origin-Schutz ab — `websocket_auth.py:127-129`. 11. `internal.*`-Tore greifen nur für identifizierte Züge; anonyme passieren (außer `purge_archive`) — §2 Nr. 3. 12. Browser-`device_action` ohne Identität abgelehnt; **Satellitenpfad bleibt offen** — `chat_handler.py:1006-1013` vs. `mcp_client.py:2185-2198`. 13. `/ws/user` verlangt Nutzeridentität — `user_events_handler.py:55-58`. 14. Meldungen/Scan-Ereignisse eigentümergerichtet — `user_events.py:116`, `scanner_jobs.py:404`. 15. Ingest-CRUD verlangt `settings.manage` — `ingest_credentials.py:150-241`. 16. Peer-Scope-Begründung verliert die Prämisse — `docs/CIRCLES.md:38`. 17. Kein Skript legt Nutzer an — `auth_service.py:711-714`. 18. Frontend-Pseudo-Admin `id 0` verschwindet — `AuthContext.tsx:250-260`. 19. Ein Gerätekonto ohne Kennzeichen sammelt Erinnerungen — `satellite_handler.py:152-157`, `conversation_memory_service.py:1114`.

## 13. Entscheidungen für den Eigentümer

- **D-2 Bestand:** (a) A Haushaltsstufe 2 für die Eigentümer-1-Rückfall-Atome (Empfehlung) · B belassen · C je Klasse. **(b)** die 330 Eigentümer-2-Atome: Nutzer 2 entscheidet, Vorgabe privat (Empfehlung) · ebenfalls heben. **(c)** Erfassungsvorgabe neuer Konten: Familie 2 / Gast 0 (Empfehlung). **(d)** Gäste: keine Mitgliedschaft (Empfehlung) · Stufe 3.
- **D-3 Unterhaltungen:** (c) sprecher-verknüpfte an den Nutzer, Rest an den Admin, Adoption aus (Empfehlung) · (a) alle an den Admin · (b) NULL lassen. Und: geteilte Unterhaltungen als Funktion bauen (Posten L) · nicht bauen (Empfehlung).
- **D-4 Satelliten:** **(a)** anonyme Aktuierung unter auth-on: Gast-Rechtesatz mit `ha.control` (Empfehlung) · ganz offen wie heute · geschlossen. **(b)** Gerätekonto „Haushalt“ ohne Gedächtnis (Empfehlung) · keins. **(c)** Zugangsberechtigung: Enrollment-PSK am Handshake (Empfehlung) · langlebiges Geräte-JWT.
- **D-5 Kiosk:** `kiosk.view` + Rolle + Gerätekonto (Empfehlung) · stehende Admin-Sitzung.
- **D-6 Sprechererkennung:** im Haushalt an, Einwilligung bei Kontoanlage (Empfehlung) · aus (dann keine Sprach-Identität; Satelliten laufen nur als Gerätekonto).
- **D-7 Subsume:** vorher mehrnutzerfähig (Empfehlung) · am Cutover abschalten; Validator bis dahin (Empfehlung).
- **D-8 Cookie:** zweiter Schritt (Empfehlung) · zusammen mit dem Cutover.
- **D-9 F-ID-2 Richtung:** eingehend aus / ausgehend an (Empfehlung) · beide an · beide aus.
- **D-10 Reihenfolge:** P0 vollständig vor P1 (Empfehlung) · P1/P2 parallel zu P0 beginnen (Konten und Verknüpfungen sind unter auth-off ohnehin wirkungslos).

## 14. Was danach entsteht

Nach den Entscheidungen: `docs/runbooks/auth-on-cutover-household.md` (Vorflug §7, P1–P3 als Kommandofolge, Abnahme §11, Rückweg §10) und die P0-Bauposten (Geräte-Zugangsberechtigung, Gerätekonto-Kennzeichen + Tor, anonymer Rechtesatz, `kiosk.view`, Adoption aus, Subsume/Validator, Backfill-Skript, Filter-Tests) als einzelne PRs — jeder dunkel, jeder für sich reviewbar.
