# Sammel-PDF aus dem Chat in Paperless ablegen

Stand: 2026-11-08 · Status: gebaut (Variante A), Modul `services/chat_paperless_split.py` · Bezug: `docs/design/pdf-split.md`

## Anlass

Auf der Instanz „mama“ wurde ein PDF mit mehreren Schreiben in den Chat gelegt und „in Paperless speichern“
verlangt. Ergebnis: ein einziges Paperless-Dokument (bzw. wegen eines zweiten, inzwischen behobenen Fehlers gar
keines). `pdf-split.md` nimmt den Chat-Weg ausdrücklich aus („deliberately out of scope“) — mit der Begründung
Wartezeit und anwesender Nutzer.

## Was es schon gibt (am Code nachgesehen)

- Ein Chat-Anhang wird mit `CHAT_UPLOAD_AUTO_INDEX` (Vorgabe an) sofort zum Wissensdokument:
  `_auto_index_to_kb` (`api/routes/chat_upload.py`) legt die `Document`-Zeile an und reiht sie beim
  document-worker ein. Dort läuft die Split-Vorstufe `maybe_split_at_ingest` **bereits heute**, sobald
  `PDF_SPLIT_ENABLED` an ist. Die Ausnahme in `pdf-split.md` stimmt also schon jetzt nur halb.
- Die Kinder eines Schnitts erben die Paperless-Ablage vom Elternteil (`pdf_splitter.py:350`: nur ein Elternteil mit
  `paperless_state='pending'` gibt `file_to_paperless` weiter). Chat-Dokumente tragen `paperless_state=NULL` —
  ihre Kinder werden darum nie abgelegt.
- „In Paperless ablegen“ aus dem Chat (`internal.forward_attachment_to_paperless`, REST
  `POST /upload/{id}/paperless`) schickt die **Originalbytes** des Anhangs, ohne Blick auf die Wissensseite.

## Vorschlag: kein zweiter Splitter — die Ablage wird schnittbewusst

Der Schnitt passiert ohnehin beim Einlesen, lange bevor der Nutzer „ablegen“ sagt. Die Ablage fragt deshalb zuerst
das Wissensdokument des Anhangs:

| Zustand des Wissensdokuments | Ablage |
|---|---|
| kein Schnitt (Einzeldokument, Nicht-PDF, Schalter aus) | wie heute: Extraktion → Bestätigungskarte bzw. stiller Weg — byte-identisch |
| geschnitten (`split_archived`, Kinder vorhanden) | **jedes Kind** einzeln: `paperless_state='pending'` setzen und den bestehenden Ablage-Nachlauf anstoßen (`refile_document_paperless`) — Original NICHT ablegen |
| Schnitt wartet auf Prüfung (`split_review`) | nichts ablegen; Antwort: „Das PDF enthält vermutlich mehrere Schreiben — bitte unter Gehirn → Prüfen bestätigen, danach lege ich die Teile ab.“ Nach der Freigabe legt der Nachlauf die Kinder ab (Ablageabsicht am Elternteil vermerkt) |
| Schnitt läuft noch (`split_pending`, Einlesen nicht fertig) | Ablageabsicht am Elternteil vermerken, Antwort „wird nach dem Aufteilen abgelegt“; die Kinder erben sie über den bestehenden Weg (`pending` am Elternteil) |

Der Ablage-Nachlauf (`paperless_filing_post_ingest_hook` / `make_paperless_leg`) ist derselbe wie für Ordner- und
Mail-Eingang: idempotent, mit Wiederholung und Abgleicher. Er extrahiert die Metadaten selbst.

## Die Entscheidung, die offen ist: Bestätigungskarte je Teil?

Der Chat-Weg zeigt in den ersten `PAPERLESS_COLD_START_CONFIRM_N` Ablagen eine Karte. Der Ablage-Nachlauf kennt
keine. Zwei Möglichkeiten:

- **A — Teile still ablegen** (wie der Ordner-Eingang), im Chat eine Sammelmeldung mit den Titeln. Einfach,
  erprobt; die Einwilligung liegt in „lege das ab“. Neue Tags/Korrespondenten legt der Nachlauf nach seinen eigenen
  Regeln an (`PAPERLESS_AUTOCREATE_*`).
