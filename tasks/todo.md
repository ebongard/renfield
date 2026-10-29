# Stufe 3 — Dokumentverweise über die Instanzgrenze

*Entwurf: `docs/design/cross-instance-document-reference.md` §3.3/§3.4 · geplant 2026-10-29*

## ✅ Stufe 2 ist vollständig (2026-10-29 09:27)

Die Personenverknüpfung steht auf beiden Seiten, und zwar als EIN Vorgang:

| | Haushalt | x-idra |
|---|---|---|
| link | id=2, peer=3 | id=1, peer=2 |
| gebunden an | lokales Konto **2** | lokales Konto **107** |
| `querier_ref` | `3012b21c63…` | `3012b21c63…` |

🛑 Der identische `querier_ref` ist der Beleg: er wird im Handschlag EINMAL geprägt und vom
Angebot mitgeführt. Zwei unabhängige Versuche hätten zwei Merkmale ergeben. Gebunden ist das
EIGENE Konto, nicht `admin` — als `admin` hätte der Peer mit dessen Reichweite geantwortet.

Zuvor stand hier „0 Zeilen, blockierend". Richtig war: gelungen war der **Paarungs**-, nicht
der **Verknüpfungs**-Handschlag. Zwei Vorgänge, derselbe Name, verschiedene Tabellen.

🛑 **Die Blockade ist jetzt Schicht 4** (unten), nicht die Verknüpfung. Und die Begründung des
Entwurfs, die Instanz-Stufe sei für Dokumente zu grob, bleibt eine **Entwurfsaussage**: sie
ist weiterhin nicht gemessen, weil über die Grenze noch nie eine Anfrage ging
(`federation_query_log` = 0 auf beiden Instanzen, seit immer).

## Stand der Bausteine (gemessen, nicht vermutet)

| Baustein | Stelle | Stand |
|---|---|---|
| `INSTANCE_ID` | beide Instanzen | ✅ `household` / `xidra` |
| Paarung | `peer_users` | ✅ je 1 aktiv, tier 2 |
| `FEDERATION_IDENTITY_LINKS_ENABLED` | beide | ✅ `true` |
| Personenverknüpfung | `federation_user_links` | ✅ je 1 Zeile, `querier_ref` identisch |
| Rechteprüfung | `knowledge.py:164 check_document_access` | ✅ vorhanden |
| Identitätsauflösung | `resolve_linked_user`, `_peer_scope_for` | ✅ vorhanden, mit Aufrufern |
| Transport | `query_brain/{initiate,retrieve}` | ⚠️ **nicht generisch** |
| `peer.document_status` / `_fetch` | — | ❌ nicht gebaut |
| Rolle darf den Peer sehen | `agent_roles.yaml` + `agent_tools.py:247` | ❌ **Schicht 4, blockierend** |

🛑 **Der Transport trägt keine Art-Kennung.** `QueryBrainInitiateRequest` ist auf `query`
zugeschnitten, und die Signatur deckt genau die benannten Felder. Ein `kind`-Feld
nachzurüsten würde ändern, *was* auf dem bestehenden Pfad signiert wird — also ein
**zweiter** Umschlag, der die Maschinerie teilt, nicht ein erweiterter erster.

## Die Arbeit

### 1. Zweiter signierter Umschlag
`PeerDocumentRequest`: `version`, `asker_pubkey`, `instance`, `document_id`, `querier_ref`,
`nonce`, `timestamp`, `depth`, `path` + `signature` über das kanonische JSON aller
Nicht-Signatur-Felder. **Geteilt** wird: Signier-/Prüfhelfer, Nonce-Speicher samt
±60-s-Fenster, `_lookup_peer`, `resolve_linked_user`, `_peer_scope_for`.
Ein Strukturtest hält fest, dass beide Umschläge **denselben** Signierhelfer benutzen —
zwei Kopien kanonischer JSON-Kodierung sind die Stelle, an der Signaturen auseinanderlaufen.

