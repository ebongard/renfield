# Circles — Zugriffsebenen im persönlichen Wissensnetz

Renfields **Circles** sind das Autorisierungsmodell über den persönlichen Wissensdaten. Kein RBAC, kein Sharepoint-Folder: eine personenzentrische Ringstruktur, die abbildet, wie Menschen ihr Wissen im echten Leben staffeln — etwas gehört *nur mir*, etwas meinen *vertrauten Nahen*, etwas dem *Haushalt*, etwas *benannten Außenstehenden*, und ein kleiner Teil *öffentlich*.

Alle Retrieval-Wege in Renfield (RAG, Knowledge Graph, Memory) ziehen Circles als Filter heran. Wer Zugriff hat, entscheidet der Circle-Graph — nicht das Dateisystem, nicht ACLs.

---

## Die fünf Stufen

| Tier | Name | Bedeutung |
|---|---|---|
| 0 | **self** | nur der Eigentümer |
| 1 | **trusted** | 1–3 engste Vertraute (Partner, enge Freunde) |
| 2 | **household** | Familie / Mitbewohner |
| 3 | **extended** | benannte Außenstehende (Kollegen, weitere Verwandtschaft) |
| 4 | **public** | jeder |

Die Leiter ist **monoton**: Tier *n* schließt alle niedrigeren Stufen ein. Wer im `household` ist, sieht *self* und *trusted* explizit **nicht**, aber alles was auf Tier 2 markiert ist. Eine Information erhält **eine** Tier-Zuweisung und ist damit vollständig beschrieben.

**Wichtig**: die Leiter ist *pro Eigentümer* definiert. Jeder Nutzer hat eine eigene Circle-Dimension, eigene Mitgliederlisten, eigene Entscheidungen — Alice' „trusted" und Bobs „trusted" sind verschiedene Mengen.

---

## Zugriffsregel (4-Zweig-Filter)

Ein Nutzer A sieht ein Atom mit Eigentümer O und Tier T wenn **eine** der folgenden Bedingungen erfüllt ist:

