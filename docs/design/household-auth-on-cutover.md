# Haushalt auf auth-on — Entwurf (BL-0400)

**Backlog:** BL-0400 (Grundsatzentscheidung; BL-0139 darin aufgegangen) mit den eingebrachten Posten BL-0141 (Sichtbarkeit von Unterhaltungen), BL-0255 (Subsume mehrnutzerfähig), BL-0302/#876 (Spike, erledigt), BL-0305 (F-ID-2 Double-Login)
**Status:** ENTWURF 2026-09-21 — **kein Go zum Bauen, kein Go zum Umschalten.** Der Eigentümer entscheidet D-1 … D-9 (§13); danach Runbook + Bauposten (§14).
**Datum:** 2026-09-21
**Autor:** Claude Fable 5.1
**Datengrundlage:** Code auf `main` `57306315`; Zählwerte der Haushalts-Datenbank vom 2026-09-21 (nur Aggregate, keine Inhalte). Andere Instanzen können eine andere Verteilung haben und sind hier kein Argument.

---

## 0. Auftrag und Ergebnis in einem Absatz

Die Triage (2026-09-20) hat die Frage „Soll der Haushalt auth-on werden?“ nicht mit Ja/Nein beantwortet, sondern einen Entwurf beauftragt. Dieser Entwurf zeigt: **der Schalter selbst ist klein, der Datenbesitz ist das Projekt.** Unter `AUTH_ENABLED=false` schreibt der Haushalt seit jeher alles auf *einen* Rückfall-Eigentümer (Nutzer 1) mit Stufe 0 und lässt Unterhaltungen ohne Eigentümer; die Zugriffsfilter sind abgeschaltet. Legt man den Schalter um, ohne die Daten vorher neu zu verorten, sehen vier von fünf Haushaltsmitgliedern nichts mehr, alle Satelliten trennen sich, das Kiosk-Display geht aus, und Sprachbefehle ohne erkannten Sprecher steuern keine Geräte mehr. **Empfehlung:** auth-on als eigenes, gestuftes Projekt planen — Konten und Sprecher-Verknüpfungen *vor* dem Umschalten anlegen, den Bestand als **Haushaltswissen (Stufe 2)** neu verorten (das ist sichtbarkeitsgleich zu heute), ein Gerätekonto für Satelliten und ein Kiosk-Konto einführen, und erst danach umschalten — mit sofortigem Rückweg (`AUTH_ENABLED=false` stellt die heutigen Lesepfade byte-identisch wieder her). Der Cookie-Modus (`AUTH_COOKIE_ENABLED`) folgt als eigener zweiter Schritt nach dem bestehenden xidra-Runbook.

## 1. Ausgangslage — die Zahlen des Haushalts

Alle Werte sind Zählungen vom 2026-09-21.

**Identität:** 5 Nutzer (2 in Rolle 1, 3 in Rolle 2), davon **1 mit verknüpftem Sprecher**; 1 Sprecher, 6 Sprecher-Einbettungen. `circles`: 1 Zeile (Eigentümer 1, Erfassungsvorgabe Stufe 0). `circle_memberships`: **1 Zeile** — und die ist Kopplungs-Rest (`(circle_owner_id=1, member_user_id=1, tier 2)`, geschrieben von `pairing_service._upsert_circle_membership` mit der *entfernten* Nutzer-ID des damaligen Peers; `services/pairing_service.py:328-333`), kein echtes Haushaltsmitglied. `atom_explicit_grants`: 0. `federation_user_links`: 0.

**Wissen:** 16 775 Atome — **16 389 mit Eigentümer 1 und Stufe 0**; 330 mit Eigentümer 2 (Stufe 0); 56 handgesetzte Atome auf Stufe 1/2/4. Nach Quelle: 4 755 `kg_node`, 4 165 `kg_edge`, 7 053 `document_fact`, 391 `kb_document`, 81 `conversation_memory`. `kg_entities` 4 811 (4 755 Eigentümer 1, 56 Eigentümer 2), `kg_relations` 4 231 (**38 mit `user_id NULL`**), `documents` 380 (373 davon in Wissensbasen 4–9 **ohne** `owner_id`), `notes` 0, `meetings` 0.

**Unterhaltungen:** 209, davon **206 mit `user_id NULL`** (2 850 von 2 970 Nachrichten); 45 tragen eine `speaker_id`, 3 eine `user_id`.

**Sonst:** `tool_outcome_stats` 3 (Eigentümer 2), `agent_trajectories` 14 (NULL), `procedural_skills` 5 Seeds (NULL, Stufe 4), `notifications` 10 (Ziel Nutzer 1), `reminders` 0.

