import type { MergeProposal, MergeProposalEntityBrief } from '../api/resources/knowledgeGraph';
import type { MergeCluster } from './MergeClusterCard';

/**
 * Group pending proposals into clusters — connected components over the pairs.
 *
 * Grouping by NAME would be the obvious rule and the wrong one: a pair may hold
 * two different spellings ("Anna" / "Anna Schmidt", a typo pair), so a name key
 * splits clusters that belong together and joins ones that do not. The pairs
 * themselves already carry the structure the owner is judging — "these entities
 * are claimed to be the same thing" — so the component IS the cluster.
 *
 * Only SAME-TIER pairs build components: a cross-tier pair changes an atom's
 * reach and never takes part in a bulk decision. Such a pair is attached to a
 * component it touches (for the footer count) and otherwise left alone; one
 * that touches no component is returned in `crossTierOnly` to be rendered as an
 * ordinary single pair.
 *
 * A pair whose REASON marks it weak (`name_typo` — "maybe two different people")
 * is excluded too, and for the sharpest reason of all: the reconciler refuses to
 * auto-merge it, so a bulk fold must not merge it either. Measured on the live
 * household graph 2026-09-24: 11 such pairs pending, SIX of them sitting inside
 * a foldable cluster.
 *
 * A pair whose entity TYPES differ is excluded from the components for the same
 * reason and a sharper one: it is usually not a duplicate at all. Embedding
 * similarity cannot separate a town from the company seated in it — both are
 * described out of the same documents — so the reconciler used to propose such
 * pairs, and ONE of them inside a component drags the whole component across
 * the type boundary (measured on the xidra graph 2026-09-24: a place pulled a
 * cluster of company spellings into itself). The backend now refuses to propose
 * them unless the names are related, and refuses to fold them in bulk either;
 * this test is the view's own half, and it also holds for the pairs already in
 * the queue from before that guard. A same-name/different-type pair is a real,
 * decidable case (a mis-typed duplicate) — it is shown as a single pair, not
 * dropped.
 */
export interface GroupedProposals {
  /** Components with MORE than one pair — worth a cluster card. */
  clusters: MergeCluster[];
  /** Everything that stays a plain pair card: lone pairs and cross-tier ones. */
  singles: MergeProposal[];
}

function tierOf(p: MergeProposal): { same: boolean } {
  return { same: p.loser.circle_tier === p.winner.circle_tier };
}

/** Same KIND of thing. The primary type is the whole test on the service side
 *  too (`_types_compatible`), so view and backend agree exactly — the multi-type
 *  superset is deliberately not consulted by either, because it only ever grows. */
function sameType(p: MergeProposal): boolean {
  return p.loser.entity_type === p.winner.entity_type;
}

/**
 * Reasons that mean "maybe two different things" BY DEFINITION. `name_typo` is a
 * review candidate precisely because a machine cannot decide it — two people one
 * character apart. Such a pair must never be an EDGE: it would join two
 * components that were never compared, and the fold button shows a count, not
 * the two names. The service refuses the same pairs (`KG_MERGE_WEAK_REASONS`);
 * this is the view's half, so a weak pair is visible as its own card instead of
 * disappearing into a cluster.
 */
const WEAK_REASONS = new Set(['name_typo']);

function isWeak(p: MergeProposal): boolean {
  return WEAK_REASONS.has(p.reason);
}

export function groupProposals(proposals: MergeProposal[]): GroupedProposals {
  // A cross-type pair is never an edge and never a cluster member — it goes
  // straight to the single-pair cards, ahead of the tier split.
  const weak = proposals.filter(isWeak);
  const strong = proposals.filter((p) => !isWeak(p));
  const crossType = strong.filter((p) => !sameType(p));
  const typed = strong.filter(sameType);
  const sameTier = typed.filter((p) => tierOf(p).same);
  const crossTier = typed.filter((p) => !tierOf(p).same);

  // Union-find over entity ids, edges = same-tier pairs.
  const parent = new Map<number, number>();
  const find = (x: number): number => {
    let r = parent.get(x) ?? x;
    if (r !== x) {
      r = find(r);
      parent.set(x, r);
    }
    return r;
  };
  const union = (a: number, b: number): void => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent.set(ra, rb);
  };
  for (const p of sameTier) {
    parent.set(p.loser.id, parent.get(p.loser.id) ?? p.loser.id);
    parent.set(p.winner.id, parent.get(p.winner.id) ?? p.winner.id);
    union(p.loser.id, p.winner.id);
  }

  const byRoot = new Map<number, { pairs: MergeProposal[]; entities: Map<number, MergeProposalEntityBrief> }>();
  for (const p of sameTier) {
    const root = find(p.loser.id);
    let bucket = byRoot.get(root);
    if (!bucket) {
      bucket = { pairs: [], entities: new Map() };
      byRoot.set(root, bucket);
    }
    bucket.pairs.push(p);
    bucket.entities.set(p.loser.id, p.loser);
    bucket.entities.set(p.winner.id, p.winner);
  }

  // Attach each cross-tier pair to a component it touches; the rest stay single.
  const crossByRoot = new Map<number, MergeProposal[]>();
  const crossOrphans: MergeProposal[] = [];
  for (const p of crossTier) {
    const root = parent.has(p.loser.id) ? find(p.loser.id)
      : parent.has(p.winner.id) ? find(p.winner.id)
        : null;
    if (root === null) {
      crossOrphans.push(p);
      continue;
    }
    crossByRoot.set(root, [...(crossByRoot.get(root) ?? []), p]);
  }

  const clusters: MergeCluster[] = [];
  const singles: MergeProposal[] = [...weak, ...crossType, ...crossOrphans];
  for (const [root, bucket] of byRoot) {
    // A cross-tier pair is ALWAYS rendered as its own card, cluster or not —
    // the cluster only carries it as a count for its footer. Counting it without
    // rendering it would make it unreachable: no approve, no reject, exactly the
    // opposite of "stays individually decidable".
    singles.push(...(crossByRoot.get(root) ?? []));
    if (bucket.pairs.length < 2) {
      // A lone pair is a pair — the familiar card with its undo window.
      singles.push(...bucket.pairs);
      continue;
    }
    const entities = [...bucket.entities.values()];
    const label = entities.reduce(
      (best, e) => ((e.mention_count ?? 0) > (best.mention_count ?? 0) ? e : best),
      entities[0],
    ).name;
    clusters.push({
      key: String(root),
      label,
      entities,
      pairs: bucket.pairs,
      crossTierPairs: crossByRoot.get(root) ?? [],
    });
  }

  // Biggest clusters first — that is where the queue actually shrinks.
  clusters.sort((a, b) => b.pairs.length - a.pairs.length);
  return { clusters, singles };
}