- **B — eine Karte je Teil.** Hält die Kaltstart-Regel ein, ist aber bei neun Schreiben neun Karten und braucht
  eine neue Verbindung Nachlauf → Chat-Karte (heute gibt es nur den umgekehrten Weg).

**Entschieden: A** (2026-11-08). Die Sammelmeldung nennt die Teile im Augenblick der Bitte; die Ablage selbst
meldet sich nicht zurück im Chat. B lohnt erst, wenn sich A als zu unkontrolliert zeigt.

## Weitere Lücken, die der Bau schließt

- Die Fortschrittsabfrage in `_auto_index_to_kb` kennt nur `completed`/`failed`; ein Elternteil in
  `split_review`/`split_archived` endet nach 30 min als `document_error` oder meldet nie. Sie muss die
  `split_*`-Zustände als Endzustände kennen und „aufgeteilt in N“ melden.
- Der Auftrag aus dem Chat reist mit `user_id=None`; der Prüfhinweis fällt auf den Atom-Eigentümer zurück. Das ist
  bereits der Hochladende (die Ablage „Chat Uploads“ gehört ihm, `get_or_create_owned_kb`) — kein Eingriff nötig;
  `user_id` mitzugeben änderte zudem die Einlese-Hooks (KG, Schicht A).
- Der manuelle Weg `POST /upload/{id}/index` liest inline im API-Pod ein, ohne Split-Vorstufe — bleibt
  unverändert (er ist kein Ablageweg), wird aber hier vermerkt.

## Gebaut: die Absicht ist DAUERHAFT und läuft zusammen

Die Bitte kann vor dem Wissensdokument eintreffen (Anhang und „lege ab“ in einer Nachricht). Darum steht sie auf
`chat_uploads.paperless_intent` (`requested` → `settled`, Migration `pc20261108_chat_pl_intent`), und
`settle_intent` überträgt sie — idempotent, nur NULL → `pending` — von DREI Stellen: der Bitte selbst, dem
Auto-Index direkt nach dem Anlegen der Dokumentzeile, und jedem Lauf des Paperless-Abgleichers
(`settle_open_intents`). Jede Verschränkung läuft spätestens beim nächsten Takt zusammen; ein Schnitt, dessen
Kinder schon vor dem `pending` am Elternteil entstanden, wird dort nachgezogen.
🛑 Ein fertiges Einzeldokument und ein gescheitertes bleiben auf dem alten Weg (Karte bzw. Direktablage) — der
neue Weg greift nur, wo der Schnitt mitzureden hat.
Nebenbei behoben: die Fortschrittsabfrage kennt `split_archived` als fertig und `split_review` als Ende.

## Schalter und Grenzen

- Kein neuer Schalter: das Verhalten hängt an `PDF_SPLIT_ENABLED` (aus → byte-identisch zu heute), an
  `CHAT_UPLOAD_AUTO_INDEX` (aus → kein Wissensdokument → Ablage wie heute) und an einem der Ablage-Schalter
  `FOLDER_INGEST_TO_PAPERLESS`/`EMAIL_INGEST_TO_PAPERLESS` (ohne sie gibt es weder Hook noch Abgleicher).
- 🛑 Seitenzahl bleibt kein Signal (Regel aus `pdf-split.md`).
- 🛑 Das archivierte Original wird nie abgelegt, auch nicht über den REST-Weg.
- Die Wartezeit im Chat entsteht nicht neu: der Schnitt läuft beim Hochladen im Worker, nicht im Chatzug.

## Tests (geplant)

Je Zeile der Tabelle ein Fall gegen echtes Postgres (`renfield_test`), dazu: Schalter aus → Ablage byte-identisch;
REST- und Agentenweg verhalten sich gleich; zweimal „ablegen“ legt kein Kind doppelt ab (Idempotenz des Nachlaufs).
Auf „mama“ zusätzlich `PDF_SPLIT_ENABLED=true` setzen und mit dem gemeldeten Sammel-PDF Ende zu Ende prüfen.