1. **OWNER** — A == O (der Eigentümer sieht immer alles Eigene)
2. **PUBLIC** — T == 4 (öffentliche Atome hat jeder)
3. **EXPLICIT GRANT** — in `atom_explicit_grants` existiert ein Eintrag `(atom_id, granted_to_user_id=A)` (der Notion/Drive/Dropbox-Ausnahme-Fall: *„teile dieses eine Dokument mit dieser einen Person, ohne ihre Tier-Einstufung anzufassen"*)
4. **TIER-REACH** — über `circle_memberships` ist A in einem Circle von O, dessen Tier ≤ T ist (Leiter-Reichweite)

Die Vier-Zweig-Logik wird einmal zentral formuliert in `services/circle_sql.py` und in jedes Retrieval-Modul als SQL-`WHERE`-Klausel injiziert — kein Retrieval-Code schreibt die Policy selbst.

**Edge-Case — Single-User-Mode**: bei `AUTH_ENABLED=false` wird die gesamte Filterlogik übersprungen (OR-Short-Circuit). Ein einzelner Nutzer sieht alles, unabhängig von Tiers.

---

## Atoms — der polymorphe Identitätsträger

Jede „Informationseinheit" die einen Circle tragen soll, bekommt einen Eintrag in der `atoms`-Tabelle. Ein Atom ist:

```
atom_id           — UUID, primary key
atom_type         — "document_chunk" | "kg_entity" | "kg_relation" | "conversation_memory" | ...
source_table      — Name der Quell-Tabelle ("document_chunks", "kg_entities", ...)
source_id         — ID in der Quell-Tabelle (Text, da heterogen)
owner_user_id     — der Eigentümer
policy            — JSON: {"circle_tier": 2, ...}
```

Die Quell-Tabellen tragen **denormalisiert** eine `circle_tier`- und `atom_id`-Spalte — nicht als Wahrheitsquelle, sondern als Performance-Abkürzung: das Retrieval kann in einem einzigen `JOIN` filtern, ohne jedes Mal über `atoms.policy` aufzulösen.

**Invariante**: Schreibzugriffe auf source_tables gehen **ausschließlich** über `AtomService.upsert_atom`. Direkter `INSERT` in `document_chunks`/`kg_entities`/etc. ist durch Code-Review + einen CI-Lint verboten. Das garantiert, dass `atoms` und die denormalisierten Spalten nicht auseinanderlaufen.

### Tier-Cascade

Wenn der Eigentümer eine Tier-Änderung an einem Atom vornimmt (`PATCH /api/atoms/{id}`), kaskadiert die Änderung auf alle incidenten Relationen (bei `kg_entity`-Atoms) und aktualisiert die denormalisierten `circle_tier`-Spalten in einem Transaktions-Schritt. Konsistenz zwischen `atoms.policy` und Quell-Tabelle ist ein ACID-Commit.

---

## Datenmodell

| Tabelle | Zweck |
|---|---|
| `atoms` | polymorphe Registry, ein Eintrag pro Circle-tragende Information |
| `atom_explicit_grants` | Ausnahme-Grants: `(atom_id, granted_to_user_id, permission_level)` |
| `circles` | Pro-User Tier-Konfiguration (optionale Anpassung der 5 Standard-Stufen) |
| `circle_memberships` | Wer ist in welchem Tier-Ring welches Eigentümers |
| `kb_shares` | Knowledge-Base-Level Share → explodiert in Per-Chunk-Grants (siehe `kb_shares_service.py`) |

**Denormalisierte Spalten**: `document_chunks.circle_tier`, `document_chunks.atom_id`, dasselbe Paar auf `kg_entities`, `kg_relations`, `conversation_memories`.

---

## Retrieval-Pfade

Jedes Subsystem, das Inhalte dem LLM präsentiert, wendet den Circle-Filter an:

| Modul | Eingang | Filterstelle |
|---|---|---|
| `services/rag_retrieval.py` | `rag.search(query, user_id=asker)` | SQL-WHERE über `circle_sql.build_filter()` |
| `services/kg_retrieval.py` | KG-Entity-Lookup aus Agent-Prompt | dieselbe `build_filter()`-Funktion |
| `services/memory_retrieval.py` | `ConversationMemoryService.retrieve(user_id=asker)` | dito |
| `services/polymorphic_atom_store.py` | `/api/atoms` Cross-Source-Suche | Reciprocal Rank Fusion über alle vier Atom-Typen mit Filter pro Source |

**Verhaltensänderung vor/nach Circles**: `ConversationMemoryService.retrieve()` respektiert nun Circle-Reichweite — Tier-2-Haushaltsmitglieder sehen die Memories der anderen auf Tier 2. Vorher war der Filter strikt `user_id == asker_id`. Aufrufer **müssen** `user_id=asker_id` übergeben; `None` reduziert im auth-enabled Modus auf `public` (Tier 4) allein.

---

## Cache & Policy Resolution

`services/circle_resolver.py` hält einen `PolicyEvaluator` mit In-Memory-Cache der Circle-Memberships pro Eigentümer. Der Cache wird bei Membership-Änderungen (`POST /api/circles/me/members`) invalidiert. Resolver-Aufrufe außerhalb der SQL-Filter-Pfade (z.B. einzelner `can_access_atom`-Check vor einer Schreiboperation) gehen durch diesen Cache.

---

## Services

| Service | Zuständigkeit |
|---|---|
| `services/atom_service.py` | `upsert_atom`, Tier-Cascade, Atom-Löschung |
| `services/circle_resolver.py` | `PolicyEvaluator` + Cache, Access-Checks außerhalb von SQL |
| `services/circle_sql.py` | `build_filter(user_id)` — die zentrale WHERE-Klausel-Factory |
| `services/polymorphic_atom_store.py` | Cross-Source-Retrieval mit RRF für `/api/atoms` |
| `services/kb_shares_service.py` | KB-Level-Share → generiert pro Chunk einen `AtomExplicitGrant` |

---

## HTTP-Routen

| Route | Zweck |
|---|---|
| `GET /api/atoms` | Unified Cross-Source-Search (`/brain` Frontend) |
| `PATCH /api/atoms/{id}` | Tier ändern; cascade auf incidente Relationen |
| `GET /api/circles/me` | Eigene Circle-Konfiguration laden |
| `GET /api/circles/me/members` | Mitgliederlisten pro Tier |
| `POST /api/circles/me/members` | Mitglied in einem Tier hinzufügen/entfernen |
| `GET /api/circles/me/review` | Review-Queue (Atome, die der Eigentümer neu klassifizieren sollte) |
| `GET /api/knowledge-graph/circle-tiers` | Lokalisierte Leiter-Labels (de/en) |
| `PATCH /api/knowledge-graph/entities/{id}/circle-tier` | Tier pro KG-Entity; Cascade |

---

## Frontend-Seiten

| Route | Funktion |
|---|---|
| `/brain` | Cross-Source-Suche über eigene Wissensebene |
| `/brain/review` | Review-Queue: vom System vorgeschlagene Tier-Zuweisungen bestätigen |
| `/settings/circles` | Circle-Mitglieder pro Stufe verwalten |
| `/settings/circles/peers` | Federations-Peers (für externe Anfragen über die Circle-Grenze) |

Shared Komponenten: `TierBadge` + `TierPicker` nutzen die `.tier-badge-{0..4}`-Utilities aus `src/frontend/src/index.css` — farbliche Zuordnung *self* (warm) → *public* (kühl), siehe `DESIGN.md`.

---

## Anti-Patterns

- **Direkter INSERT in Source-Tabellen** — umgeht AtomService, produziert inkonsistente denormalisierte Spalten. Review blockiert.
- **Retrieval mit `user_id=None` im auth-enabled Modus** — reduziert die Ergebnisse still auf Tier 4 alleine. Jede `rag.search()`-Call-Site muss den Asker übergeben.
- **Tier auf Source-Tabelle statt über AtomService ändern** — die `atoms.policy`-Quelle bleibt dann falsch.
- **Circles mit RPBAC verwechseln** — RPBAC (`docs/ACCESS_CONTROL.md`) ist eine Schicht darunter (*kann der Nutzer überhaupt mit Renfield sprechen?*); Circles ist darüber (*wessen Wissen bekommt er zu sehen?*).

---

## Siehe auch

- [SECOND_BRAIN.md](SECOND_BRAIN.md) — Überblick über die vier Wissenssysteme, die Circles als Filter verwenden
- [ACCESS_CONTROL.md](ACCESS_CONTROL.md) — RPBAC (Authentifizierung + Rollen, orthogonal zu Circles)
- `CLAUDE.md` — Developer-zentrische Zusammenfassung mit Code-Referenzen
