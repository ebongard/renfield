# Design — Dokumentverweise über Instanzgrenzen (Proxy statt Deeplink)

**Status:** Entwurf, 2026-10-27. Ausgelöst durch die Frage des Eigentümers beim Bau des
Scan-Dokumentknopfes (`tasks/todo.md`): *„wenn ich aus dem Haushalt nach x-idra scanne?
Wir haben doch eine Verbindung zwischen den Instanzen."*

**Vorgabe des Eigentümers, wörtlich:** „Ich will keinen Deeplink. Wenn ich ein Dokument in
der gleichen Instanz anfrage, reicht der normale Link. Wenn ich einen Link in einer anderen
Instanz anfrage, dann sollte die Anfrage in die eigene Instanz gehen, die erkennt, es ist
nicht lokal sondern remote, und macht dann einen Proxy für die entfernte Instanz."

---

## 1. Der Fall

Ein Scan betritt **genau eine** Instanz (Routing-Invariante, `.claude/rules/scanner.md`).
Die Fertigmeldung geht aber an die Instanz, die **gefragt** hat — der Scanner-Vertrag sagt
das ausdrücklich:

> *„the scanner tells the instance that REQUESTED a scan how it ended. A separate seam from
> ingest (the document may have gone to a different instance than the one that asked)."*
> — `renfield-mcp-scanner/renfield_mcp_scanner/contract.py:29-31`

```
Haushalt: „scanne nach x-idra"
   │
   ├─► Scanner ──► Dokument nach X-IDRA        → dort Kennung 515
   │
   └─◄ Fertigmeldung an den HAUSHALT           → trägt `renfield_document_id: 515`
```

🛑 **Die Kennung ist instanzlokal.** Beide Instanzen führen eigene Zählfolgen; Dokument 515
gibt es im Haushalt auch, als etwas völlig anderes. Ein Knopf, der 515 lokal auflöst, führt
also nicht ins Leere, sondern ins **Falsche**. Der Zugriffsschutz greift zwar, verhindert
aber nur Fremdes — nicht das eigene falsche Dokument.

## 2. Was bereits existiert (gemessen, nicht vermutet)

