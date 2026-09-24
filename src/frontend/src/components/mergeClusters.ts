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

export function groupProposals(proposals: MergeProposal[]): GroupedProposals {
  const sameTier = proposals.filter((p) => tierOf(p).same);
  const crossTier = proposals.filter((p) => !tierOf(p).same);

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
  const singles: MergeProposal[] = [...crossOrphans];
  for (const [root, bucket] of byRoot) {
    if (bucket.pairs.length < 2) {
      // A lone pair is a pair — the familiar card with its undo window.
      singles.push(...bucket.pairs);
      singles.push(...(crossByRoot.get(root) ?? []));
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
