# Per-fact tier override (Schicht A) — plan (2026-06-06)

Branch: `feat/per-fact-tier-override`. Motivation: within one document, an issuer
can be public while the content (amount, Steuernummer) is private. Facts already
have their own `circle_tier` + atom + PATCH route, and retrieval already filters
by the fact's own tier — the ONLY blocker is the doc→facts cascade stomping them.

Decision (user): **sticky both ways** — an explicit per-fact override always
survives a later document re-tier, in both directions (public issuer stays public
even after the doc is privatized). Visible badge + reset action make it inspectable.

## Backend
- [ ] B1 model: `DocumentFact.tier_overridden Boolean NOT NULL default false`
- [ ] B2 migration pc20260608 (down_revision = pc20260607_oblig_digest) — add column server_default false
- [ ] B3 `AtomService.update_tier(..., *, fact_override=True)`: for a document_fact atom, set `tier_overridden = fact_override`. kb_document cascade UPDATEs (document_facts.circle_tier + facts' atoms.policy) gain `AND NOT tier_overridden` so overrides survive.
- [ ] B4 `AtomService.reset_fact_tier(fact_id)`: look up parent doc tier + fact atom_id → `update_tier(atom_id, {tier: doc_tier}, fact_override=False)` (clears flag, resets tier+policy+cache).
- [ ] B5 route `POST /api/atoms/documents/facts/{fact_id}/reset-tier` (owner-only, SELECT FOR UPDATE on the fact's atom)
- [ ] B6 response: `tier_overridden` on `_FACT_COLS` + `_row_to_dict` + `DocumentFactResponse`

## Frontend
- [ ] F1 DocumentFact type: `tier_overridden?: boolean`
- [ ] F2 `useResetFactTier` mutation (POST reset) in brain.ts
- [ ] F3 Wissen detail drawer (document_fact): the existing TierPicker now sets the override; add an "übersteuert/geerbt" indicator + "auf Dokument-Tier zurücksetzen" link (useResetFactTier)
- [ ] F4 FaktenPanel: read-only override indicator next to TierBadge (editing stays in the drawer; /knowledge can be a non-owner view)
- [ ] F5 i18n de+en

## Tests
- [ ] T1 backend PG: per-fact PATCH sets tier_overridden; doc re-tier skips overridden facts (sticky both ways — over-public override survives doc→private); non-overridden facts follow the doc; reset clears flag + restores doc tier; retrieval honors the fact's own tier
- [ ] T2 reset route owner-gated 404
- [ ] T3 frontend RTL: drawer reset calls endpoint; override indicator renders

## Notes / accepted
- Re-ingest purges+recreates facts → overrides reset on re-extraction (fresh fact set). Acceptable; note in PR.
- Sticky both ways means privatizing a doc does NOT auto-clamp an over-public override; the badge surfaces it (user decision).

## Then (separate PR): the two quick polish items
- Fakten filter chip on /brain (client-side type filter)
- .ics export of obligations (backend endpoint mirroring trajectory export)
