# FAQ — Dokumente, Verarbeitung & Paperless

Häufige Fragen rund um Dokumente, die Wissensbasis, die Verarbeitung und
Paperless — und was du **selbst per Chat oder Oberfläche** erledigen kannst,
ohne dass jemand in die Technik schauen muss.

---

## „Ich finde ein Dokument nicht / wo ist meine Datei?"

Renfield legt jedes Dokument unter einem automatisch erzeugten, **sprechenden
Titel** ab (z. B. „McPaper AG – Kassenbon vom 14.04.2026"), **nicht** unter dem
ursprünglichen Dateinamen. Suche deshalb nach dem **Inhalt oder Aussteller**,
nicht nach dem Dateinamen.

- **Oberfläche:** Suchfeld auf `/wissen/dokumente` (bzw. `/knowledge`) — die
  Suche findet ein Dokument über **Name, Inhalt UND extrahierte Fakten**, auch
  ältere, die nicht mehr in der Liste der zuletzt gezeigten Dokumente stehen.
- **Chat:** *„Suche McPaper"* / *„finde die Rechnung von …"*

**Warum eine neu abgelegte Datei „verschwindet":** Legst du eine Datei erneut in
den Überwachungsordner, die inhaltlich **schon** in der Wissensbasis ist,
erkennt Renfield sie als **Dublette** (byte-genauer Abgleich) und legt sie
**nicht doppelt** an — sie ist bereits vorhanden (unter ihrem ursprünglichen
Titel). Das ist gewollt und verhindert Doubletten. Über die Suche findest du das
vorhandene Dokument.

---

## „Ist alles verarbeitet? Was hängt gerade?"

- **Chat:** *„Wie ist der Verarbeitungsstatus?"*

Renfield zeigt dann: wie viele Dokumente **fertig / in Warteschlange / in Arbeit
/ fehlgeschlagen** sind, ob der Verarbeitungs-Worker **läuft**, wie tief die
Queue ist, und die **Paperless-Ablage** (abgelegt / ausstehend / fehlgeschlagen).

Das ist auch die zuverlässige Antwort auf **„Sind alle Dokumente in Paperless?"** —
die Statusausgabe enthält genau diese Zahlen (z. B. „391 abgelegt, 7 ausstehend,
3 fehlgeschlagen").

---

## „Welche Dokumente sind nicht in Paperless? / Ist Dokument X abgelegt?"

- **Nicht abgelegte auflisten:** *„Welche Dokumente sind nicht in Paperless?"* /
  *„Welche Dokumente konnten nicht abgelegt werden?"* — Renfield listet **mit
  Namen** die Dokumente, deren Paperless-Ablage **fehlgeschlagen** oder noch
  **ausstehend** ist.
- **Ein bestimmtes prüfen:** *„Ist die Rechnung Taxon in Paperless?"* /
  *„Ist Dokument X abgelegt?"* — Renfield meldet den Ablage-Status (abgelegt mit
  Paperless-Nummer / als Duplikat vorhanden / fehlgeschlagen / ausstehend).
- **Erneut ablegen (reparieren):** *„Lege die fehlgeschlagenen Dokumente in
  Paperless ab."* — Renfield stellt die fehlgeschlagenen Ablagen erneut in die
  Warteschlange; die Ablage läuft im Hintergrund. Gezielt geht auch:
  *„Lege Dokument X erneut in Paperless ab."*

> **Wichtig:** Ein **normaler Upload** über die Oberfläche oder den Chat wird
> **nicht** automatisch nach Paperless abgelegt — nur Dateien aus dem
> **Überwachungsordner** und dem **E-Mail-Postfach** laufen in Paperless. Und in
> Paperless erscheinen Dokumente unter ihrem **aus dem Inhalt erkannten Titel /
> Korrespondenten**, nicht unter dem Dateinamen — suche dort also nach
> **Korrespondent oder Inhalt**, nicht nach dem ursprünglichen Dateinamen.

---

## „Welche Dokumente sind nicht durchsuchbar / leer?"

Ein Dokument ist nur auffindbar, wenn es **Chunks** (durchsuchbare Textabschnitte)
hat. Fehlen sie, ist das Dokument zwar da, aber die Suche findet es nicht über
den Inhalt.

- **Auflisten:** *„Welche Dokumente haben keine Chunks?"* — mit Kennzeichnung
  **REPARIERBAR** vs. **UNINDEXIERBAR**.
- **Reparieren:** *„Indexiere die Dokumente ohne Chunks neu."* — reparierbare
  Dokumente werden neu eingelesen.

---

## „Doppelte Dokumente in Paperless aufräumen"

- **Chat:** *„Finde und lösche die Duplikate in Paperless."*

Renfield findet Dubletten (gleiche Datei, gleiche Metadaten oder gleicher
OCR-Text), **behält das älteste Original** und verschiebt die Kopien in den
**Papierkorb** (wiederherstellbar). Bei sehr vielen Dubletten läuft das
in Schüben — Renfield meldet, wie viele noch übrig sind; einfach erneut fragen.

---

## „Doppelte Dokumente in der Wissensbasis finden"

- **Chat:** *„Gibt es Dubletten in der Wissensbasis?"* / *„Sind Dokumente doppelt vorhanden?"*

Das ist eine **andere** Prüfung als die Paperless-Duplikate oben: sie findet
zwei Dokumente in der **Wissensbasis**, die inhaltlich dasselbe Dokument sind
(z. B. dieselbe Rechnung aus zwei Quellen), aber **unterschiedliche Dateien**
sind — die byte-genaue Dublettenerkennung beim Einlesen übersieht sie. Erkannt
werden sie an einer gemeinsamen eindeutigen Kennung (z. B. gleiche
Rechnungsnummer).

Renfield **löscht nie automatisch** — jedes Paar landet zur Prüfung unter
`/brain/review` (bzw. `/wissen` → „Prüfen"). Dort wählst du, welches Dokument
erhalten bleibt und ob das doppelte **ausgeblendet** (wiederherstellbar) oder
**gelöscht** wird.

---

## „Ein Dokument an Simba (Steuerkanzlei) senden" *(nur xidra)*

Auf einer Dokumentzeile in `/wissen/dokumente` (oder `/knowledge`) gibt es die
Aktion **„An Simba senden"**. Ein Klick öffnet ein **Overlay**: du prüfst
**Bezeichnung, Kategorie, Typ und Zeitraum** (vorbefüllt), bestätigst in **zwei
Schritten** und überträgst das Dokument **direkt dort** an die Steuerkanzlei.

> ⚠️ **Die Übertragung ist unwiderruflich.** Das Portal erlaubt keinen Rückzug.
> Deshalb der bewusste Zwei-Schritt-Bestätigen-Dialog. Brichst du ab, bleibt der
> Vorschlag in der Prüf-Queue unter `/brain/review` liegen (dort kannst du ihn
> später übertragen oder verwerfen).

Watch-Folder-PDFs landen automatisch als Vorschlag in derselben Queue — sie
werden **nie** automatisch übertragen.

---

## Wie funktioniert die Dokument-Pipeline?

1. Datei in den **Überwachungsordner** legen (SMB-Share).
2. Renfield liest sie ein: **OCR / Docling → durchsuchbare Chunks** in der
   Wissensbasis.
3. Renfield legt sie in **Paperless** ab.
4. **Dubletten** (byte-genau identische Dateien) werden übersprungen — kein
   Doppel.

E-Mail-Anhänge aus dem überwachten Postfach laufen durch dieselbe Pipeline.

---

## Kurzreferenz — was ich selbst per Chat erledigen kann

| Ich möchte … | Chat-Frage |
|---|---|
| Ein Dokument finden | *„Suche &lt;Begriff&gt;"* |
| Verarbeitungsstatus / was hängt | *„Wie ist der Verarbeitungsstatus?"* |
| Paperless-Ablagestatus | *„Wie ist der Verarbeitungsstatus?"* (enthält Paperless-Zahlen) |
| Nicht abgelegte Dokumente auflisten | *„Welche Dokumente sind nicht in Paperless?"* |
| Ein Dokument in Paperless prüfen | *„Ist die Rechnung X in Paperless?"* |
| Fehlgeschlagene Ablagen erneut ablegen | *„Lege die fehlgeschlagenen Dokumente in Paperless ab."* |
| Dokumente ohne Chunks auflisten | *„Welche Dokumente haben keine Chunks?"* |
| Dokumente ohne Chunks reparieren | *„Indexiere die Dokumente ohne Chunks neu."* |
| Paperless-Duplikate aufräumen | *„Finde und lösche die Duplikate in Paperless."* |
| Dubletten in der Wissensbasis finden | *„Gibt es Dubletten in der Wissensbasis?"* (Prüfung unter `/brain/review`) |
| Dokument an Simba senden *(xidra)* | Aktion **„An Simba senden"** auf der Dokumentzeile |

> Funktioniert eine Chat-Frage einmal nicht wie erwartet, findest du dieselben
> Informationen auch in der Oberfläche unter `/wissen` (Suche, Status, Prüfen).
