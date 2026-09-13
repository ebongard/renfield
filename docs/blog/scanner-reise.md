# Der Scanner war nie kaputt

*Werkstattbericht — Dokumentenerfassung · ScanSnap S1500 · SANE · MCP*

Nach einem Betriebssystem-Update nahm der Dokumentenscanner keine Seite mehr an.
Die Ursache lag woanders, als alle Anzeichen zeigten — und was danach kam, war
kein Scannerproblem mehr, sondern die Frage, wie ein Blatt Papier den richtigen
Empfänger findet.

> Technische Referenz: [`docs/design/scanner-ingest.md`](../design/scanner-ingest.md) ·
> Skript: [`bin/scan.sh`](../../bin/scan.sh)

---

## Die Diagnose stand zwei Befehle tiefer

Der Scanner war seit Jahren unauffällig. Dann kam ein grosses
Betriebssystem-Update, und er verschwand. Nicht halb, nicht sporadisch — die
Herstellersoftware fand kein Gerät, und auch das freie Scanner-Backend meldete
beharrlich: *keine Scanner gefunden*.

Die naheliegende Erklärung war Altersschwäche. Das Gerät ist über zehn Jahre alt,
der Hersteller hat den Support längst eingestellt, die mitgelieferte Software
läuft nur noch emuliert. Ein Fall für den Elektroschrott.

Tatsächlich war das Gerät am USB-Bus einwandfrei sichtbar, mit korrekter
Hersteller- und Produktkennung. Die Gerätesuche fand es sogar. Nur das Öffnen
scheiterte — und der Grund stand erst da, als man die Fehlerausgabe des
Scanner-Backends einschaltete:

```
USBDeviceOpen: another process has device opened for exclusive access
```

Die Herstellersoftware lief noch. Sie belegte das Gerät **exklusiv** — und konnte
selbst nicht mehr damit scannen. Ihre Geräteüberwachung startete beim Anmelden
brav mit, ihre Scanfunktion war unter dem neuen Betriebssystem tot. Sie
blockierte sich selbst und jede Alternative gleich mit.

Ein Beenden des Programms genügte. Danach meldete sich der Scanner binnen einer
Sekunde, und der erste Testlauf gegen einen leeren Einzug gab die schönste
Fehlermeldung des Tages zurück: `Document feeder out of documents` — der ganze
Weg bis zur Hardware stand, es fehlte nur Papier.

> **Was bleibt:** „Das Gerät ist kaputt" und „etwas hält das Gerät fest" sehen von
> aussen identisch aus. Der Unterschied stand in einer Fehlerausgabe, die
> standardmäßig niemand sieht.

---

## Dann sah das Ergebnis schlechter aus als vorher

Scannen ging wieder. Nur waren die Scans sichtbar schlechter als die der alten
Herstellersoftware — blasser Hintergrund, ein Blaustich, eine Seite schief. Der
Reflex wäre gewesen, an Helligkeit und Kontrast zu drehen, bis es „besser
aussieht".

Stattdessen wurde gemessen. Jeder einzelne Fehler liess sich beziffern, und jede
Zahl zeigte auf eine andere Ursache. Sechs waren es am Ende — und fünf davon
waren nicht der Scanner, sondern die frisch gebaute Verarbeitungskette.

| Defekt | Ursache | vorher | nachher |
|---|---|---|---|
| Seitenhöhe | Voreinstellung war US Letter, nicht A4 — 17,7 mm fehlten unten, samt Fusszeile mit Bankdaten und Steuernummer | 3299 px | 3508 px |
| Kompression | Die OCR-Stufe transkodierte den Archiv-Scan stillschweigend verlustbehaftet | JPEG, 3,2 % | verlustfrei |
| Farbstich | Rohaufnahme deutlich blau; Papier ist per Definition neutral, also am Papiergipfel verankert | B−R +26,0 | +2,6 |
| Durchschein | Duplex beleuchtet von beiden Seiten, die Rückseite scheint durch | 7,08 % | 1,93 % |
| Schräglauf | Die automatische Entzerrung *erzeugte* die Schieflage auf der Rückseite, statt sie zu beheben | ~34° | 0° |
| Seitenmass | Beim Nachbearbeiten ging die Auflösungsangabe verloren — Pixel korrekt, Blatt 66 cm breit | 96 dpi | 300 dpi |

Zwei davon sind lehrreicher als die anderen.

Die **Entzerrung** fiel nur auf, weil die Vorderseite gerade war und die
Rückseite nicht — bei einem schief eingezogenen Blatt wären *beide* schief. Ein
einseitiger Schräglauf kann gar nicht vom Papier kommen; er ist immer Software.

