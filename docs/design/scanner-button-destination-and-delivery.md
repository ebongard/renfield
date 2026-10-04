# Der blaue Knopf: ein Ziel und ein Rückweg

**Status:** Entwurf, 2026-10-04. Kein Code vor Abnahme.
**Betroffen:** `renfield-mcp-scanner` (`button.py`, `tools.py`, `config.py`) und
`src/backend/services/scanner_jobs.py` + `api/routes/scanner*.py`.
**Verwandt:** `docs/design/scanner-ingest.md` · `.claude/rules/scanner.md` · #1243

---

## 1. Der Anlass

Am 2026-10-04 meldete der Eigentümer „das Scannen von Briefen funktioniert
nicht". Die Untersuchung fand **drei** Dinge, von denen nur das erste ein
Gerätefehler war:

| Zeit | Auslöser | Ausgang |
|---|---|---|
| 13:07 | Chat, „Briefe" | `device_unavailable` — der Scanner antwortete nicht |
| 15:23:54 | blauer Knopf | `scanner_fault` — Papierstau nach 2 Seiten |
| 15:24:59 | blauer Knopf | **38 Seiten gescannt**, Ausgang `unrouted` |

Der Gerätefehler war Hardware und ist behoben. Die beiden anderen Fälle legen
zwei Lücken offen, die unabhängig voneinander bestehen und sich gegenseitig
verdecken.

Dass die Untersuchung überhaupt so lange dauerte, hatte einen eigenen Grund: die
Zustellzeile des Backends schrieb nur `(failed)`, nie den `error_code`. Das ist
seit `53ca61da` behoben und hier nicht mehr Gegenstand.

## 2. Lücke A — der Knopf hat kein Ziel, und die Doku verspricht eines

`button.py` sagt im Kopfkommentar:

> WOHIN SO EIN SCAN GEHT — Nirgendwohin, und das ist Absicht. […] **Wer den
> Knopf fest zuordnen will, setzt `SCANNER_CALLER_TARGET_BUTTON`.**

Die Variable **ist** gesetzt (`run-scanner-mcp.sh`: `household`). Sie hat
trotzdem nichts bewirkt. Grund: `caller_targets` wird im gesamten Code an
**genau einer** Stelle gelesen —

```python
# jobs.py
def target_for(self, caller: str | None) -> ScanTarget | None:
    target_id = self._config.caller_targets.get((caller or "").lower())
```

— und das ist `JobEvents`, also der **Rückweg** für das Abschlussereignis. Für
das Ziel des **Dokuments** wird sie nie befragt. `config.py` beschreibt sie
korrekt als Rückmeldeweg; die beiden Kommentare widersprechen sich, und der
falsche steht genau dort, wo jemand nachschlägt.

Folge am 2026-10-04: der Klassifizierer schlug `xidra` mit 0,90 vor, unter der
Schwelle von 0,95, und der Stapel blieb liegen:

```
classifier suggested 'xidra' at 0.90, below the 0.95 threshold
```

Das Zurückhalten ist **richtig** — die Routing-Invariante verlangt, dass ein
Scan erst eine Instanz betritt, wenn sein Ziel feststeht. Falsch ist nur, dass
die dokumentierte Notbremse nicht existiert.

🛑 **Was NICHT die Lösung ist:** die Schwelle senken. 0,90 war eine Vermutung
über 38 Seiten fremder Post; fünf Hundertstel tiefer wäre sie zur Tatsache
geworden. Die Schwelle ist die Grenze zwischen Wissen und Raten, nicht ein
Komfortregler.

## 3. Lücke B — ein Knopfdruck kann sein Ergebnis nie melden

Beide Knopf-Aufträge bekamen vom Backend:

```
scanner: event for unknown job 6a3fe346… (failed) ignored   →   409 Conflict
```

Der Grund steckt in der Architektur, nicht in einer Fehlkonfiguration:
`remember_scan_requester()` wird aus `action_executor` gerufen und schreibt den
Anfragenden aus einem **angemeldeten Chat-Zug** nach Redis. Ohne `session_id`
wird nichts gespeichert — der Docstring sagt es selbst: *„Without a session
there is no conversation to report into."*

Ein Knopfdruck hat keinen Chat-Zug. Also ist **jeder** Knopf-Auftrag für das
Backend ein unbekannter Auftrag. Der Scanner wiederholt die Zustellung 24
Stunden lang (gemessen: 11 Versuche in 90 Sekunden mit wachsendem Abstand) und
gibt dann auf.

**Damit ist der Knopf stumm, nicht kaputt.** Ob gestaut, gescannt oder
zurückgehalten — niemand erfährt es. Das erklärt die Meldung „der Knopf
funktioniert auch nicht" vollständig, ohne dass am Knopf etwas defekt wäre.

## 4. Entwurf A — ein Ziel für den Knopf

Drei Wege wurden erwogen:

| | Weg | Urteil |
|---|---|---|
| A1 | Eigenes, knopf-spezifisches Ziel in der Konfiguration | **empfohlen** |
| A2 | `caller_targets` auch auf das Dokumentziel anwenden | verworfen, s. u. |
| A3 | Schwelle für Knopf-Scans senken | verworfen — Raten statt Wissen |