### 2. Antwortseite — `POST /peer/document/status`
Löst `querier_ref` auf die lokale Person auf und ruft `check_document_access` mit **dieser**
Identität, nicht mit der Instanz-Identität. Antwort: `{status, in_paperless, filed_at}`.
🛑 **Fail-closed bei fehlender Abbildung:** ist der Satz nicht `mapped`, wird abgewiesen,
nicht auf `PeerScope` zurückgefallen. Für einen Dokumentzugriff ist die Instanz-Stufe zu
grob — dieselbe Begründung wie im Entwurf.
Uniform 400 wie der bestehende Pfad, kein Orakel über Signatur/Nonce/Zeitfenster.

### 3. Fragerseite — `GET /api/knowledge/documents/{instance}/{id}`
Eigene Instanz → delegiert intern an `GET /documents/{id}`. Fremde → Peer-Anfrage.
**Dieselbe Antwortform** wie der lokale Endpunkt, aber die Paperless-Adresse der Gegenseite
wird entfernt, nicht durchgereicht. Löst nebenbei die LAN-Frage: die Gegenseite muss nur von
der eigenen Instanz erreichbar sein, nicht vom Gerät der Person.

### 4. Oberfläche
`DocumentRefArtifact`: bei gesetztem `instance` den qualifizierten Endpunkt abfragen statt
`enabled: false`. Aus dem Hinweis „liegt in … — noch nicht abrufbar" wird ein Knopf. Der
`remote`-Zweig und sein Kommentar entfallen; der Grund, der dort steht (eigene Zählfolgen je
Instanz), bleibt als Begründung der **Qualifizierung** erhalten.

### 5. Prüfprotokoll
Siehe offene Entscheidung D2.

### 6. Tests (echtes Postgres)
- Nicht abgebildete Person → **abgewiesen**, nicht auf Instanz-Stufe beantwortet.
- Fremde Instanz → wird **nie** lokal aufgelöst (die Falle, die Stufe 1 abstellt).
- Signatur falsch / Nonce wiederholt / Zeitstempel außerhalb → abgewiesen, uniform.
- `check_document_access` greift mit der **abgebildeten** Identität: dieselbe Anfrage als
  zwei verschiedene verknüpfte Personen sieht Verschiedenes.
- Negativkontrolle zu jedem Riegel: erst zeigen, dass er beim Nachbarfall **nicht** feuert.

## Offene Entscheidungen

**D1 — `document_status` allein, oder zusätzlich `document_fetch`?**
Status heißt: der Knopf sagt „dort abgelegt" und nennt den Zustand. Fetch heißt: die Bytes
kommen durch die eigene Instanz, das Dokument öffnet im eigenen Browser — mit
Zwischenspeicherung, Protokollierung und einem Durchsatzpfad, den es heute nicht gibt.

**D2 — Prüfprotokoll in `FederationQueryLog` oder eigene Tabelle?**
`FederationQueryLog` deckt Atom-Abfragen. Ein Dokumentabruf ist etwas anderes; dieselbe
Tabelle spart eine Migration, eine eigene hält die Auswertung sauber.

## Was dieser Plan NICHT vorschlägt
- **Keinen Deeplink** in die Paperless-Oberfläche der Gegenseite (Entwurf §7).
- **Kein `kind`-Feld** im bestehenden Umschlag — das änderte, was auf dem laufenden Pfad
  signiert wird.
- **Kein Zurückfallen auf die Instanz-Stufe**, wenn die Verknüpfung fehlt.

---

# Schicht 4 — eine Rolle muss den Föderationspeer sehen dürfen

*Gefunden 2026-10-29 beim Gegenmessen der Verknüpfung · liegt VOR Stufe 3*

## Der Befund

Die Verknüpfung steht (beide Seiten, `querier_ref` identisch, gebunden an Konto 2 / 107).
Trotzdem überschritt die Frage „Welche Dokumente sind in der x-idra instanz heute angelegt
worden?" die Grenze nicht: `federation_query_log` zählt auf **beiden** Instanzen 0 Zeilen —
nicht seit gestern, sondern **überhaupt nie**.

