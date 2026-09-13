# Kontaktpunkte — Telefon, Mobil, Fax, Mail, Web an Personen und Firmen

**Issue:** [#1240](https://github.com/ebongard/renfield/issues/1240)
**Status:** Review-Entscheidungen getroffen 2026-09-13 (§11) — **kein Go zum Bauen**; Stufe 1 braucht ein ausdrückliches Go
**Datum:** 2026-09-13
**Autor:** Claude Opus 5
**Bezug:** #875 / PR #1213 (Gültigkeitsintervalle auf `kg_relations`)

---

## 1. Das Problem, gegen den Code geprüft

Renfield hat heute **keinen Ort für Kontaktdaten**. Wer im Chat eine Handynummer nennt, eine
Rechnung mit Briefkopf ablegt oder eine Mail bekommt, kann die Nummer oder Adresse danach nicht
abfragen. „Schreib Hans per WhatsApp" scheitert schon daran, dass die Nummer fehlt.

Das ist teils Absicht:

- **Der Wissensgraph schließt Kontaktdaten zweifach aus.** Alle vier Varianten von
  `prompts/knowledge_graph.yaml` führen „Telefonnummern", „E-Mail-Adressen" und „Faxnummern" unter
  *IGNORIERE / NEVER extract*. Unabhängig davon verwirft
  `KnowledgeGraphService._is_valid_entity` (`knowledge_graph_service.py:349`) jeden Namen, der auf
  `_RE_PHONE` oder `_RE_EMAIL` passt, keinen Buchstaben enthält oder zu über 50 % aus Ziffern
  besteht. Aufgerufen wird die Prüfung in beiden Extraktionspfaden — Chat (`extract_and_save`) und
  Dokumente (`extract_from_text`).
- **Schicht A** kennt die Kategorie `identifier`, extrahiert deterministisch aber nur Steuernummer
  und IBAN, per LLM etwa Vertragskonten. Keine Kontaktart.
- **Es gibt keine Kontakt-Anbindung.** Unter den MCP-Servern in `config/mcp_servers.yaml` ist
  weder CardDAV noch ein Adressbuch noch WhatsApp.
- **Eine Quelle existiert schon, wird aber nicht genutzt:** `email_ingest_log.sender`
  (`String(320)`) speichert den Absender jeder per Email-Ingest gepushten Anlage, zusammen mit
  `document_id`.

**Der Filter im Wissensgraphen ist richtig und bleibt.** Er hält Knoten frei von Nummern, IDs und
IBANs. Es fehlt ein Ort, an dem solche Werte als **Eigenschaft** einer Person oder Firma stehen
dürfen.

## 2. Nicht-Ziele

- **Keine Nummern oder Adressen als Knoten.** Siehe §3.
- **Kein Kontaktieren.** Kein WhatsApp-, SMS- oder Mailversand in diesem Vorhaben. v1 speichert
  und findet. Versand ist ein eigenes Folgevorhaben.
- **Keine Aufweichung von `_is_valid_entity`.**
- **Keine Postanschriften in v1.** Strukturiert schwierig (Adressfelder, Mehrzeiligkeit) und für
  „kontaktieren" nicht nötig. **Entschieden:** später als `kind = postal` mit strukturiertem Wert in
  **derselben Tabelle** — das Schema lässt dafür Platz.

## 3. Warum eine eigene Tabelle und keine Knoten

Naheliegend wäre, `+49 170 …` als Entität anzulegen und per Relation `hat_telefon` anzuhängen.
Drei Gründe dagegen:

1. **Falsche Zusammenführungen.** Eine Firmenzentrale, eine Familien-Festnetznummer oder eine
   gemeinsame Info-Adresse wäre *ein* Knoten mit vielen eingehenden Kanten. Über diesen Knoten
   rücken fremde Personen in der Graph-Expansion (`graph_expansion.expand_fused`) zu Nachbarn
   zusammen — genau die Art Magnet, die `kg_demagnetize` für Personenbeschreibungen aufräumen
   musste.
2. **Der Filter müsste fallen**, und mit ihm der Schutz gegen OCR-Müll, Aktenzeichen und IBANs.
3. **Mehrwertigkeit.** Eine Person hat mehrere gleichzeitig gültige Nummern (Mobil, geschäftlich,
   Fax). Das ist keine Beziehung zwischen zwei Dingen, sondern ein Attribut mit Liste — und passt
   nicht zur Widerspruchslogik von #875, die gerade *funktionale* Prädikate braucht.

## 4. Schema

```sql
CREATE TABLE kg_entity_contact_points (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    user_id         INTEGER NULL REFERENCES users(id),     -- Eigentümer, wie kg_entities
    kind            VARCHAR(16) NOT NULL,   -- phone | mobile | fax | email | web
    label           VARCHAR(16) NULL,       -- private | work | NULL (unbekannt)
    value           TEXT NOT NULL,          -- wortgetreu, wie in der Quelle
    normalized      TEXT NOT NULL,          -- E.164 | kleingeschriebene Mail | Host+Pfad
    source_type     VARCHAR(16) NOT NULL,   -- chat | document | email | vcard | manual
    source_ref      TEXT NULL,              -- message_id | document_id | email_ingest_log.id | Import-Id
    confidence      FLOAT NULL,
    circle_tier     INTEGER NOT NULL DEFAULT 0,
    valid_from      TIMESTAMP NULL,         -- gleiche Semantik wie #875
    valid_to        TIMESTAMP NULL,
    superseded_by_id INTEGER NULL REFERENCES kg_entity_contact_points(id) ON DELETE SET NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

-- Ein Wert hängt höchstens einmal gültig an derselben Entität.
CREATE UNIQUE INDEX uq_contact_points_live
    ON kg_entity_contact_points (entity_id, kind, normalized)
    WHERE valid_to IS NULL;

-- Rückwärtssuche: „wem gehört diese Nummer?"
CREATE INDEX idx_contact_points_normalized ON kg_entity_contact_points (normalized);
```

**Gültigkeit wie in #875, aber ohne Abhängigkeit.** Die Spaltennamen und die NULL-Semantik
(`valid_from` NULL = Anfang unbekannt, `valid_to` NULL = gilt weiterhin) sind bewusst identisch.
Baut #875 Stufe 1 einen `kg_validity_sql`-Baustein, nutzt dieses Vorhaben ihn mit; baut es ihn
nicht, genügt hier eine Bedingung pro Lesepfad — es gibt nur wenige.

**`valid_to` ist hier kein Widerspruchsergebnis.** Ein Kontaktpunkt läuft nur ab, wenn eine Quelle
das *ausdrücklich* sagt („Hans hat eine neue Handynummer: …" ersetzt die bisherige `mobile`
desselben `label`) oder ein vCard-Re-Import den Wert nicht mehr enthält. Eine zweite Nummer allein
ist nie ein Grund.

## 5. Zugriff (Circles)

Kontaktdaten Dritter sind personenbezogene Daten und dürfen nicht weiter reichen als ihre Quelle.

- **Tier-Regel:** `circle_tier = LEAST(Tier der Entität, Tier der Quelle)` — dieselbe Regel wie
  `kg_relations` (MIN von Subjekt und Objekt). Ein Merge, ein Re-Tier oder ein neuer Kontaktpunkt
  macht einen Wert nie sichtbarer, als seine Quelle war. Die Tier-Kaskade in
  `AtomService.update_tier` für `kg_node` muss die Kontaktpunkte mitnehmen.
- **Lesefilter:** ein `contact_points_circles_filter` in `circle_sql.py`, nach dem Muster von
  `kg_relations_circles_filter`. Kein Lesepfad ohne ihn.
- **Kein eigener `atom_type` (entschieden, §11.1).** Zugriff allein über das eigene `circle_tier`
  und den Filter; explizite Freigaben wirken auf Entitätsebene. Kein neuer Typ im
  `PolymorphicAtomStore`, keine Atom-Zeile je Wert.

## 6. Normalisierung

| kind | normalisiert | Werkzeug |
|---|---|---|
| `phone` / `mobile` / `fax` | E.164 (`+491701234567`) | **`phonenumbers`** — neue Abhängigkeit (entschieden) |
| `email` | kleingeschrieben, validiert | `email-validator` — **bereits** in `requirements.txt` |
| `web` | Host + Pfad, ohne Schema und Tracking-Parameter | stdlib `urllib.parse` |

**Standardregion** für Nummern ohne Ländervorwahl kommt aus der Instanz-Konfiguration
(`CONTACT_POINTS_DEFAULT_REGION`, Standard `DE`) — eine Instanz mit Kunden in Österreich oder der
Schweiz braucht keinen Code-Change.

Ein Wert, der nicht normalisiert werden kann, wird **nicht** gespeichert. Das ist der Ersatz für den
Schutz, den `_is_valid_entity` im Graphen leistet.

`mobile` gegen `phone`: `phonenumbers` kann für DE den Nummerntyp bestimmen. Die Quelle darf das
überschreiben (eine vCard sagt `CELL` ausdrücklich).

## 7. Zuordnung zur Entität

Der schwierigste Teil ist nicht das Erkennen einer Nummer, sondern **wem** sie gehört.

- **Anhängen über die bestehende Kaskade** `resolve_entity` (Name → surface form → …), gescoped auf
  `person` bzw. `organization`. Personen lösen dort ohnehin nur exakt auf, nie per Embedding.
- **Keine unsichere Zuordnung.** Ist der Name nicht eindeutig oder fehlt er, wird der Kontaktpunkt
  **nicht** geraten, sondern verworfen (Chat, Dokument) bzw. zur Prüfung vorgelegt (vCard).
- **Geteilte Werte führen nie Entitäten zusammen.** Hängt dieselbe normalisierte Nummer schon an
  einer anderen Entität, bekommt die neue Entität *zusätzlich* einen Kontaktpunkt. Das ist
  legitim (Zentrale, Familienfestnetz). Der Reconciler darf „gleiche Nummer" **nicht** als
  Merge-Signal verwenden.
- **Grounding wie `kg_validator` Regel 2:** Ein extrahierter Wert muss wortgetreu im Quelltext
  stehen. Ein vom LLM ergänzter oder „korrigierter" Wert wird verworfen.

## 8. Die vier Quellen

### 8.1 vCard-Import

Strukturiert, deterministisch, kein LLM — deshalb die erste Quelle.

- Upload einer `.vcf`-Datei; CardDAV-Abgleich ist eine spätere Erweiterung derselben Logik.
- `FN`/`N` → Person, `ORG` → Firma, `TEL;TYPE=CELL|WORK|FAX` → `kind` + `label`, `EMAIL`, `URL`.
- Parser: **`vobject`** — neue Abhängigkeit (entschieden). vCard hat genug Varianten (v2.1/3.0/4.0,
  Line-Folding, Encodings), dass ein eigener Parser die schlechtere Wahl wäre.
- **Legt fehlende Entitäten an**, auf einem beim Import gewählten Tier (Standard: `self`).
  Bestehende Personen werden exakt über den Namen gefunden; mehrdeutige Treffer gehen in eine
  Prüfliste statt geraten zu werden.
- **Re-Import ist ein Diff:** Werte, die in der neuen Datei fehlen, bekommen `valid_to`; neue
  kommen hinzu; unveränderte bleiben unberührt (idempotent über `source_ref` = Import-Kennung +
  vCard-`UID`).

### 8.2 Chat

- Im bestehenden Hintergrundpfad nach dem `done`-Frame (`chat_handler._extract_structured_background`),
  **kein zusätzlicher LLM-Aufruf**: Die KG-Extraktion (`extract_and_save`) bekommt ein
  zusätzliches Ausgabefeld `contact_points: [{entity, kind, label, value}]`.
- Die IGNORIERE-Zeilen im Prompt bleiben für *Entitäten* bestehen und werden präzisiert:
  „nicht als Entität — sondern als `contact_points` der Person/Firma".
- Grounding (§7) und Normalisierung (§6) laufen deterministisch nach dem LLM.
- Tier der Quelle: das der Konversation bzw. des Sprechers, wie bei den Relationen desselben Turns.

### 8.3 Dokumente

- Briefköpfe, Signaturen, Fußzeilen: Hier stehen Kontaktdaten fast immer — und fast immer die des
  **Absenders**, nicht des Empfängers.
- **Zweistufig:** Ein deterministischer Regex-Durchlauf findet Kandidaten; die Zuordnung übernimmt
  der bestehende KG-Dokumentpfad (`extract_from_text` über `kg_post_document_ingest_hook`) mit
  demselben `contact_points`-Feld wie im Chat.
- **Ausgenommen:** `source == meeting_transcript` — dort ist die KG-Extraktion bereits gezielt
  eingeschränkt (Phase 0 der Meeting-Redesign), und in Gesprächen genannte Nummern sind nicht
  zuverlässig einer Person zuzuordnen.
- Tier der Quelle: das Tier des Dokuments. Ein privates Dokument erzeugt keinen öffentlichen
  Kontaktpunkt einer öffentlichen Firma.
- **Über den KG-Dokumentpfad, nicht über Schicht A (entschieden, §11.4).** Die Zuordnung zur Firma
  oder Person entsteht dort ohnehin; in Schicht A müsste sie nachträglich gebaut werden. Den
  Vertrauensanker, den `document_facts.excerpt` bietet, übernimmt hier das Grounding aus §7 plus
  `source_ref` = `document_id`.

### 8.4 Mail-Absender

- `email_ingest_log.sender` enthält den Absender, `document_id` das daraus entstandene Dokument.
- `"Name <adresse>"` wird zerlegt; der Anzeigename löst die Entität auf (§7).
- **Ohne Anzeigenamen keine Zuordnung.** Eine nackte Adresse wird nicht zu einer neuen Person —
  sonst entstünde für jede Newsletter- und No-Reply-Adresse eine Entität.
- Tier der Quelle: das Tier des Postfach-Ziels (serverseitig über `mailbox_id` bestimmt, wie beim
  Dokument selbst).
- **Grenze, ehrlich benannt:** Der Watcher pusht nur Mails **mit Anlage**. Absender von Mails ohne
  Anlage erreichen das Backend nie und bleiben unerfasst. Das zu ändern ist ein Eingriff in
  `renfield-mcp-email-ingest`, nicht Teil von v1.

## 9. Finden

- **Agent-Tool `internal.find_contact`** (`name`, optional `kind`): löst die Entität auf und
  liefert die gültigen Kontaktpunkte, circle-gefiltert, mit Label und Quelle. Rückwärtssuche über
  `normalized` („wessen Nummer ist das?") im selben Tool.
  Registrierung in zwei Schritten wie jedes `internal.*`-Tool: `InternalToolService` **und**
  `config/agent_roles.yaml` (ConfigMap-served).
- **Wissen-Drawer:** Die Detailansicht einer Person/Firma zeigt ihre Kontaktpunkte mit Quelle.
- **Nicht** in den allgemeinen RAG-Kontext: Nummern gehören nicht in jede Antwort, nur in die, die
  danach fragt.

## 10. Stufen

| Stufe | Inhalt | Wirkung ohne die nächste |
|---|---|---|
| **1** | Migration, Modell, Circles-Filter + Tier-Kaskade, Normalisierung, `internal.find_contact`, manuelles Anlegen/Bearbeiten im Drawer | Kontaktdaten lassen sich pflegen und finden. Keine Automatik |
| **2** | vCard-Import mit Re-Import-Diff | Ein Adressbuch ist in einem Schritt drin — der größte Nutzen bei geringstem Risiko |
| **3** | Chat-Extraktion (Prompt-Feld + Grounding) | Genannte Nummern bleiben hängen |
| **4** | Dokumente + Mail-Absender | Briefköpfe und Absender füllen die Firmen-Seite |

Jede automatische Quelle (3, 4) hinter eigenem Flag, dunkel per Default. Stufe 1 und 2 sind
deterministisch und ohne LLM testbar.

## 11. Entscheidungen der Review (2026-09-13)

| # | Frage | Entscheidung |
|---|---|---|
| 1 | Eigener `atom_type` pro Kontaktpunkt? | **Nein.** Eigenes `circle_tier` + `contact_points_circles_filter`; Freigaben auf Entitätsebene — §5 |
| 2 | Neue Abhängigkeiten | **`phonenumbers` und `vobject`** — §6, §8.1 |
| 3 | Standardregion | **Instanz-Konfiguration** `CONTACT_POINTS_DEFAULT_REGION`, Standard `DE` — §6 |
| 4 | Dokumente: KG-Pfad oder Schicht A? | **KG-Dokumentpfad** (`extract_from_text`) — §8.3 |
| 5 | Postanschriften | **Später**, als `kind = postal` in derselben Tabelle — §2 |

Offen bleibt allein das **Go für Stufe 1**.

## 12. Risiken

- **R1 — Falsche Zuordnung (HOCH).** Die Nummer der Sachbearbeiterin hängt an der Firma, die des
  Absenders am Empfänger. Gegenmaßnahmen: keine unsichere Zuordnung (§7), Grounding, Quelle an
  jedem Wert sichtbar und im Drawer korrigier-/löschbar.
- **R2 — Datenleck über Tiers (HOCH).** Siehe §5: `LEAST`-Regel, eigener Filter, Tests je Lesepfad
  „Nicht-Mitglied sieht den Kontaktpunkt nicht".
- **R3 — Magnet über geteilte Werte (MITTEL).** §3 und §7: geteilte Werte führen nie Entitäten
  zusammen; Kontaktpunkte sind keine Kanten und nehmen an der Graph-Expansion nicht teil.
- **R4 — Datenmüll aus Newslettern und Signaturen (MITTEL).** Kein Anlegen von Personen aus nackten
  Adressen (§8.4), Normalisierungspflicht (§6).
- **R5 — Löschpflicht.** Wird eine Person gelöscht (Entität), müssen ihre Kontaktpunkte mit
  (`ON DELETE CASCADE` greift nur bei hartem Löschen; der Soft-Delete über `is_active` braucht eine
  ausdrückliche Kaskade).
