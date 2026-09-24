# KG-Namensurteil (Nachfolger von #1331) — Arbeitsliste

Entwurf: `docs/design/kg-name-adjudicator.md`. **Noch nicht freigegeben — erst
den Entwurf abnehmen, dann bauen.**

## Vor dem Bau zu entscheiden (Eigentümer)

- [ ] Entwurf abgenommen?
- [ ] Prompt-Sprache: Deutsch oder Englisch (§9.1) — messen, nicht raten
- [ ] Bestehende `name_typo`/`name_tokenization`-Gründe: umetikettieren oder aus
      dem Live-Zustand ableiten (§9.2)
- [ ] Zwischenspeicher in der lesenden Phase aus? (§9.3)

## Bauen

- [ ] `prompts/kg_name_adjudicator.yaml` — System + Nutzer, geschlossene
      Aufzählung für `grund`, Beispiele aus der Abnahmeliste
- [ ] `services/kg_name_adjudicator.py` — Bündelung zu 20, strenges JSON-Schema,
      `neutralize_delimiters` auf jedem Namen, Redis-Zwischenspeicher (30 d)
- [ ] `find_duplicate_pairs`: die vier Funktionen entfernen
      (`_names_near_typo`, `_osa_distance_is_one`, `_split_camel`,
      `_names_related_after_split`), Adjudikator einsetzen
- [ ] `_names_related` **unangetastet** lassen — einziges Tor zur stillen Faltung
- [ ] `ReconcileReport`: `adjudicated`, `adjudicator_failed`, `rescued_by_model`
- [ ] Flag `KG_NAME_ADJUDICATOR_ENABLED` (dunkel) + `docs/ENVIRONMENT_VARIABLES.md`
- [ ] Grund-Aufzählung in den drei Sprachdateien, Karte auf i18n abbilden

## Prüfen

- [ ] Ausfall scheitert geschlossen: Modell weg, Zeitüberschreitung, kaputtes
      JSON, unbekannter Aufzählungswert → verworfen, gezählt, nicht vorgelegt
- [ ] Das Modell erreicht `person_ok` nicht — Test, der beweist, dass ein
      „plausibel" keine stille Faltung auslöst
- [ ] Nur die beiden Namen und Typen gehen in den Prompt (keine Beschreibungen,
      keine Stufen, keine Dokumentinhalte)
- [ ] Abnahmeliste §8: fünf reva-Fälle plausibel, Verbundvornamen unplausibel,
      `XidraSystemsGmbH` plausibel
- [ ] Flag aus = Verhalten byte-identisch zu `_names_related` allein
- [ ] Gegenprobe je inhaltlichem Fix (ohne Fix muss der Test fallen)
- [ ] `/review` inkl. adversariellem Durchgang — er hat bei #1330 fünf von acht
      Commits erzeugt und bei #1331 den Befund, der den Zweig kippte

## Ausrollen

- [ ] Dunkel ausrollen, beide Instanzen
- [ ] **Lesende Phase:** Urteile protokollieren, nicht anwenden; gegen die
      Abnahmeliste halten
- [ ] reva misst gegen ihren Mehrnutzer-Graphen
- [ ] Erst dann scharf schalten

## Erledigt

- [x] #1330 Typriegel — live beide Instanzen, im Browser abgenommen
- [x] #1331 verworfen (sieben Heuristiken, ein Paar Ertrag) — Zweig steht,
      Befunde F1–F9 darin dokumentiert
- [x] `admin`-Passwort gedreht (Eigentümer, 2026-09-24)