**Warum nicht A2**, obwohl `button.py` es verspricht: der `caller` ist bei einem
**Chat**-Scan die anfragende Instanz, und dort darf er das Ziel gerade **nicht**
bestimmen. Der Fall vom 2026-10-04 ist der Beleg: die Anfrage kam aus dem
Haushalt, der Klassifizierer las geschäftliche Post und schlug `xidra` vor. Würde
der Aufrufer das Ziel setzen, wäre die mehrinstanzige Zuordnung für jeden
Chat-Scan tot. Die Kopplung sieht sparsam aus und nimmt dem Entwurf seinen Kern.

**A1 im Einzelnen:**

```
SCANNER_BUTTON_TARGET=<target id>   # leer = heutiges Verhalten (unrouted)
```

- Greift **nur** für `caller == "button"`, also für Scans ohne jeden anderen
  Anhaltspunkt.
- **Trennblätter schlagen es**, wie heute. Sie sind die einzige Routing-Schicht,
  die einen unbeaufsichtigten Scan übersteht, und müssen gewinnen — sonst kann
  man einen Stapel nicht mehr bewusst nach xidra schicken.
- Der Klassifizierer wird **weiterhin befragt** und sein Vorschlag protokolliert,
  auch wenn das Ziel feststeht. Sonst verliert man die Messung, wie gut er wäre
  — und genau die braucht man, um die Schwelle je zu begründen.
- Unbekannte Ziel-Id → Start verweigern, nicht stillschweigend ignorieren. Eine
  stille Fehlkonfiguration ist, was uns diesen Tag gekostet hat.
- Der Kopfkommentar in `button.py` wird berichtigt; die Zusage auf
  `SCANNER_CALLER_TARGET_BUTTON` verschwindet.

## 5. Entwurf B — ein Rückweg für den Knopf

Der Knopf braucht einen Empfänger, der nicht aus einem Chat-Zug stammt.

🛑 **Die Kennung darf NICHT aus dem Ereignis kommen.** Die bestehende Regel ist
ausdrücklich: *„The event carries no user identity — never trust one from it."*
Ein Entwurf, der `caller: "button"` aus dem Rumpf liest und daraus einen
Empfänger ableitet, macht den Rückweg zu einem Weg, in fremde Konversationen zu
schreiben.

**Vertrauenswürdig ist allein der authentifizierte Mandant.** Die Route
akzeptiert nur Client-Ids aus `SCANNER_INGEST_CLIENT_IDS`; dieser Wert kommt aus
dem Token, nicht aus dem Rumpf. Daran wird der Empfänger gehängt:

```
SCANNER_UNATTENDED_RECIPIENT_<CLIENT_ID>=<user_id>:<session_id>
```

- `handle_job_event` greift darauf **nur** zurück, wenn für den Auftrag **kein**
  Anfragender in Redis steht. Ein Chat-Scan ist davon unberührt.
- Die Zustellung läuft danach durch **dieselbe** dreischichtige Kette wie heute
  (Marker, Lease, Advisory-Lock vor dem Insert). Keine zweite Zustellmechanik.
- Die Eigentumsprüfung bleibt: gehört die Konversation einem anderen Konto, ist
  die Ablehnung endgültig (`refused`), nicht wiederholbar.
- Fehlt die Konfiguration, bleibt es beim heutigen 409 — aber mit einer
  **WARNING**, die den Mandanten nennt. Heute schweigt diese Stelle.

**Zusätzlich, nicht stattdessen:** die Raumansage. Für Sprachanfragen gibt es
sie schon (`announce_in_room`). Wer am Scanner steht, hört dann „Scan fertig,
38 Seiten" — der Kanal für genau die Person, die den Knopf gedrückt hat und
keinen Chat offen hat. Sie trägt bewusst **keinen Titel und kein Dokumentdetail**
(gemeinsamer Raum) und braucht eine Raum-Id in der Konfiguration des Mandanten.

Personengebundene Benachrichtigungen bleiben **nicht** der Kanal — sie sind
präsenzabhängig, und das ist die falsche Kopplung für ein Blatt Papier, das
bereits eingezogen ist.

## 6. Was unverändert bleibt

- Die Routing-Invariante: ein Scan betritt genau **eine** Instanz, und erst wenn
  das Ziel feststeht. A1 setzt das Ziel vorher fest; es umgeht die Regel nicht.
- `scan_document` startet nur und gibt `{job_id}` zurück. Kein Scan im Werkzeugruf.
- Die Nachricht wird aus **festen, lokalisierten Vorlagen** gebaut. Kein Freitext
  aus dem Ereignis erreicht den Chat.
- Unbekannter Auftrag → **409**, nie 404. Der schnell scheiternde Scan muss
  wiederholen dürfen, bis der Anfragende eingetragen ist.
- Die Schwelle von 0,95 bleibt, wo sie ist.

## 7. Entscheidungen für den Eigentümer

