# KG-Namensurteil: das Modell entscheidet die Vorlage, nie die Faltung

**Status:** Entwurf, 2026-09-24. Nachfolger des verworfenen PR #1331.
**Betroffen:** `services/kg_reconciler_service.py`, `prompts/` (neu), `/brain/review`.
**Verwandt:** `docs/design/structured-memory.md` §Reconciler · `.claude/rules/kg-memory.md` · #1330 (Typriegel, live)

---

## 1. Warum

Der Abgleicher beantwortet eine **Bedeutungsfrage** — „bezeichnen diese beiden
Zeilen dasselbe Ding?" — mit **sieben Zeichenkettenfunktionen**:

```
_norm · _name_collision_low_signal · _names_related · _split_camel
· _names_related_after_split · _osa_distance_is_one · _names_near_typo
```

Der Abgleicher ruft dabei nie ein Modell; die einzige Zeile mit Modellbezug ist
`_get_embedding`. Am 2026-09-24 hat ein einziger Zweig (#1331) viermal dieselbe
Ursache vorgeführt:

| Fehlschlag | Was die Regel nicht wissen konnte |
|---|---|
| `XidraSystemsGmbH` → `Xidra Systems Gmb H` | dass `GmbH` eine Rechtsform ist. „Dafür bräuchte es ein Verzeichnis." |
| `Anna` ~ `AnnaLena` wurde gerettet | dass ein Verbundvorname **zwei Menschen** sind |
| `QA` ~ `QAEngineer` bewusst aufgegeben | dass beides eine **Rolle** ist, kein Mensch |
| `ProjektÜbersicht` trennte nie | dass `Ü` ein Großbuchstabe ist (`[A-Z]` ist ASCII) |

Der dritte Fall ist der Beweis: der Fix für `Anna`/`AnnaLena` hat `QA`/`QAEngineer`
mitgerissen, weil **kein Test über Zeichen eine Rolle von einem Menschen
unterscheidet**. Das ist keine Panne, sondern die Grenze der Methode.

Gemessener Ertrag von #1331 über drei Produktionsgraphen: **ein Paar**.

## 2. Was das Modell NICHT entscheidet

Das ist der Kern des Entwurfs, und er ist eine Konstruktions-, keine
Vertrauensfrage.

> **Das Modell kann nur die Prüfschlange verlängern, niemals etwas verschmelzen.**

Deterministisch bleiben, unverändert und vom Modell unerreichbar:

- **Stufe** — `loser_tier == winner_tier` (Sichtbarkeit, `tier = MIN`)
- **Typ** — `_types_compatible` auf dem Primärtyp (#1330)
- **Eigentümer** — `a.user_id = b.user_id`, im SQL
- **Ähnlichkeit** — `>= kg_reconciler_candidate_threshold`, im SQL
- **`person_ok`** — das Automatik-Tor liest weiter `_names_related`, siehe §3
- **`block_auto_merge`** — `_name_collision_low_signal` bleibt, wie es ist

Ein falsches „plausibel" kostet **einen Posten in der Prüfschlange**. Ein
falsches „unplausibel" kostet eine übersehene Dublette — derselbe Preis, den die
heutigen stillen Verwerfungen schon kosten. Keines von beidem kann den Graphen
beschädigen.

## 3. Die Aufteilung

Heute leisten die sieben Funktionen zwei verschiedene Dinge, und genau deshalb
sind sie unauflösbar verheddert. Der Entwurf trennt sie:

| | bleibt/kommt | Aufgabe |
|---|---|---|
| **`_names_related`** (gleich oder Token-Teilmenge) | **bleibt, deterministisch** | einziges Tor zur **stillen Faltung** (`person_ok`). Wird nie wieder aufgeweitet. |
| `_names_near_typo`, `_osa_distance_is_one`, `_split_camel`, `_names_related_after_split` | **entfallen** | existierten nur, um die **Vorlage** zu verbreitern |
| **Adjudikator** (neu) | **kommt** | entscheidet allein, was dem Eigentümer **vorgelegt** wird |

Damit ist die Sicherheitszusage beweisbar statt beteuert: das Modell taucht im
Auto-Merge-Pfad nicht auf. `_names_related` bleibt als die einfachste der sieben
Funktionen stehen — und sie ist heute schon die einzige, die das Tor bedient.

## 4. Was gefragt wird

Eine Frage, und es ist die, die tatsächlich zählt:

> **Ist es plausibel, dass diese beiden Zeilen dasselbe reale Ding bezeichnen —
> so plausibel, dass ein Mensch hinsehen sollte?**

Nicht „sind sie dasselbe" (das kann das Modell nicht wissen) und nicht „ist der
Name gleich geschrieben" (das ist die Zeichenkettenfrage, an der wir gescheitert
sind). Die Frage ist auf **Trefferquote** gestellt, nicht auf Genauigkeit: die
Prüfschlange ist billig, eine stille Faltung nicht.

Rückgabe je Paar, als strenges JSON:

```json
{"i": 3, "plausibel": true, "grund": "tokenisierung", "sicherheit": "hoch"}
```

`grund` ist eine **geschlossene Aufzählung**, kein Freitext:
`schreibweise | abkuerzung | tokenisierung | rollenvariante | teilbegriff | anderes`.
Das erhält die bestehende Fluchtgrenze — die Karte rendert nie Modelltext,
sondern bildet die Aufzählung auf i18n-Schlüssel ab, wie heute `cross_tier` und
`gray_zone`. Der Grund ersetzt `name_typo` und `name_tokenization`; die Rangfolge
`cross_tier` > `cross_type` > `<Modellgrund>` > `gray_zone` bleibt.

Erwartete Urteile auf den bekannten Fällen:

| Paar | plausibel | Grund |
|---|---|---|
| `Product Owner` ~ `ProductOwner` | ja | tokenisierung |
| `XidraSystemsGmbH` ~ `Xidra Systems GmbH` | ja | schreibweise |
| `QA` ~ `QAEngineer` | ja | rollenvariante |
| `Anna Schmidt` ~ `Anna Schmitt` | ja | schreibweise |
| **`Anna` ~ `AnnaLena`** | **nein** | — |
| `Release` ~ `HelmRelease` | nein | — |
| `Product A - 1.2.4` ~ `Product A - 1.2.3` | nein | — |

Die letzte Zeile ist #1329, das der heutige Abgleicher **automatisch verschmolzen
hat**. Der Adjudikator löst es nicht direkt (er entscheidet die Vorlage, nicht
die Faltung), aber er macht sichtbar, dass die Frage stellbar ist.

## 5. Ausfall, Wiederholbarkeit, Vertrauensgrenze

**Ausfall scheitert geschlossen, und zwar als Abstufung, nicht als Absturz.**
Modell nicht erreichbar, Zeitüberschreitung, kaputtes JSON, unbekannter
Aufzählungswert → das Paar wird behandelt, als hätte das Modell „unplausibel"
gesagt. Es gilt dann allein `_names_related`, also exakt das Verhalten von vor
#1330 ohne die Typo-Ausnahme. Gezählt in `ReconcileReport.adjudicator_failed`;
eine Logzeile pro Lauf, nicht pro Paar.

**Wiederholbarkeit.** Ein Urteil hängt nur von den beiden Namen ab, nicht vom
Graphen. Zwischenspeicher in Redis, Schlüssel = Hash über die beiden
normalisierten Namen in fester Reihenfolge, Haltbarkeit 30 Tage. Das beseitigt
den Wiederholungsaufwand für dauerhaft verworfene Paare (die heute in **jedem**
Lauf neu bewertet würden) und macht zwei Läufe innerhalb der Haltbarkeit
reproduzierbar.

**Vertrauensgrenze.** Entitätsnamen stammen aus LLM-Extraktion von
Nutzerdokumenten, sind also **unvertrauenswürdig**. Jeder Name läuft durch
`utils/prompt_safety.neutralize_delimiters` (#686), bevor er in den Prompt geht.
Die Antwort wird als strenges JSON gegen ein festes Schema gelesen; alles andere
gilt als Ausfall (siehe oben). Das Modell sieht **nur die beiden Namen und die
beiden Typen** — keine Beschreibungen, keine Dokumentinhalte, keine Kreisstufen.

## 6. Aufwand

- **Bündelung:** ein Aufruf je 20 Paare. Beim Kappungswert von 100 also
  **≤ 5 Aufrufe je Nutzer und Lauf**, nicht 100.
- **Modelltier:** `ollama_intent_model` (kurze Klassifikation) mit
  `get_classification_chat_kwargs` — dieselbe Schiene wie Router und
  Absichtserkennung, Denkmodus aus.
- **Takt:** der Abgleicher läuft täglich (`kg_reconciler_interval`, 86 400 s).

## 7. Ausrollen

Dunkel: `KG_NAME_ADJUDICATOR_ENABLED=false`. Flag aus → der Pfad ist
**byte-identisch** zum heutigen Verhalten minus der entfallenen vier Funktionen,
also `_names_related` allein. Neue Umgebungsvariablen nach
`docs/ENVIRONMENT_VARIABLES.md`.

Reihenfolge: ausrollen → **lesend** gegen beide Bestände messen (der Adjudikator
läuft, sein Urteil wird protokolliert, aber nicht angewandt) → Urteile gegen die
Abnahmeliste halten → erst dann scharf schalten.

## 8. Abnahme

Von der reva-Instanz beigesteuert, gegen ihren Produktionsgraphen gemessen — und
diesmal überleben **alle sieben**, weil das Modell nicht am Zeichen raten muss:

| Paar | Ähnlichkeit | erwartet |
|---|---|---|
| `concept 'Product Owner'` ~ `person 'ProductOwner'` | 0,9030 | plausibel |
| `person 'Security'` ~ `person 'SecurityEngineer'` | 0,5659 | plausibel |
| `thing 'QA'` ~ `person 'QAEngineer'` | 0,5801 | plausibel |
| `person 'Management'` ~ `thing 'DocumentManagement'` | 0,5242 | plausibel |
| `person 'BackendEngineer'` ~ `thing 'backend'` | 0,6025 | plausibel |

Aus dem Haushaltsgraphen, als Gegenprobe (7 Personenzeilen dieser Form):

| Paar | erwartet |
|---|---|
| `person` Vorname ~ `person` zusammengeschriebener Doppelname | **unplausibel** |

Dazu die Grenze aus #1331: `XidraSystemsGmbH` ~ `Xidra Systems GmbH` muss
**plausibel** sein — der Fall, für den keine Stellungsregel existiert.

🛑 **Die Negativkontrollen wiegen schwerer als die Positivfälle.** Ein viel zu
großzügiger Adjudikator besteht jeden Positivfall. Die Abnahme ist bestanden,
wenn die Positivfälle **und** die Verbundvornamen stimmen.

## 9. Offen, vor dem Bau zu entscheiden

1. **Sprache des Prompts.** Die Bestände sind deutsch, das Modell ist mehrsprachig.
   Deutscher Prompt oder englischer mit deutschen Namen darin? Messen, nicht raten.
2. **Was mit `name_typo` und `name_tokenization` in bestehenden Vorschlägen?**
   Die Schlange trägt gespeicherte Gründe. Umetikettieren oder stehen lassen und
   die Karte aus dem Live-Zustand ableiten (wie bei #1330 gelöst)?
3. **Zählt ein zwischengespeichertes Urteil als Messung?** Für die lesende
   Phase §7 vermutlich nein — dort sollte der Zwischenspeicher aus sein.
4. **#1329** (Auto-Merge über Versionsnummern) bleibt ein eigener Vorgang. Der
   Adjudikator berührt ihn nicht, weil er die Faltung nicht entscheidet.