Der **Blaustich** liess sich nicht am Weisspunkt beheben: der Stich sass in den
Mitteltönen. Erst als Ankerpunkt der Papiergipfel des Histogramms diente —
Papier ist neutral, also müssen seine drei Kanäle gleich sein — stimmte die
Farbe.

> **Eigener Fehler:** Fünf der sechs Defekte hatte die neue Verarbeitungskette
> selbst eingebaut. Der Test mit einer künstlichen, fast weissen Seite hatte sie
> alle durchgelassen — die verlustbehaftete Kompression greift erst bei echten
> Bilddaten. Ein Testbild, das der Wirklichkeit ausweicht, bestätigt nur die
> eigene Annahme.

---

## Ab hier war es kein Scannerproblem mehr

Papier kommt hier nicht für einen Empfänger. Es gibt mehrere Instanzen mit
unterschiedlichen Einsatzzwecken — getrennte Systeme, getrennte Datenbestände
und unterschiedliche Aufbewahrungspflichten. Bisher hatte ein Mensch die Post
nach dem Scannen von Hand in den richtigen Ordner geschoben. Genau dieser Handgriff
*ist* die Routing-Entscheidung.

Der naheliegende Entwurf: eine Instanz nimmt alles entgegen, schaut hinein,
verteilt weiter. Er scheidet aus. Denn eine Ablage ist praktisch nicht rückgängig
zu machen — binnen Sekunden ist ein Dokument in Textabschnitte zerlegt,
eingebettet, mit extrahierten Fakten versehen und archiviert. Das Löschen der
Zeile holt davon nichts zurück.

> **Der Satz, der den Entwurf bestimmt:** Ein Scan erreicht genau eine Instanz —
> und erst, wenn sein Ziel feststeht. Ein unentschiedener Scan liegt in keiner
> Datenbank.

Damit fällt die Sammelstelle weg, und die Entscheidung muss vor dem Einliefern
fallen. Drei Wege, in dieser Reihenfolge:

**bestimmt — der Mensch sagt es.** „Scanne das in die zweite Instanz." Man steht
ohnehin am Gerät. Kein Rateanteil.

**bestimmt — ein Trennblatt sagt es.** Ein Deckblatt mit Strichcode, vor den Stapel
gelegt. Es markiert Dokumentgrenze *und* Ziel in einer Marke — der einzige Weg,
der auch bei unbeaufsichtigtem Scannen funktioniert.

**geschätzt — der Inhalt legt es nahe.** Ein Sprachmodell liest die erste Seite und
schlägt ein Ziel mit einem Vertrauenswert vor.

**Boden — sonst entscheidet ein Mensch.** Alles Unsichere wartet — auf dem Rechner
am Scanner, nicht in einer Datenbank.

Beim Trennblatt zeigte sich gleich, warum die Nutzlast nicht einfach `ZIEL-A`
lauten darf: echte Dokumente tragen selbst Strichcodes — den Zahlcode auf
Rechnungen, die Sendungsnummer auf Benachrichtigungen. Ein schlichter Bezeichner
hätte jeden davon zum Trennblatt gemacht und ein Dokument stillschweigend in der
Mitte zerschnitten. Die Nutzlast bekam ein eigenes Präfix mit Version.

---

## Dieselbe Mechanik, sehr ungleiche Folgen

Für die inhaltliche Einordnung gab es bereits etwas im System: ein Klassifikator,
der Dokumente per Sprachmodell in eine Taxonomie einsortiert und die Antwort
gegen eine Positivliste prüft. Genau das Verfahren, das gebraucht wurde. Der
Reflex, etwas Eigenes danebenzustellen, war falsch — aber ihn einfach zu
übernehmen wäre es auch gewesen.

Denn der bestehende Klassifikator *füllt ein Auswahlfeld vor*. Liegt er daneben,
korrigiert ein Mensch, bevor etwas passiert. Der neue legt oberhalb seiner
Schwelle ein Dokument über eine Vertrauensgrenze hinweg ab, **ohne dass jemand
hinsieht**. Gleiches Verfahren, sehr ungleiche Folgen — also eine deutlich
strengere Schwelle, und jeder Fehlerpfad endet bei „unentschieden":
unerreichbares Modell, unlesbare Antwort, erfundenes Ziel, fehlender
Vertrauenswert.

Was das kostet, zeigte der erste Lauf gegen echte Texte. Ein eindeutig einer
Instanz zuzuordnender Brief kam auf **0,90** — und landete damit im Prüfstapel,
obwohl jeder Mensch ihn sofort zugeordnet hätte. Das ist kein Fehler, das ist der
bewusst gewählte Preis.