| Baustein | Stand |
|---|---|
| Föderation als MCP-Server-Art | `MCPServerKind.FEDERATION`, `services/mcp_client.py:857` |
| Paarung + Peer-Registratur | `api/routes/federation_pairing.py`, `PeerUser`, `CircleMembership` |
| Instanz-Identität | Ed25519-Schlüsselpaar, `services/federation_identity.py`; `FEDERATION_REQUIRE_PERSISTENT_IDENTITY=true` im Haushalt |
| Kreis-Durchsetzung über die Grenze | `circle_sql.peer_scoped` + `enforce_circles` (#957) |
| Schwärzung für die Gegenseite | `atom_types.redacted_for_remote()` |
| Prüfprotokoll | `services/federation_audit.py`, `FederationQueryLog` |
| **Personenbezogene Abbildung** | **gebaut und beidseitig verdrahtet**, nur dunkel (`FEDERATION_IDENTITY_LINKS_ENABLED=false`). Siehe §4 — die erste Fassung dieses Entwurfs lag hier falsch. |

🛑 **Drei Lücken, die den Zeitplan bestimmen:**

1. **Die beiden Instanzen sind heute nicht gepaart.** Der Haushalt trägt
   `FEDERATION_ADVERTISED_URL`, x-idra trägt **keine** Föderationsvariable.
2. **Die Föderation föderiert ATOME, nicht REST-Ressourcen.** `FederationQueryResponder`
   beantwortet Atom-Abfragen. Ein Dokumentverweis ist keine Atom-Abfrage.
3. **Es gibt keine global eindeutige Dokumentkennung.** `documents.atom_id` ist nullable;
   laut Modellkommentar haben 207 von 210 Zeilen keine. Die naheliegende Lösung („nimm die
   Atom-UUID statt der lokalen Zahl") trägt für den Bestand also nicht.

## 3. Vorgeschlagene Architektur

Der Kern der Vorgabe: **der Klient spricht immer nur mit der eigenen Instanz.**

```
Browser ──► eigene Instanz ──┬─ lokal?  ──► wie heute antworten
                             │
                             └─ entfernt? ──► Föderations-MCP ──► Peer-Instanz
                                                  │                    │
                                              Identität           prüft Rechte
                                              abbilden            der PERSON
                                                  │                    │
                                             ◄────┴── geschwärzte Antwort
```

### 3.1 Der Verweis wird qualifiziert

Heute trägt das Artefakt `{ documentId: int }`. Künftig:

```jsonc
{ "kind": "document_ref",
  "data": { "documentId": 515, "instance": "xidra" } }   // `instance` optional
```

`instance` ist das **Scan-Ziel** aus dem Ereignis (`result["target"]`, bereits vorhanden,
`api/routes/scanner_jobs.py:82`). Fehlt es oder ist es die eigene Instanz → lokal.

### 3.2 Die Instanz bekommt einen lesbaren Namen

Heute kennt eine Instanz nur ihren Ed25519-Schlüssel. Es braucht eine **Selbstkennung**,
die mit den Scan-Zielnamen zusammenfällt:

```
INSTANCE_ID=household     # bzw. xidra
```

🛑 **Fail-closed:** ohne Selbstkennung gilt *jeder* qualifizierte Verweis als entfernt, und
ohne Peer gibt es keinen Knopf. Ein unkonfiguriertes System verlinkt also nie falsch.

Diese eine Einstellung ist der kleinste gemeinsame Nenner beider Stufen — Stufe 1 braucht
sie, um den falschen Link zu verhindern, Stufe 2, um den Peer zu adressieren.

### 3.3 Ein Proxy-Endpunkt, kein Deeplink

```
GET /api/knowledge/documents/{id}                  # lokal, wie heute
GET /api/knowledge/documents/{instance}/{id}       # NEU: löst auf oder reicht weiter
```

Der zweite Endpunkt antwortet in **derselben Form** wie der erste. Ist `{instance}` die
eigene, delegiert er intern an den ersten. Sonst stellt er eine Föderationsanfrage an den
Peer und gibt dessen Antwort zurück — **ohne** dessen Paperless-Adresse preiszugeben.

Für das Öffnen des Dokuments gilt dasselbe Prinzip:

```
GET /api/knowledge/documents/{instance}/{id}/open  # streamt vom Peer durch
```

So bleibt die Paperless-Adresse der anderen Instanz vollständig hinter der Grenze. Der
Browser sieht nur die eigene. Das löst nebenbei die LAN-Frage: die Gegenseite muss **nicht**
vom Gerät des Nutzers erreichbar sein, nur von der eigenen Instanz.

### 3.4 Ein neuer Föderationswerkzeug-Typ

Die Föderation beantwortet heute Atom-Abfragen. Es braucht ein zweites, eng umrissenes
Werkzeug:

```
peer.document_status(document_id) -> { status, in_paperless, filed_at }
peer.document_fetch(document_id)  -> Bytes (nur wenn die Person es darf)
```

🛑 **Beide gehen durch dieselbe Rechteprüfung wie eine lokale Anfrage**
(`check_document_access` auf der Peer-Seite), mit der **abgebildeten** Identität der
anfragenden Person — nicht mit der Instanz-Identität. Genau das leistet die bereits
gebaute Identitätsverknüpfung (§4).

## 4. Die Abhängigkeit — und eine Korrektur

🛑 **Die erste Fassung dieses Entwurfs behauptete, personenbezogene Föderation sei nicht
gebaut und Stufe 3 deshalb nicht möglich. Das war falsch.** Der Eigentümer widersprach
(„die Föderation sollte die aufrufende Person mitgeben und dann die Rechte auflösen — so
hatten wir das designed"), und die Nachmessung gibt ihm recht:

| Seite | Aufruf | Stelle |
|---|---|---|
| Fragerseite | `resolve_querier_ref(peer_id, local_user_id)` hängt die Person an den Umschlag | `services/federation_query_asker.py:144-149` |
| Antwortseite | `resolve_linked_user(...)` löst sie auf einen lokalen Nutzer auf | `services/federation_query_responder.py:314-315` |
| Verwaltung | `create_link` / `list_links` / `delete_link` | `services/federation_identity_link.py` + `api/routes/federation_user_links.py` |

Beide Seiten haben **echte Aufrufer** — das ist eigens geprüft, weil in diesem Projekt
mehrfach korrekt gebaute Riegel ohne Aufrufer standen.

**Was F-ID-2 wirklich ist:** die *Selbstbedienung* beim Verknüpfen (doppelte Anmeldung als
Einwilligung). Eine Verwaltungskraft kann die Verknüpfung heute schon anlegen. F-ID-2 ist
Bequemlichkeit, nicht Fähigkeit.

**Was Stufe 3 also tatsächlich braucht:**

1. `FEDERATION_IDENTITY_LINKS_ENABLED=true` in beiden Instanzen (heute `false`).
2. Eine Verknüpfung je Person, die auf beiden Seiten ein Konto hat.
3. Eine bestehende Paarung der beiden Instanzen — **die fehlt heute wirklich**: x-idra
   trägt keine einzige Föderationsvariable.

🛑 **Ohne die Verknüpfung fällt die Anfrage auf die Instanz-Stufe zurück** — der Peer sieht
dann „die andere Instanz fragt", nicht wer. Für einen Dokumentzugriff ist das zu grob:
entweder sähe jede Person im Haushalt jedes x-idra-Dokument dieser Stufe, oder es bleibt
bei „öffentlich" und der eigene Scan ist nie dabei. Die Verknüpfung ist deshalb keine
Verfeinerung, sondern Voraussetzung.

## 5. Vorgeschlagene Staffelung

| Stufe | Inhalt | Abhängigkeit |
|---|---|---|
| **1 — jetzt** | `INSTANCE_ID`, Artefakt trägt `instance`, Knopf **nur bei eigener Instanz**; bei fremder ein ruhiger Hinweis „liegt in \<Instanz\>" ohne Link | keine |
| **2 — danach** | Gegenseitige Paarung Haushalt ↔ x-idra; `FEDERATION_IDENTITY_LINKS_ENABLED=true`; Verknüpfung für die Person anlegen | nichts Ungebautes |
| **3 — zuletzt** | Proxy-Endpunkte + `peer.document_status` / `peer.document_fetch` | Stufe 2 |

Stufe 1 stellt den falschen Link sofort ab und legt die Selbstkennung, auf der die anderen
beiden aufbauen. Sie verschenkt nichts: der Knopf, den Stufe 3 bringt, ersetzt in Stufe 1
lediglich einen Hinweistext.

## 6. Offene Entscheidungen

1. **Name der Selbstkennung.** `INSTANCE_ID` ist neu; es gäbe auch den Weg, die
   Scan-Zielnamen aus einer Zielregistratur zu beziehen, die das Backend heute nicht hat.
2. **Hinweistext in Stufe 1.** „liegt in x-idra" nennt die andere Instanz beim Namen. Ist
   das im Haushalt erwünscht, oder soll dort nur „liegt in einer anderen Instanz" stehen?
3. **Reicht `document_status`, oder braucht es `document_fetch`?** Wenn der Knopf nur sagen
   soll „dort abgelegt", genügt der Status. Erst das Öffnen im eigenen Browser verlangt das
   Durchreichen der Bytes — mit allem, was das an Zwischenspeicherung und Protokollierung
   nach sich zieht.
4. **Prüfprotokoll.** `FederationQueryLog` deckt Atom-Abfragen. Ein Dokumentabruf über die
   Grenze gehört ebenfalls protokolliert — dieselbe Tabelle oder eine eigene?

## 7. Was dieser Entwurf NICHT vorschlägt

- **Keinen Deeplink** in die Paperless-Oberfläche der anderen Instanz. Ausdrücklich
  verworfen: er setzt voraus, dass die Gegenseite vom Gerät des Nutzers erreichbar ist, und
  er trägt die Rechteprüfung nicht mit.
- **Keine gemeinsame Dokumentkennung** über beide Instanzen. Das wäre eine Migration des
  Bestands und löst das Rechteproblem nicht.
- **Kein Hub, der alles aufnimmt.** Die Routing-Invariante bleibt: ein Scan betritt genau
  eine Instanz.

Verwandt: `docs/design/federation-identity-mapping.md`, `docs/design/scanner-ingest.md`,
`.claude/rules/scanner.md`, `.claude/rules/circles.md`.
