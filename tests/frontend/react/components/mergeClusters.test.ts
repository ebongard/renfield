/**
 * Grouping the review queue into clusters.
 *
 * The queue's unit of judgement is the cluster, not the pair: 1 365 pending
 * pairs over 952 entities (live household, 2026-09-24). Grouping by NAME is the
 * tempting rule and the wrong one — a pair may hold two spellings — so the
 * pairs themselves define the components.
 */
import { describe, it, expect } from 'vitest';
import { groupProposals } from '../../../../src/frontend/src/components/mergeClusters';
import type { MergeProposal, MergeProposalEntityBrief } from '../../../../src/frontend/src/api/resources/knowledgeGraph';

function ent(id: number, name: string, tier = 2, mentions = 1): MergeProposalEntityBrief {
  return { id, name, entity_type: 'person', circle_tier: tier, mention_count: mentions, surface_forms: [] };
}

let nextId = 1;
function pair(loser: MergeProposalEntityBrief, winner: MergeProposalEntityBrief, reason = 'gray_zone'): MergeProposal {
  nextId += 1;
  return {
    id: nextId, similarity: 0.9, reason, status: 'pending',
    created_at: '2026-07-01T00:00:00', loser, winner,
  };
}

describe('groupProposals', () => {
  it('joins a chain of pairs into ONE cluster', () => {
    const a = ent(1, 'Anna', 2, 1);
    const b = ent(2, 'Anna', 2, 9);
    const c = ent(3, 'Anna', 2, 3);
    const { clusters, singles } = groupProposals([pair(a, b), pair(c, b)]);

    expect(clusters).toHaveLength(1);
    expect(singles).toHaveLength(0);
    expect(clusters[0].entities.map((e) => e.id).sort()).toEqual([1, 2, 3]);
    expect(clusters[0].pairs).toHaveLength(2);
    // Label = the most-established spelling, the reconciler's own winner rule.
    expect(clusters[0].label).toBe('Anna');
  });

  it('groups by the pairs, NOT by the name', () => {
    // Two spellings in one pair (the typo case) still form one cluster …
    const typoA = ent(1, 'Firstname Lastname', 2, 3000);
    const typoB = ent(2, 'Firstname Lastnrame', 2, 134);
    // … while two identically-named entities with no pair between them do not.
    const lonelyA = ent(3, 'Anna', 2, 5);
    const lonelyB = ent(4, 'Anna', 2, 6);
    const other = ent(5, 'Berta', 2, 2);

    const { clusters } = groupProposals([
      pair(typoA, typoB), pair(typoB, ent(6, 'Firstname Lastname', 2, 10)),
      pair(lonelyA, other),
    ]);

    const chain = clusters.find((c) => c.entities.some((e) => e.id === 1));
    expect(chain?.entities.map((e) => e.id).sort()).toEqual([1, 2, 6]);
    expect(chain?.entities.some((e) => e.id === lonelyB.id)).toBe(false);
  });

  it('leaves a lone pair as a pair', () => {
    const { clusters, singles } = groupProposals([pair(ent(1, 'Anna'), ent(2, 'Anna', 2, 4))]);
    expect(clusters).toHaveLength(0);
    expect(singles).toHaveLength(1);
  });

  it('never lets a cross-tier pair into a cluster, but counts it', () => {
    const a = ent(1, 'Jutta', 2, 1);
    const b = ent(2, 'Jutta', 2, 9);
    const c = ent(3, 'Jutta', 2, 3);
    const privat = ent(4, 'Jutta', 0, 4);   // andere Stufe
    const { clusters } = groupProposals([
      pair(a, b), pair(c, b), pair(privat, b, 'cross_tier'),
    ]);

    expect(clusters).toHaveLength(1);
    expect(clusters[0].pairs).toHaveLength(2);
    expect(clusters[0].crossTierPairs).toHaveLength(1);
    // The cross-tier entity is NOT offered as part of the bulk decision.
    expect(clusters[0].entities.some((e) => e.id === privat.id)).toBe(false);
  });

  it('renders a cross-tier pair that touches no cluster as a plain pair', () => {
    const { clusters, singles } = groupProposals([
      pair(ent(1, 'Anna', 0), ent(2, 'Anna', 2), 'cross_tier'),
    ]);
    expect(clusters).toHaveLength(0);
    expect(singles).toHaveLength(1);
  });

  it('puts the biggest cluster first — that is where the queue shrinks', () => {
    const big = [pair(ent(1, 'A', 2, 1), ent(2, 'A', 2, 9)), pair(ent(3, 'A', 2, 2), ent(2, 'A', 2, 9)),
      pair(ent(4, 'A', 2, 3), ent(2, 'A', 2, 9))];
    const small = [pair(ent(10, 'B', 2, 1), ent(11, 'B', 2, 4)), pair(ent(12, 'B', 2, 2), ent(11, 'B', 2, 4))];
    const { clusters } = groupProposals([...small, ...big]);
    expect(clusters[0].pairs.length).toBeGreaterThan(clusters[1].pairs.length);
  });
});