| # | Frage | Empfehlung |
|---|---|---|
| 1 | `SCANNER_BUTTON_TARGET` einführen? | **ja**, auf `household` |
| 2 | Soll der Klassifizierer trotz festem Ziel laufen? | **ja** — nur so lässt sich die Schwelle später belegen |
| 3 | Empfänger je Mandant oder global? | **je Mandant** (`<CLIENT_ID>`), sonst ist die dritte Instanz wieder ein Sonderfall |
| 4 | Raumansage für Knopf-Scans? | **ja**, zusätzlich — der Mensch steht am Gerät |
| 5 | Ohne konfigurierten Empfänger: 409 wie heute, oder stillschweigend verwerfen? | **409 + WARNING** — ein verworfener Ausgang ist unauffindbar |

## 8. Prüfplan

**Scanner-Seite**
- Knopf-Scan mit gesetztem `SCANNER_BUTTON_TARGET` → `done`, nicht `unrouted`.
- Mit Trennblättern → Trennblätter gewinnen, das feste Ziel wird überstimmt.
- Unbekannte Ziel-Id → Start verweigert, Fehlermeldung nennt die Variable.
- Leeres `SCANNER_BUTTON_TARGET` → byte-identisch zu heute.

**Backend-Seite**
- Ereignis ohne Redis-Eintrag, mit konfiguriertem Empfänger → `delivered`.
- Ereignis ohne Eintrag, **ohne** Empfänger → 409 **und** eine WARNING, die den
  Mandanten nennt.
- Chat-Scan mit Eintrag → unverändert; der Rückfall darf nicht greifen.
- Fremde Konversation als Empfänger → `refused`, nicht wiederholt.
- 🛑 **Negativkontrolle:** mit entferntem Rückfall müssen genau die zwei neuen
  Tests rot werden und kein weiterer.
- 🛑 **Gegenprobe gegen das Ereignis:** ein Ereignis, das `user_id` oder
  `session_id` im Rumpf mitschickt, darf sie **nicht** verwenden.

**Ende zu Ende**
- Echtes Papier, Knopfdruck, Haushalt: Dokument abgelegt, Nachricht in der
  konfigurierten Konversation, Ansage im Raum, keine 409-Schleife im Protokoll.

## 9. Nachtrag 2026-10-04 — die Korrektur ist nicht idempotent

Beim Versuch, den liegengebliebenen Stapel einzuliefern, wies das Backend ihn mit
`file_too_large` ab: **147,3 MB gegen eine Grenze von 50 MB**. Die Untersuchung
fand dabei etwas Schwerwiegenderes als ein Größenproblem.

**Gemessen:** `correct_file()` schreibt jede Seite *in place* und macht sie bei
jedem Durchlauf kleiner. Dieselbe Seite, viermal nachkorrigiert:

```
1,444 → 1,198 → 1,129 → 1,068 → 1,004 MB
```

Das ist keine Effizienz, das ist **Substanzverlust**. Jeder Wiederholungsversuch
— und `route_scan` auf einen liegenden Stapel ist einer — bearbeitet bereits
bearbeitete Pixel erneut. Die Notiz zu diesem Scanner warnt dieselbe
Fehlerklasse schon einmal („Never deskew twice — a second resampling pass"); sie
gilt für die ganze Korrekturkette, nicht nur fürs Entzerren.

**Folge für den liegenden Stapel:** die Seiten auf Platte sind bereits einmal
nachkorrigiert; das 147-MB-PDF stammt vom ersten Durchlauf und ist damit die
**beste** Fassung. Es gehört unverändert eingeliefert, nicht neu gebaut.

**Was damit NICHT die Lösung ist:** das PDF kleiner zu packen, indem die
Korrektur erneut läuft. Das tauscht Qualität gegen Bytes, und zwar lautlos.

**Vorschlag:** `correct_file()` erhält eine Marke je Seite (Seitenzustand neben
der Datei oder ein Eintrag in `stage.json`) und läuft **genau einmal**. Ein
zweiter Aufruf ist dann ein No-op statt eines Qualitätsverlusts. Die Montage
darf danach beliebig oft wiederholt werden — genau das braucht ein
Wiederholungsversuch.

Gegengeprüft wurde dabei auch, was es **nicht** ist: `img2pdf` bettet PNG
verlustfrei und größengleich ein, und `ocrmypdf` kostet in der PDF/A-Vorgabe
rund 8 % (gemessen: 13 → 14 MB auf sechs Seiten). Beide meiner ersten
Erklärungen waren falsch.

Die Größengrenze selbst ist separat auf 200 MB angehoben, weil der PDF-Schneider
als Vorstufe im document-worker **nach** der Einlieferung läuft: ein Stapel, der
das Schneiden braucht, kam vorher nie bis dorthin.

## 10. Offen

- Die Raum-Id für den Scanner-Standort ist noch nirgends konfiguriert.
- Ob die zwei wartenden Ablagen vom 2026-10-04 über `route_scan` hereingeholt
  oder verworfen werden, entscheidet der Eigentümer — eine Datenaktion, die über
  den Chat läuft.
