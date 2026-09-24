/**
 * The cluster card: one decision for a whole name cluster.
 *
 * The point of the card is that the owner can actually decide — which means the
 * evidence the embedding lacks (edges, mentions, the seen window, a description
 * when there is one) has to be on screen, and the survivor has to be choosable.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { renderWithProviders } from '../test-utils';
import MergeClusterCard, { type MergeCluster } from '../../../../src/frontend/src/components/MergeClusterCard';
import type { MergeProposal, MergeProposalEntityBrief } from '../../../../src/frontend/src/api/resources/knowledgeGraph';

function ent(id: number, name: string, extra: Partial<MergeProposalEntityBrief> = {}): MergeProposalEntityBrief {
  return {
    id, name, entity_type: 'person', circle_tier: 2, mention_count: 1,
    surface_forms: [], relation_count: 0, ...extra,
  };
}

function pair(id: number, loser: MergeProposalEntityBrief, winner: MergeProposalEntityBrief): MergeProposal {
  return { id, similarity: 0.9, reason: 'gray_zone', status: 'pending', created_at: '', loser, winner };
}

function cluster(overrides: Partial<MergeCluster> = {}): MergeCluster {
  const a = ent(1, 'Anna', { mention_count: 1, relation_count: 0 });
  const b = ent(2, 'Anna', { mention_count: 294, relation_count: 12, description: 'Nachbarin' });
  const c = ent(3, 'Anna', { mention_count: 35, relation_count: 3 });
  return {
    key: 'k1', label: 'Anna', entities: [a, b, c],
    pairs: [pair(10, a, b), pair(11, c, b)], crossTierPairs: [],
    ...overrides,
  };
}

describe('MergeClusterCard', () => {
  it('names the KIND of thing once, in the header', () => {
    // Per row it would be pure repetition: grouping is primary-type equality,
    // which is transitive, so every entity in a cluster carries the same type.
    renderWithProviders(
      <MergeClusterCard
        cluster={cluster({
          label: 'Beispiel GmbH',
          entities: [
            ent(1, 'Beispiel GmbH', { entity_type: 'organization' }),
            ent(2, 'Beispiel Gmbh', { entity_type: 'organization', mention_count: 9 }),
          ],
        })}
        onMerge={vi.fn()} onReject={vi.fn()}
      />,
    );
    expect(screen.getAllByText(/organization/)).toHaveLength(1);
    // …and still once after expanding: this is the actual claim.
    fireEvent.click(screen.getByRole('button', { expanded: false }));
    expect(screen.getAllByText(/organization/)).toHaveLength(1);
  });

  it('summarises the cluster without expanding it', () => {
    renderWithProviders(
      <MergeClusterCard cluster={cluster()} onMerge={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getByText('Anna')).toBeInTheDocument();
    expect(screen.getByText(/3 Entitäten · 2 Paare/)).toBeInTheDocument();
  });

  it('shows the evidence once expanded', () => {
    renderWithProviders(
      <MergeClusterCard cluster={cluster()} onMerge={vi.fn()} onReject={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button', { expanded: false }));

    expect(screen.getByText('12 Kanten')).toBeInTheDocument();
    expect(screen.getByText('3 Kanten')).toBeInTheDocument();
    expect(screen.getByText('Nachbarin')).toBeInTheDocument();
  });

  it('defaults the survivor to the most-established entity', () => {
    const onMerge = vi.fn();
    renderWithProviders(
      <MergeClusterCard cluster={cluster()} onMerge={onMerge} onReject={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Alle 2 zusammenführen/ }));
    expect(onMerge).toHaveBeenCalledWith(2);   // 294 Erwähnungen
  });

  it('merges into the survivor the owner picks', () => {
    const onMerge = vi.fn();
    renderWithProviders(
      <MergeClusterCard cluster={cluster()} onMerge={onMerge} onReject={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button', { expanded: false }));
    fireEvent.click(screen.getAllByRole('radio')[2]);   // der dritte Eintrag
    fireEvent.click(screen.getByRole('button', { name: /Alle 2 zusammenführen/ }));
    expect(onMerge).toHaveBeenCalledWith(1);
  });

  it('says how many pairs stay individually decidable', () => {
    const c = cluster({ crossTierPairs: [pair(12, ent(4, 'Anna', { circle_tier: 0 }), ent(2, 'Anna'))] });
    renderWithProviders(<MergeClusterCard cluster={c} onMerge={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/ändert die Sichtbarkeit/)).toBeInTheDocument();
  });

  it('disables both actions while a decision is in flight', () => {
    renderWithProviders(
      <MergeClusterCard cluster={cluster()} busy onMerge={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getByRole('button', { name: /Alle 2 zusammenführen/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Alle verwerfen/ })).toBeDisabled();
  });

  it('asks once more before folding a LARGE cluster, and says why', () => {
    // Connected components over a similarity relation are not equivalence
    // classes: a chain A~B~C joins two things that were never compared.
    const many = Array.from({ length: 7 }, (_, i) => ent(100 + i, 'Anna', { mention_count: i }));
    const onMerge = vi.fn();
    renderWithProviders(
      <MergeClusterCard
        cluster={cluster({ entities: many })}
        onMerge={onMerge}
        onReject={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Alle 2 zusammenführen/ }));
    expect(onMerge).not.toHaveBeenCalled();
    expect(screen.getByText(/Kette von Ähnlichkeiten/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Wirklich 7 Entitäten verschmelzen/ }));
    expect(onMerge).toHaveBeenCalledTimes(1);
  });

  it('folds a small cluster without a second click', () => {
    const onMerge = vi.fn();
    renderWithProviders(
      <MergeClusterCard cluster={cluster()} onMerge={onMerge} onReject={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Alle 2 zusammenführen/ }));
    expect(onMerge).toHaveBeenCalledTimes(1);
  });
});