Was daraus folgt: **Teilen wurde nie benutzt** (0 Grants, 0 echte Mitgliedschaften), und **aus den Daten lässt sich nicht ablesen, wem etwas gehört** — der Eigentümer 1 ist ein technischer Rückfall (`services/atom_owner.py:33-46`: `SELECT id FROM users ORDER BY id LIMIT 1`, weil `atoms.owner_user_id` NOT NULL ist, `models/database.py:2661`), nicht der Autor.

## 2. Was der Schalter ändert

`AUTH_ENABLED` ist der eine Auth-Schalter für REST **und** WebSocket (`WS_AUTH_ENABLED` ist zurückgezogen; ein widersprechender Rest bricht den Start, `utils/config.py:2115-2121`). Rund 120 Lesestellen prüfen `settings.auth_enabled`; die wichtigsten Klassen:

1. **Wer ist „der Nutzer“ — die REST/WS-Asymmetrie.** REST-Routen mit `get_user_or_default` liefern unter auth-off das `admin`-Konto (`services/auth_service.py:515-565`), `get_current_user` liefert `None` (`:363`), `require_permission` lässt alles durch (`:585`). Der WebSocket liefert `{"authenticated": True, "auth_skipped": True}` **ohne** `user_id` (`services/websocket_auth.py:173-174`) → `chat_handler.py:1190` liest `None`. Einzige Rückgewinnung: ein erkannter Sprecher mit Konfidenz ≥ `voice_auth_min_confidence` (0,7) wird über `users.speaker_id` zu einem Nutzer (`chat_handler.py:1208-1235`, `:141-157`). **Getippte Chat-Züge bekommen nie eine Identität.** Genau deshalb sind 206 Unterhaltungen eigentümerlos.
2. **Die Circle-Filter schalten scharf.** Alle Retrieval-Pfade kurzschließen heute mit `if not settings.auth_enabled and not enforce_circles` (`rag_retrieval.py:647`, `memory_retrieval.py:528`, `note_retrieval.py:62`, `document_fact_retrieval.py:138`, `kg_retrieval.py:193,246,487`, `graph_expansion.py:49,94`). Danach gilt der Vier-Zweig-Filter (`circle_sql.py:124-195`: Eigentümer ∨ Stufe 4 ∨ expliziter Grant ∨ Tier-Reichweite über `circle_memberships`). Ein Zug **ohne** Identität liest nur Stufe 4 (`memory_retrieval.py:529-530`).
3. **Schreibtore schalten scharf.** `_identity_scoped_write_denied` verweigert Memory-Änderungen ohne Identität (`conversation_memory_service.py:1107-1120`); `_find_similar_memories`/`_find_duplicate` liefern leer (`:1907`, `:2262`); `device_action` ohne `user_id` wird abgelehnt (`chat_handler.py:1006-1013`); `internal.*`-Rechte greifen (`kb_maintenance_tool.py:608,1116`, `system_health_tool.py:197`, `paperless_reextract_tool.py:142`; `internal.purge_archive` verweigert unidentifizierte Züge, `docs/INTERNAL_TOOLS.md:245`).
4. **Bootbedingungen.** `RENFIELD_ENV=production` allein schärft den `SECRET_KEY`-Wächter (`config.py:2045-2082`, ≥ 32 Zeichen, in *jedem* Backend-Image-Workload — `backend.yaml:163`, `document-worker.yaml:122`, `meeting-worker.yaml:86`, `pdf-split-worker.yaml:77`, `alembic-upgrade-job.yaml:62` injizieren ihn bereits). Prod + auth-on + `ALLOW_REGISTRATION` **nicht gesetzt** bricht den Start (#1295, `config.py:2183-2188`; der Haushalt setzt `"false"`, `k8s/configmap.yaml:31`). Cookie-Modus zusätzlich: `CORS_ORIGINS` ≠ `*`, `COOKIE_SECURE=true` (`:2143-2168`).
5. **Frontend.** `AuthContext.tsx:250-260` erfindet unter auth-off einen Pseudo-Admin `id: 0`; `ProtectedRoute.tsx:35-41` leitet danach jede Route auf `/login`; der Bootstrap-Admin hat `must_change_password=true` (`auth_service.py:741`) und wird auf `/change-password` gezwungen — der WS-Handshake lehnt einen solchen Nutzer ebenfalls ab (`websocket_auth.py` ~278-285), Chat ist bis zur Rotation tot.

Die vollständige Gefahrenliste (18 Punkte, mit Zeilen) steht in §12; sie ist aus dem Code abgeleitet, nicht vermutet.

## 3. Zielbild

- **Jede Person ein Konto**, Rolle aus dem bestehenden Satz: *Admin* (voll), *Familie* (`kb.shared`, `ha.full`, `cam.view`, `chat.own`, `rooms.read`, `speakers.own`, …), *Gast* (`docs/ACCESS_CONTROL.md:180-196`). Selbstregistrierung bleibt aus; Konten legt der Admin über `POST /api/users` an — ein `bin/`-Skript dafür gibt es nicht (§12 Nr. 17).
- **Identität aus zwei Quellen:** Login (Browser, PWA) und Stimme (Sprecher → `users.speaker_id`, eindeutig, `models/database.py:1197`). Die Sprechererkennung bleibt im Haushalt **an** — sie *ist* die Identität am Satelliten. Das ist eine bewusste Abweichung von der auth-on-Instanz, die sie abgeschaltet hat (D4 in `docs/design/browser-voice-auth-on-instances.md:35`, Art.-9-Gate); im Haushalt sind die Betroffenen die Mitglieder selbst, die Einwilligung ist Teil der Kontoanlage (D-6).
- **Geteilt wird über Stufen, nicht über Konten:** Haushaltswissen liegt bei einem Eigentümer auf Stufe 2 und ist für alle Mitglieder über `circle_memberships` erreichbar; Privates bleibt Stufe 0. Das ist das Modell aus `docs/CIRCLES.md`, das bisher nur nie eine zweite Person hatte.
- **Geräte sind keine Personen.** Satelliten und Kiosk bekommen **Gerätekonten** mit eng geschnittenen Rollen (§6), nicht das Admin-Konto.
- **Unterhaltungen bleiben persönlich** (eigentümergebunden, `chat.own`); Haushaltswissen fließt über Erinnerungen und den Wissensgraphen, nicht über geteilte Chatverläufe (§8.1).

## 4. Das Kernproblem: Datenbesitz neu verorten

Drei Klassen von Bestandsdaten, drei verschiedene Probleme:

### 4.1 Atome (Eigentümer 1, Stufe 0) — 16 389 von 16 775

Ohne Eingriff wird das gesamte Wissen beim Umschalten **nur für das Admin-Konto** sichtbar. Wem ein Atom „wirklich“ gehört, steht nirgends; eine Zuordnung je Person ist aus den Daten nicht herstellbar. Optionen:

| | A — Bestand = Haushaltswissen | B — Bestand bleibt Stufe 0 | C — je Quellklasse entscheiden |
|---|---|---|---|
| Vorgehen | alle Atome, die vor dem Cutover entstanden sind, auf **Stufe 2** heben (Eigentümer bleibt 1); jedes Mitglied bekommt bei Eigentümer 1 eine Mitgliedschaft Stufe 2 | nichts ändern | z. B. Dokumente/Fakten → 2, KG → 2, Erinnerungen → 0 |
| Sichtbarkeit nach Cutover | **wie heute** — jeder sieht, was heute jeder sieht | nur Admin sieht den Bestand | gemischt |
| Leckrisiko gegenüber heute | **keins** — heute ist alles für alle sichtbar; Stufe 2 reicht nicht über den Haushalt hinaus (Föderation sieht Stufe 2 nicht, `circle_sql.py:138-141`) | keins, aber Verlust | keins, aber Verlust je Klasse |
| Rückbau | Stufen zurück auf 0 (Skript), Mitgliedschaften löschen | — | wie A |
| Aufwand (Mensch / CC) | Skript mit `--dry-run`, per-Zeilen-Transaktion, über `AtomService` (Kaskade Entität → Relationen; nie direktes UPDATE, `docs/CIRCLES.md` Anti-Patterns) — ~1 Tag / ~1 h | 0 | Skript + Entscheidungsliste — ~2 Tage / ~2 h |

**Empfehlung A.** Sie ist die einzige Option, die den heutigen Zustand *erhält* statt ihn zu verschlechtern, und sie lässt jede spätere Verfeinerung zu: ein Mitglied kann einzelne Atome des Bestands nicht herabstufen (sie gehören Eigentümer 1), aber der Admin kann es, und alles **Neue** entsteht ab dem Cutover beim richtigen Eigentümer mit dessen Erfassungsvorgabe. Die 56 handgesetzten Stufen ≠ 0 bleiben unangetastet (sie sind Absicht). Die Erfassungsvorgabe (`circles.default_capture_policy`) jedes neuen Kontos wird bei der Anlage gesetzt (D-2 Zusatz: Vorgabe Stufe 2 „Haushalt“ oder Stufe 0 „privat“ — Empfehlung 2 für Familie, 0 für Gast).

### 4.2 Eigentümerlose Zeilen — 206 Unterhaltungen, 38 Relationen, 373 Dokumente in Wissensbasen ohne `owner_id`

- **`kg_relations` mit `user_id NULL` (38):** unter auth-on in keinem Zweig erreichbar → beim Backfill auf Eigentümer 1 setzen (sie hängen an Eigentümer-1-Entitäten) und mit 4.1 auf Stufe 2.
- **Wissensbasen 4–9 (373 Dokumente):** `knowledge_bases.owner_id NULL`; die Dokumente erreichen den Eigentümer heute nur über den Atom-Eigentümer-Rückfall (`circle_sql.py:89-98`, `:302-405`). Backfill: `owner_id = 1`; Dokument-Atome wie 4.1. Der Backfill schlüsselt auf **Dokumente**, nicht auf Atome (391 `kb_document`-Atome gegenüber 367 lebenden Dokumenten — Registry-Drift).
- **Unterhaltungen (206 NULL):** Unterhaltungen sind **keine Atome** (keine Stufe, kein `atom_id`; `models/database.py:29-91`; `api/routes/chat.py:511-530`). Sieben Lesepfade filtern auf `Conversation.user_id == asker` (`chat.py:292-303, 408-431, 475-505, 511-563, 442-455`; `conversation_service.py:344-353, 477-489`) — und der WS **adoptiert** eine eigentümerlose Unterhaltung durch den ersten authentifizierten Öffner (`conversation_service.py:488-489`): ein Zufallseigentümer. Optionen: (a) alle 206 dem Admin-Konto zuordnen („Archiv vor dem Cutover“, für andere unsichtbar); (b) NULL lassen (unsichtbar, bis jemand sie zufällig adoptiert); (c) die 45 mit `speaker_id` dem verknüpften Nutzer, den Rest dem Admin. **Empfehlung (c)** — und die Adoption in `conversation_service.py:488-489` vor dem Cutover **abschalten** (eigentümerlose Unterhaltung + auth-on → nicht adoptieren, sondern neue anlegen), sonst entscheidet der Zufall.

### 4.3 Der Kopplungs-Rest

Die eine Mitgliedschaft `(1,1,tier,2)` ist ein Self-Row aus einer alten Kopplung, deren Gegenseite nicht mehr existiert. Vor dem Cutover löschen; die neuen Mitgliedschaften entstehen aus 4.1 (n·(n−1) Zeilen für n Mitglieder, Stufe je Eigentümer — Empfehlung: jedes Familienmitglied bei jedem anderen Familienmitglied auf Stufe 2, Gäste auf 3 oder gar nicht).

## 5. Identität

- **Sprecher-Verknüpfung für alle Mitglieder** vor dem Cutover: Enrollment (`SPEAKER_CONTROLLED_ENROLLMENT_ENABLED=true` läuft im Haushalt, `configmap.yaml:469`) und `POST /api/users/{id}/link-speaker` (`api/routes/users.py:639-713`, UI in `UsersPage.tsx`). Heute 1 von 5 — das ist der Engpass für Sprach-Identität, nicht der Schalter.
- **Erkennungsschwelle:** die Beförderung Sprecher → Nutzer verlangt Konfidenz ≥ 0,7 (`chat_handler.py:1215-1219`); darunter bleibt der Zug anonym. Nach dem Cutover heißt anonym: Lesen nur Stufe 4, keine Erinnerung (#1260: Satelliten extrahieren ohnehin nur mit erkanntem Sprecher, `satellite_handler.py:119-141`), kein `device_action`. Deshalb §6.1.
- **`VOICE_AUTH_ENABLED`** (Login per Stimme, `api/routes/auth.py:844-868`) ist ein anderes Feature, im Haushalt aus, und wird für den Cutover nicht gebraucht.

## 6. Geräte und Dienste

### 6.1 Satelliten — Gerätekonto „Haushalt“

Nach dem Umschalten schließt `satellite_handler.py:316-319` jede Verbindung ohne Token (4001), und der eigene Token-Abruf der Satelliten (`satellite.py:695-718` → unauthentifiziertes `POST /api/ws/token`) bekommt 401 (`main.py:513-515`). Jeder Satellit braucht ein vorab ausgegebenes Token (`server.auth_token`, `renfield_satellite/config.py:330`, oder `RENFIELD_AUTH_TOKEN`, `:481`) und `auth_enabled: true` (`:47`) — eine **Flotteneinstellung mit zwei Quellen** (Ansible-Inventar + laufende Konfiguration; siehe Regel `satellites.md`), ausgerollt über die Provisionierung, **nie** per SSH-Handgriff (Pi-Zero-Karten).

Wessen Token? Optionen: (a) **ein Gerätekonto „Haushalt“** (Rolle: Familie ohne `chat`-Besitz-Relevanz, `ha.full`, `kb.shared`, keine Admin-Rechte), an das alle Satelliten gebunden sind — ein Zug ohne erkannten Sprecher läuft dann als dieses Konto: Gerätesteuerung funktioniert, Lesen reicht bis Stufe 2 des Haushaltswissens (über Mitgliedschaften des Gerätekontos), **Erinnerungen entstehen nicht** (das #1260-Tor bleibt: Extraktion nur bei erkanntem Sprecher — das Gerätekonto darf kein Gedächtnis ansammeln); ein erkannter Sprecher überschreibt das Gerätekonto durch die Beförderung; (b) **strikt** — kein Gerätekonto, unbekannte Stimme = Gast: keine Gerätesteuerung, kein Wissen. **Empfehlung (a).** Sie erhält den heutigen Nutzen des Sprachassistenten für alle, auch für Besucher, ohne dass ein anonymer Zug in die persönlichen Bereiche greift. Zu prüfen im Bau: die Beförderung in `chat_handler.py:1208-1235` gilt heute nur `if user_id is None` — mit Gerätekonto ist `user_id` gesetzt; die Beförderung muss ein Gerätekonto ausdrücklich überschreiben dürfen (Kennzeichen am Konto, nicht am Namen).

### 6.2 Kiosk — eigenes Konto und eigene Rolle

`kiosk_handler.py:421-435` verlangt `Permission.ADMIN`, `App.tsx:111-115` kapselt `/kiosk` in `<AdminRoute>`. Ein Wanddisplay mit stehender Admin-Sitzung ist die falsche Antwort. Empfehlung: Rolle **„Kiosk“** (`rooms.read`, Kiosk-WS, sonst nichts) und ein Gerätekonto; `kiosk_handler` und `App.tsx` prüfen eine Kiosk-Berechtigung statt `ADMIN`. Der Kiosk zeigt keine personenbezogenen Inhalte (Regel `kiosk.md`: inhaltsfreie Nutzlasten) — die Rolle darf entsprechend eng sein. Langlebige Sitzung: Refresh-Token-Laufzeit für das Kiosk-Konto (30 Tage heute) reicht mit automatischem Refresh; ein „nie ablaufendes“ Token wird nicht eingeführt.

### 6.3 Unverändert

HA-Webhook (eigenes Token, `notifications.py:97-121`), `/api/internal/auth/verify` (Voice-Server, eigenes Geheimnis), Ingest-Zugangsdaten `rfi.*` (eigenes Schema; nur ihre Admin-CRUD wird — richtigerweise — adminpflichtig, `ingest_credentials.py:150-241`), Föderation (Responder filtert immer, `federation_query_responder.py:313,505`). Meldungen und Scan-Ereignisse werden vom Broadcast zu eigentümergerichtet (`user_events.py:116`, `scanner_jobs.py:404`) — gewollt; `/ws/user` verlangt eine Nutzeridentität (`user_events_handler.py:56-59`) — das Gerätekonto deckt den Kiosk-Fall nicht (Kiosk nutzt `/ws/kiosk`).

## 7. Konfiguration des Schalters

Der Kommentarblock in `k8s/configmap.yaml:36-44` beschreibt die sechs Schlüssel bereits richtig; sie werden **gemeinsam** in einem Commit gesetzt:

| Schlüssel | heute | Cutover | Grund |
|---|---|---|---|
| `RENFIELD_ENV` | `development` | `production` | schärft den `SECRET_KEY`-Wächter; Voraussetzung: starker Schlüssel liegt in `renfield-secrets` (ist er — MultiFernet #1297 läuft damit) |
| `AUTH_ENABLED` | `false` | `true` | der Schalter |
| `ALLOW_REGISTRATION` | `false` | `false` (gesetzt lassen!) | #1295 bricht sonst den Start |
| `CORS_ORIGINS` | `*` | `https://renfield.local` | `*` schaltet den CSWSH-Schutz des WS ab (`websocket_auth.py` ~127-131); Cookie-Modus verweigert `*` |
| `TRUSTED_PROXIES` | `""` | Traefik-Pod-CIDR | sonst degradiert die Anmeldesperre auf „nur Nutzername“ (#1296; `docs/SECURITY.md:131`) |
| `API_RATE_LIMIT_STORAGE_URI` | `memory://` | `redis://redis:6379` | geteilte Limits, sobald >1 Backend-Pod |

`AUTH_COOKIE_ENABLED` bleibt beim Cutover **aus** und folgt als zweiter Schritt nach `docs/runbooks/cookie-auth-flag-flip-xidra.md` (Vorflug §0 dort gilt unverändert; der Haushalts-ConfigMap-Pfad ist git → `kubectl apply -f k8s/configmap.yaml`, kein Merge-Patch). Weitere Schlüssel: `MEMORY_SUBSUME_TO_KG` (§8.2), `SPEAKER_RECOGNITION_ENABLED` bleibt an (§3).

## 8. Die vier eingebrachten Posten

### 8.1 BL-0141 — Sichtbarkeitsstufe für Unterhaltungen: **nicht bauen**

Unterhaltungen tragen weder Stufe noch Atom; eine Stufe würde sieben Lesepfade und die Nachrichtensuche auf `circle_sql` umstellen (§4.2). Der Bedarf („shared-private Chat“) war nie belegt, und das Modell des Zweiten Gehirns teilt **Wissen** (Erinnerungen, Graph, Dokumente), nicht **Verläufe**. Empfehlung: Unterhaltungen bleiben `chat.own`; wer etwas teilen will, tut es über die Stufe der daraus extrahierten Erinnerung. Vermerk in der Roadmap: gestrichen, nicht vertagt.

### 8.2 BL-0255 — Subsume mehrnutzerfähig: **vor dem Cutover, sonst abschalten**

`MEMORY_SUBSUME_TO_KG=true` läuft im Haushalt (`configmap.yaml:417`). Das Signal ist **namensbasiert** (`captured_kg_subjects`, `conversation_memory_service.py:603-606, 700-757`), der Rückfall löst „eigene oder eigentümerlose“ Entitäten ohne Tier-Reichweite auf (`:759-800`, `:741-744`): in einer Mehrnutzer-Datenbank kann ein Fakt verworfen werden, weil der Graph eine gleichnamige Entität eines **anderen** Nutzers kennt. Es gibt keinen Validator, der die Kombination verbietet. Zwei Wege: (a) **vorher mehrnutzerfähig bauen** — Subjekt-Auflösung je Frager durch `kg_entities_circles_filter`, das erfasste Set nach Entitäts-ID *und* Eigentümer geschlüsselt statt nach Name, und dasselbe Tor im v2-Pfad (BL-0421: v2 umgeht das Subsume-Tor, `:318-325`); (b) **am Cutover abschalten** und bis (a) mit doppelter Speicherung leben. Empfehlung: (a) als Bauposten *vor* dem Cutover (Größe M), Rückfall (b) im Runbook, falls (a) nicht rechtzeitig steht. Zusätzlich ein Validator: `MEMORY_SUBSUME_TO_KG=true` + `AUTH_ENABLED=true` bricht den Start, solange (a) nicht gemergt ist — das macht die Prosa-Grenze aus TODOS.md:575 hart.

### 8.3 BL-0302 / #876 — erledigt, Linker nach diesem Entwurf

Spike gemergt (#1302): Co-Referenz statt Shared-Ownership. Für den Haushalt braucht es **keinen** Linker: nach 4.1 gibt es *einen* Eigentümer für den Bestand und Mitgliedschaften für alle; neue persönliche Knoten derselben Person entstehen erst, wenn Mitglieder getrennt über dieselben Personen sprechen — dann greift derselbe Linker wie auf der auth-on-Instanz (E-1: nach diesem Entwurf).

### 8.4 BL-0305 — F-ID-2 Double-Login-Consent: **durch diesen Cutover freigeschaltet**

P0 der personenbezogenen Föderation ist genau dieser Cutover (`docs/design/federation-identity-mapping.md:52,115,263`). Der Bauplan steht (§9 dort: offer → accept → complete, beide Seiten signiert, keine Halblinks; Routen, Zustandsautomat, UI und Tests fehlen — `federation_user_links.py:68-122` ist Admin-CRUD). Zwei Entscheidungen aus §7 dort werden hier festgehalten: `querier_ref` wird je `(peer, user)` geprägt, nie global; das **Ehren eingehender Links ist ein Schalter je Peer und je Richtung** (neue Spalte an `peer_users`), Vorgabe für den Haushalt: eingehend **aus**, ausgehend an — die Person kartiert sich vom persönlichen auf das geschäftliche Gerät, nicht umgekehrt (D-9). Nach dem Cutover verliert außerdem die Peer-Scope-Begründung „Haushalt = ein Nutzer“ (`docs/CIRCLES.md:38`, `docs/SECOND_BRAIN.md:139`) ihre Prämisse; die dort notierte `remote_user_id`-Kollision bei ≥ 2 Peers ist vor dem ersten Personenlink zu schließen (F-ID-3).

## 9. Reihenfolge — jede Stufe für sich rückbaubar

| Stufe | Inhalt | Rückweg |
|---|---|---|
| **P0 Vorbereitung (Code)** | Kiosk-Rolle + Gerätekonto-Kennzeichen (§6); Adoption eigentümerloser Unterhaltungen abschalten (§4.2); Subsume mehrnutzerfähig oder Validator (§8.2); Backfill-Skript `bin/backfill_household_tiers.py` (`--dry-run`, per-Zeilen-Txn, über `AtomService`); Satelliten-Token in der Provisionierung; Tests §11 | gewöhnliche PRs, alle dunkel |
| **P1 Konten (noch auth-off)** | Konten für alle Mitglieder anlegen (`POST /api/users`), Rollen, Erfassungsvorgabe, Sprecher-Enrollment + Verknüpfung; Gerätekonten Haushalt + Kiosk | Konten sind unter auth-off wirkungslos |
| **P2 Backfill (noch auth-off)** | Dry-Run mit Zählwerten → Freigabe → Commit: Stufen (§4.1), NULL-Relationen, KB-Eigentümer, Unterhaltungen (§4.2), Kopplungs-Rest, Mitgliedschaften (§4.3) | unter auth-off ohne Wirkung auf Lesepfade; Skript kennt `--revert` |
| **P3 Umschalten** | Satelliten-Token ausrollen (Flotte); ConfigMap-Commit mit den sechs Schlüsseln; Rollout; Admin-Passwortrotation; Abnahme §11 | `AUTH_ENABLED=false` (+ `RENFIELD_ENV` zurück) → Lesepfade byte-identisch zu heute; Satelliten mit Token verbinden auch unter auth-off (`/api/ws/token` liefert dann `None`, Token wird ignoriert — zu verifizieren in P0) |
| **P4 Cookie** | `AUTH_COOKIE_ENABLED` nach dem xidra-Runbook | Flag zurück, ~1 min |
| **P5 danach** | Subsume (falls in P0 nur der Validator kam), F-ID-2, Linker (E-1) | eigene Posten |

## 10. Rollback

Der Schalter zurück auf `false` stellt jeden Lesepfad byte-identisch wieder her (die Kurzschlüsse in §2 Nr. 2). Die Datenänderungen aus P2 sind unter auth-off **inert** (Stufen und Mitgliedschaften werden nicht ausgewertet) und bleiben liegen; `--revert` stellt die Stufen wieder her, falls gewünscht. Konten, Sprecherverknüpfungen und Gerätekonten sind harmlos. Was **nicht** rückbaubar ist: Erinnerungen, die zwischen Umschalten und Rückbau bereits beim richtigen Eigentümer entstanden sind — sie bleiben diesem zugeordnet und werden unter auth-off wieder für alle sichtbar (das ist der heutige Zustand, kein Leck).

## 11. Tests und Abnahme (Pflicht vor P3)

1. **Vier-Zweig-Filter mit ≥ 2 Nutzern auf echtem Postgres** (`renfield_test`): Mitglied sieht Stufe-2-Zeilen des Eigentümers, nicht Stufe 0; Nicht-Mitglied nur Stufe 4; Grant überschreibt; je Retrieval-Verbraucher (RAG, KG, Memory, Notizen, Fakten, Graph-Expansion). Es gibt keinen Live-Beleg, dass die Tier-Reichweite je gearbeitet hat — dieser Cutover wäre der erste.
2. **Backfill-Skript:** Dry-Run-Zählwerte = Commit-Zählwerte; Kaskade Entität → Relationen; `--revert` stellt her; Idempotenz.
3. **Gerätekonto:** anonymer Satelliten-Zug steuert Geräte, liest Stufe 2, schreibt **keine** Erinnerung; erkannter Sprecher überschreibt das Gerätekonto.
4. **Kiosk-Rolle:** Kiosk-Konto öffnet `/ws/kiosk` und `/kiosk`, sonst nichts (Admin-Routen 403).
5. **Satellit mit Token** verbindet unter auth-on **und** auth-off (Rückweg).
6. **Adoption aus:** eigentümerlose Unterhaltung wird unter auth-on nicht adoptiert.
7. **Boot-Validatoren:** die sechs Schlüssel zusammen booten; jede Einzelabweichung bricht laut (`test_config_auth_consistency.py` erweitern).
8. **Browser-E2E nach P3** (smoke-tester): Login, Passwortrotation, Chat, Wissen, Kiosk, ein Sprachzug am Satelliten mit und ohne erkannten Sprecher — Rückweg geprobt.

## 12. Gefahrenliste (aus dem Code, nummeriert)

1. Start verweigert ohne starken `SECRET_KEY` — `config.py:2051-2081`. 2. Start verweigert, wenn `ALLOW_REGISTRATION` nicht gesetzt — `:2183-2188`. 3. Alle außer Admin verlieren den Bestand — `atom_owner.py:41`, Filter §2. 4. WS-Zeilen tragen `user_id NULL`, aus den Daten nicht zuordenbar — `websocket_auth.py:173-174`, `chat_handler.py:1190`. 5. Alle Satelliten trennen sich — `satellite_handler.py:316-319`, `main.py:513-515`. 6. Kiosk geht aus — `kiosk_handler.py:421-435`, `App.tsx:111-115`. 7. Unerkannte Sprecher werden still nicht mehr erinnert — `conversation_memory_service.py:1114,1907,2262`. 8. Bootstrap-Admin muss rotieren, WS lehnt bis dahin ab — `auth_service.py:741`, `ProtectedRoute.tsx:46-48`. 9. Anmeldesperre ohne `TRUSTED_PROXIES` nur je Nutzername — `docs/SECURITY.md:131`. 10. `CORS_ORIGINS=*` schaltet den WS-Origin-Schutz ab — `websocket_auth.py` ~127-131. 11. `internal.*`-Tore schalten alle zugleich scharf — `kb_maintenance_tool.py:608,1116`, `system_health_tool.py:197`, `paperless_reextract_tool.py:142`. 12. `device_action` ohne Identität abgelehnt — `chat_handler.py:1006-1013`. 13. `/ws/user` verlangt Nutzeridentität — `user_events_handler.py:56-59`. 14. Meldungen/Scan-Ereignisse werden eigentümergerichtet — `user_events.py:116`, `scanner_jobs.py:404`. 15. Ingest-CRUD wird adminpflichtig — `ingest_credentials.py:150-241`. 16. Peer-Scope-Begründung verliert die Prämisse — `docs/CIRCLES.md:38`. 17. Kein Skript legt Nutzer an — `auth_service.py:711-714`. 18. Frontend-Pseudo-Admin `id 0` verschwindet — `AuthContext.tsx:250-260`.

## 13. Entscheidungen für den Eigentümer

- **D-1 Grundsatz:** auth-on als geplantes Projekt (Empfehlung) · später mit benanntem Auslöser · nein (Einzel-Vertrauensdomäne bleibt; F-ID-2, Cookie, Personenlinks im Haushalt dauerhaft gegenstandslos).
- **D-2 Bestand:** A Haushaltsstufe 2 (Empfehlung) · B Stufe 0 · C je Klasse. Zusatz: Erfassungsvorgabe neuer Konten — Familie 2 / Gast 0 (Empfehlung).
- **D-3 Unterhaltungen:** (c) Sprecher-verknüpfte an den Nutzer, Rest an den Admin, Adoption aus (Empfehlung) · (a) alle an den Admin · (b) NULL lassen. BL-0141 streichen (Empfehlung).
- **D-4 Satelliten:** Gerätekonto „Haushalt“ mit `ha.full`, ohne Gedächtnis (Empfehlung) · strikt.
- **D-5 Kiosk:** eigene Rolle + Gerätekonto (Empfehlung) · stehende Admin-Sitzung.
- **D-6 Sprechererkennung:** im Haushalt an, Einwilligung bei Kontoanlage (Empfehlung) · aus wie auf der auth-on-Instanz (dann keine Sprach-Identität, Satelliten laufen nur als Gerätekonto).
- **D-7 Subsume:** vorher mehrnutzerfähig (Empfehlung) · am Cutover abschalten.
- **D-8 Cookie:** zweiter Schritt (Empfehlung) · zusammen mit dem Cutover.
- **D-9 F-ID-2 Richtung:** eingehend aus / ausgehend an (Empfehlung) · beide an · beide aus.

## 14. Was danach entsteht

Bei Go zu D-1: `docs/runbooks/auth-on-cutover-household.md` (Vorflug wie §7, P1–P3 als Kommandofolge, Abnahme §11, Rückweg §10) und die P0-Bauposten (Kiosk-Rolle, Gerätekonto-Kennzeichen, Adoption aus, Subsume/Validator, Backfill-Skript, Satelliten-Token in der Provisionierung, Filter-Tests) als einzelne PRs — jeder dunkel, jeder für sich reviewbar.