Bemerkenswert war die zweite Messung. Mit sorgfältig ausformulierten
Zielbeschreibungen sprang derselbe Fall auf **1,00**. Die Beschreibung in der
Konfiguration ist keine Dokumentation — sie ist die gesamte
Entscheidungsgrundlage.

---

## Vier Fehlschläge auf dem Weg

Das Interessante am Bauen sind nicht die Teile, die funktionierten.

**Routing.** Der erste Sprachbefehl „scanne das Dokument" landete beim
*Bluetooth-Gerätescan*. Die Rollenbeschreibung für Smart-Home war die einzige,
die das Wort „scannen" enthielt; die Dokumentrolle erwähnte es nirgends. Das
Werkzeug zu registrieren genügt nicht — die Beschreibung ist das Einzige, was der
Verteiler sieht.

**TLS.** Der erste echte Scan lief durch und scheiterte am letzten Schritt:
Zertifikatsprüfung fehlgeschlagen. Minuten zuvor hatte dieselbe Adresse im
Terminal einwandfrei geantwortet — das Kommandozeilenwerkzeug vertraut dem
Systemspeicher, die Programmbibliothek ihrem eigenen. Die Adresse sieht gesund
aus und ist es aus einem anderen Programm heraus nicht.

**Auth.** Die Zugangsprüfung für den Dienst war eingebaut, getestet — und
wirkungslos. Das verwendete Rahmenwerk erzeugt bei jedem Aufruf eine *neue*
Anwendung, sodass die Prüfschicht an einer weggeworfenen Kopie hing. Aufgefallen
ist es nur, weil danach geprüft wurde, ob eine Anfrage ohne Zugangsdaten
wirklich abgewiesen wird.

**Bericht.** Der Assistent meldete: „Die Dokument-ID in der Ablage ist 426."
Beides falsch — es war die interne Kennung, und in der Ablage lag das Dokument zu
dem Zeitpunkt noch gar nicht. Das Feld hiess schlicht `document_id`, ohne zu
sagen, aus welchem System. Kein Fehler des Modells: eine unqualifizierte Kennung
ist eine Einladung zur Fehldeutung.

> **Was sich ausgezahlt hat:** Beim Zertifikatsfehler blieben die Seiten liegen,
> statt verworfen zu werden. Nach dem Beheben liess sich derselbe Scan
> einliefern — ohne das Papier noch einmal einzuziehen. Ein Scan ist die einzige
> Art Datei, die man nicht neu erzeugen kann, ohne aufzustehen.

---

## Gleicher Code, andere Konfiguration

Zum Schluss die Frage, die am meisten hängenblieb: Warum verhielten sich zwei
Installationen desselben Codes beim Ausrollen so unterschiedlich?

Die Antwort zerfällt sauber. **122 von rund 149** Konfigurationswerten sind
identisch; die Unterschiede folgen den unterschiedlichen Einsatzzwecken. Das ist
der Sinn von Konfiguration.

Der Rest war Geschichte, nicht Entwurf: ein anderes Dateiformat, ein fehlendes
Ablagefach für Zugangsdaten, und ein fest verdrahteter Namensraum in der
Datenbank-Migration. Der letzte war der gefährlichste — ein Namensraum im
Manifest *überstimmt* den Schalter auf der Kommandozeile. Die Migration wäre
gegen die *falsche* Installation gelaufen, hätte Erfolg gemeldet, und die
gemeinte wäre unverändert geblieben. Gefangen hat das nur eine Notiz von früher.

> **Regel danach:** Ein struktureller Unterschied zwischen zwei Installationen
> braucht einen Grund, der irgendwo aufgeschrieben steht. Unterschiedliche
> Funktionen: ja. Unterschiedliches Dateiformat: nein — das ist Drift, und jede
> einzelne davon ist eine Falle für das nächste Ausrollen.

---

## Wo es endete

Ein gesprochener Satz genügt jetzt. Der Einzug läuft an, das Blatt wird
korrigiert, mit Texterkennung versehen, dem richtigen System übergeben, dort in
durchsuchbare Abschnitte zerlegt und abgelegt — samt automatisch erzeugtem Titel
aus den erkannten Fakten. Achtzehn Sekunden.

Der Scanner ist derselbe wie am Morgen. Er war nie kaputt.

---

*Alle Zahlen in diesem Text sind an echten Scans gemessen, nicht geschätzt.
Instanznamen, Adressen und Dokumentinhalte sind absichtlich nicht enthalten.*