Gemessen am Zug vom 09:32:

    Router  -> Rolle `documents`
    Vorauswahl: 4/33 -> ingest_status, list_unfiled_documents,
                        paperless.get_document, paperless.search_documents

`mcp.peer_3.query_brain` ist nicht dabei, und die Gesamtzahl ist unverändert **33**.

Ursache, zwei Zeilen:

| Stelle | Inhalt |
|---|---|
| `config/agent_roles.yaml` | `documents: mcp_servers: [paperless, email, scanner]` |
| `agent_tools.py:247` | `if server_filter is not None and mcp_tool.server_name not in server_filter: continue` |

🛑 **Exakter Namensvergleich gegen einen Namen, der erst beim Paaren entsteht.**
`FEDERATION_SERVER_PREFIX = "peer_"`, der Server heißt `peer_<id>` — er lässt sich nicht in
eine Konfiguration schreiben, die vorher existiert. Nur `general` (`mcp_servers: null`) sieht
ihn je, und Dokumentfragen landen bei `documents`.

Dieselbe Klasse wie die Zwei-Schritt-Falle für `internal.*` in CLAUDE.md
(„Registrierung UND Eintrag in die Rolle"), nur schärfer: der zweite Schritt ist hier
**unmöglich**. Ergebnis ist ein korrekt gebauter Kanal ohne Aufrufer.

## Reichweite der Änderung (gemessen)

`server_filter` hat genau **zwei** Aufrufer, beide reichen `role.mcp_servers` durch dieselbe
Funktion: `chat_handler.py:2268` und `orchestrator.py:536`. Eine Änderung in
`_register_mcp_tools` deckt beide. `intent_registry` hängt nicht daran. Nur `general` trägt
`null`.

## Die drei Formen

### F1 — ein erkannter Eintrag `peer_*` in der Positivliste
Der Filter lässt ein Werkzeug zu, wenn `server_name in server_filter` **oder** der Eintrag
`peer_*` in der Liste steht und `server_name.startswith("peer_")`. Kein allgemeines Globbing,
ein einziges erkanntes Zeichen. `documents` bekommt `peer_*` dazu.

- ✅ **Eine** Stelle beantwortet „welche Server sieht diese Rolle" — und das ist in diesem
  Projekt wiederholt der Unterschied zwischen tragend und driftend gewesen.
- ✅ Je Rolle ausdrücklich: `smart_home` bekommt den Peer nicht, nur weil es ihn gibt.
- ✅ Kleinste Änderung: eine Funktion, eine Konfigurationszeile, zwei Aufrufer gedeckt.
- ❌ Führt ein Muster in eine Liste ein, die bisher exakt verglich. Ein Leser könnte
  annehmen, `paperles*` ginge auch. Braucht einen Test, der festhält: **nur** `peer_*` wirkt,
  jedes andere Sternchen bleibt ein gewöhnlicher Name.

### F2 — ein Rollen-Flag `federation: true`
Neues Feld an `AgentRole` und in der YAML; der Filter lässt `peer_`-Server zu, wenn es gesetzt
ist. Vorgabe `false`.

- ✅ Die Absicht steht als Wort da, nicht als Zeichenmuster — greppbar, nicht zu verwechseln.
- ✅ Kein Muster in der Positivliste, der exakte Vergleich bleibt exakt.
- ✅ Kann später mehr tragen (etwa *welche* Peers eine Rolle sehen darf).
- ❌ **Zwei** Stellen beantworten dieselbe Frage: `mcp_servers` und `federation`. Wer künftig
  wissen will, was eine Rolle sieht, muss beide lesen — genau die Zweiteilung, die in dieser
  Sitzung dreimal etwas gekostet hat.
- ❌ Neues Feld in Doku, Schema und Tests.

### F3 — instanzbezogene Fragen an `general` routen
Keine Änderung am Filter; der Router schickt Fragen, die eine andere Instanz benennen, an
`general`, das ohnehin alles sieht.

- ✅ Kein Eingriff in Filter oder Konfiguration.
- ❌ **Verschiebt das Problem.** `general` trägt ALLE Werkzeuge — die Vorauswahl steht dann
  vor 33+ statt vor einer gezielten Auswahl, und genau an dieser Fläche liegen die Fehlgriffe
  dieser Woche.
- ❌ Die Dokumentfrage verliert die Hinweise der `documents`-Rolle, die sie erst tragen.
- ❌ Eine Router-Prompt-Änderung hat **keinen Prüfstand** (anders als die Vorauswahl mit
  `bin/measure-preselect.py`) — sie wäre geraten, nicht gemessen.
- ❌ Jede andere Rolle bleibt föderationsunfähig.

## Architektur-Review (`architecture-guide`, 2026-10-29) — zwei Korrekturen

🛑 **Zwei weitere Konsumenten von `role.mcp_servers`, beide exakt vergleichend**, von mir
übersehen und am Code nachgeprüft:

| Stelle | Inhalt | Folge |
|---|---|---|
| `agent_router.py:181-192` | `_filter_available_roles`: `any(server in connected_set …)` | `connected_set` enthält Peers, weil `peer_mcp_registry.py:133` `connected=True` setzt — im eigenen Kommentar als „a lie — there's no MCP session" bezeichnet. Ein Literal `peer_*` greift hier nicht. Heute harmlos (`documents` hat reale Server), latent: fallen die aus, während der Peer steht, gilt die Rolle fälschlich als nicht verfügbar. |
| `agent_router.py:260-271` | `role_for_intent`: `if role.mcp_servers and server in role.mcp_servers` | `None` ist falsy → `general` ist **nie** Kandidat. Für `mcp.peer_3.query_brain` kommt `None` zurück: die Korrektur „das war eine Föderationssache" bleibt wirkungslos. **Vorbestehender Fehler, den KEINE der Formen nebenbei behebt.** |

`kiosk_handler.py:345` ist nur Admin-Anzeige, `filter_agent_tools` (`utils/hooks.py:285`) eine
Plugin-Erweiterungsstelle ohne Handler im Kern — beide nicht betroffen.

🛑 **Sicherheitskorrektur — ein Argument von mir fällt.** Ich hatte gefragt, welche Form
unabsichtlichen Zugriff am schwersten macht. Die Zugriffskontrolle sitzt aber beim
**antwortenden** Peer, zirkel-skaliert gegen die aufgelöste verknüpfte Identität; welche
lokale Rolle gefragt hat, ändert die Antwort nicht. Der Rollenfilter ist **nicht** die
Sicherheitsgrenze. Was wirklich auf dem Spiel steht: die Frage im Freitext verlässt unnötig
die Instanz, Rate-Limit, Prompt-Verschmutzung.

## F2′ — die empfohlene Form (vom Review, nicht von mir)

Bool-Flag `federation` **plus** Erkennung über `MCPServerConfig.transport == FEDERATION`
(existiert, `peer_mcp_registry.py:103`) statt über das Namenspräfix.

- ✅ Kein Abgleich zwischen zwei getrennt gepflegten Dateien; bindet an ein Enum, nicht an
  eine Zeichenkette, die woanders definiert wird.
- ✅ Kein Kollisionsrisiko mit einem künftigen echten Server namens `peer_etwas`.
- ✅ Im Review leicht zu sehen („hat die Rolle `federation: true`?") — ein `peer_*` zwischen
  zehn Servernamen übersieht man.
- ❌ **Von mir nachgemessen:** `MCPToolInfo` (`mcp_client.py:975-984`) trägt `server_name`,
  `original_name`, `namespaced_name`, `description`, `input_schema`, `hint` — **keinen
  Transport**. Der sitzt an `state.config.transport`, ein öffentlicher Zugang existiert
  nicht. F2′ braucht daher ein Prädikat am Manager (`is_federation_server(name)`), sonst
  greift der Filter in privaten Zustand. Eigener Posten, kein Gegenargument: es benennt den
  Begriff einmal an einer Stelle.

🛑 **Das Argument, das F1 bei mir gewonnen hätte, kippt:** F1 müsste Präfix-Vergleich an
**drei** Stellen einführen statt an einer, und vergrößert damit gerade die Driftfläche, die
es verkleinern sollte. Dazu eine Nebenwirkung: `role_for_intent` sortiert nach
`len(role.mcp_servers)` — `peer_*` zählt als ein Eintrag und verschiebt die Spezifitätsregel.

**Prompt-Last:** F1/F2/F2′ gleichwertig (nur Opt-in-Rollen sehen den Peer). Die vierte Form
(Peers generell ausnehmen) verschlechtert sie in **jeder** Rolle — Föderations- und
`internal.*`-Werkzeuge tragen laut `.claude/rules/agent-loop.md` **keinen** `hint`, ihre
ungekürzte Beschreibung geht voll in den Vorauswahl-Prompt. Sie gibt außerdem fail-closed auf:
es gäbe keinen Hebel mehr, eine Rolle vom Kanal auszuschließen.

## Umsetzungsstellen (jede Form)
`agent_tools.py:247` · `agent_router.py:181-192` · `agent_router.py:260-271` ·
bei F2′ zusätzlich ein Prädikat an `MCPManager` (`mcp_client.py`, bei `get_connected_server_names`).

## Tests zusätzlich
- Der vorbestehende `role_for_intent`-Fehler: eine Korrektur auf ein `mcp.peer_*`-Intent
  findet heute **keine** Rolle. Negativkontrolle: für `mcp.paperless.*` findet sie eine.
- Rollenverfügbarkeit: steht nur der Peer und sonst nichts, gilt eine föderationsfähige
  Rolle als verfügbar.

## Tests (zu jeder Form)
- Ein `peer_3`-Werkzeug erscheint für eine zugelassene Rolle und **nicht** für `smart_home`.
- Negativkontrolle: ohne die Zulassung ist es auch bei der zugelassenen Rolle weg — sonst
  prüft der Test nur, dass Werkzeuge überhaupt ankommen.
- Bei F1 zusätzlich: ein Eintrag `paperles*` wirkt **nicht** als Muster.
- Die Zahl im Protokoll steigt von 33 auf 34 — die Messung, die den Befund fand.

---

# Schicht 5 — der Instanzname muss beim Paaren mitkommen

*Gefunden 2026-10-29 beim Messen der Peer-Beschreibung · getrennt von Schicht 4*

## Der Befund, gemessen

Nach der Textfassung V2 trifft die Vorauswahl 36/39 (92 %, vorher 27/39). Der EINE
verbleibende Ausfall ist genau die ursprünglich gemeldete Frage:

    „Welche Dokumente sind in der x-idra instanz heute angelegt worden?"   0/3

🛑 Er widersteht **drei verschiedenen** Dokumentformulierungen (V3 und V4 gemessen, beide
92 %, kein Zugewinn). Es ist also **keine** Vokabellücke im Text. Was fehlt, ist der
INSTANZNAME selbst — „x-idra" steht nirgends, weil:

| Stelle | Befund |
|---|---|
| `peer_users` | trägt **keine** entfernte `INSTANCE_ID` — nur `display_name`, `pubkey`, `remote_user_id` |
| `pairing_service` | das Angebot führt `INSTANCE_ID` **nicht** mit |
| `remote_display_name` | ist `evdb`, also der **Personen**name der Gegenseite, nicht der Instanzname |

Daraus folgt eine Begriffslücke durch das ganze System: Scan-Ziele heißen nach der INSTANZ
(`xidra`), die Instanzgrenze sagt „liegt in xidra", die Kopplung ist nach der PERSON benannt
(`evdb`) — und nichts verbindet beides.

## Die Arbeit
1. `INSTANCE_ID` in das Paarungsangebot und die Antwort (beide signiert — das Feld geht
   also in den signierten Umschlag, nicht daneben).
2. Spalte `remote_instance_id` an `peer_users`, Migration, nullable (bestehende Paarungen
   haben sie nicht und sollen nicht brechen).
3. Die Beschreibung nennt beides: Person UND Instanz. **Vorher und nachher messen** —
   mehr Text kann schlechter messen, zweimal belegt.
4. Fail-safe: fehlt die Instanzkennung (alte Paarung), bleibt die heutige Fassung.

🛑 Ohne Punkt 4 wäre das eine Verschlechterung für jede bestehende Paarung.

---

# Schicht 6 — ~~ein Hinweis-Kanal für Peer-Werkzeuge~~ GEMESSEN WIDERLEGT

🛑 **Dieser Plan ist am 2026-10-29 durch Messung erledigt — und zwar negativ.**
Ein Hinweis geht ungekürzt mit, kostet also dieselbe Länge wie das Aufheben der Kappung.
Und das Aufheben kostete 7 Punkte in der Peer-Sammlung (92 % → 85 %) und brach die
Negativkontrolle „suche nach Rechnungen" (3/3 → 0/3): eine LOKALE Suche wurde zur
föderierten Anfrage. Ein Hinweis-Kanal hätte dasselbe getan, nur aus einer anderen Datei.

Gelöst ist es stattdessen durch **`_PRESELECT_DESC_BUDGET`** — eine Grenze je Werkzeug mit
Obergrenze auf der SUMME (+589, die gemessene Kante). Peer 92 %, Scanner 95 %, beides.

Der ursprüngliche Text bleibt unten als Begründung stehen, warum der Weg verlockend aussah.

---

## Verworfen: ein Hinweis-Kanal für Peer-Werkzeuge

*Gefunden 2026-10-29 · getrennt von Schicht 4 und 5*

## Der Befund

Die Beschreibung eines Peer-Werkzeugs wird bei `_PRESELECT_DESC_CHARS` (480) **gekappt**.
Der Betreiber-Hinweis aus `mcp_servers.yaml` geht danach **ungekürzt** mit — bei
`scan_document` sind das 1760 Zeichen. Peer-Werkzeuge haben diesen Kanal **nicht**: sie
entstehen in `peer_mcp_registry`, nicht aus der Konfiguration.

Folge, heute schon spürbar: V3 wollte 499 Zeichen und wäre abgeschnitten worden — mitten im
Scanner-Abgrenzungssatz, also genau an der Stelle, die „scanne nach x-idra" beim Scanner
hält. Ich musste kürzen, nicht weil der Text zu lang war, sondern weil dem Peer der Kanal
fehlt, in dem er lang sein dürfte.

🛑 Dazu die Falle, die der Kappungstest jetzt bewacht: der Anzeigename kommt von der
GEGENSEITE (`offer.display_name`) und ist nicht unter eigener Kontrolle. Ohne die Kappung auf
40 Zeichen schiebt ein langer Name den Abgrenzungssatz über die Kante.

## Die Arbeit
1. Ein `tool_hints`-Eintrag für Peer-Werkzeuge — unter einem reservierten Schlüssel in
   `mcp_servers.yaml` (etwa `peer:` als Pseudoserver), oder ein eigenes Feld.
2. `peer_mcp_registry` setzt `hint` am `MCPToolInfo`, statt alles in `description` zu
   drängen. Das Feld existiert bereits (`MCPToolInfo.hint`).
3. Dann kann der Steuertext ohne Codeänderung justiert werden — und **muss** nach jeder
   Justierung durch `bin/measure-preselect.py`.

## Was beide Schichten NICHT sind
Keine Voraussetzung für Stufe 3. Die 92 % reichen für die gemeldeten Formulierungen; diese
zwei Schichten schließen den letzten Fall und machen den Text justierbar.
