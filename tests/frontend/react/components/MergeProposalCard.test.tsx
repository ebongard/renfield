/**
 * MergeProposalCard — the /brain/review merge-proposal card (T10, D2/D4/D6).
 * Presentational: comparison + survivor toggle + cross-tier visibility warning
 * + stakes-adjusted button emphasis. German default.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import MergeProposalCard from '../../../../src/frontend/src/components/MergeProposalCard';
import { renderWithRouter } from '../test-utils';
import type {
  MergeProposal,
  MergeProposalEntityBrief,
} from '../../../../src/frontend/src/api/resources/knowledgeGraph';

function brief(o: Partial<MergeProposalEntityBrief> = {}): MergeProposalEntityBrief {
  return {
    id: 0, name: '', entity_type: 'person', circle_tier: 0, mention_count: 1,
    surface_forms: [], ...o,
  };
}

function proposal(o: Partial<MergeProposal> = {}): MergeProposal {
  return {
    id: 1, similarity: 0.9, reason: 'cross_tier', status: 'pending', created_at: '',
    loser: brief({ id: 10, name: 'Alice', circle_tier: 0, mention_count: 2 }),
    winner: brief({ id: 20, name: 'Alice Brown', circle_tier: 2, mention_count: 9, surface_forms: ['A.B.'] }),
    ...o,
  };
}

describe('MergeProposalCard', () => {
  it('renders both entities + surface-form pill (de)', () => {
    renderWithRouter(<MergeProposalCard proposal={proposal()} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Alice Brown')).toBeInTheDocument();
    expect(screen.getByText('A.B.')).toBeInTheDocument(); // surface form chip
  });

  it('cross_tier proposal shows the visibility-change warning', () => {
    renderWithRouter(<MergeProposalCard proposal={proposal({ reason: 'cross_tier' })} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/Sichtbarkeit ändert sich/)).toBeInTheDocument();
  });

  it('same-tier gray_zone hides the warning and uses a primary merge button', () => {
    const p = proposal({
      reason: 'gray_zone',
      loser: brief({ id: 10, name: 'Alice', circle_tier: 0 }),
      winner: brief({ id: 20, name: 'Alice Brown', circle_tier: 0, mention_count: 9 }),
    });
    renderWithRouter(<MergeProposalCard proposal={p} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.queryByText(/Sichtbarkeit/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zusammenführen' }).className).toContain('btn-primary');
  });

  it('name_typo: renders the spelling-variant label, no visibility warning, cautious button', () => {
    // A same-tier pair one in-token edit apart is "maybe two people" by
    // definition — it gets the label and the no-nudge button, but not the
    // visibility warning (that one is keyed on the tiers).
    const p = proposal({
      reason: 'name_typo',
      loser: brief({ id: 10, name: 'Anna Schmitt', circle_tier: 0, mention_count: 4 }),
      winner: brief({ id: 20, name: 'Anna Schmidt', circle_tier: 0, mention_count: 5 }),
    });
    renderWithRouter(<MergeProposalCard proposal={p} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText('Schreibvariante eines Personennamens')).toBeInTheDocument();
    expect(screen.queryByText(/Sichtbarkeit/)).not.toBeInTheDocument();
    const merge = screen.getByRole('button', { name: 'Zusammenführen' });
    expect(merge.className).toContain('btn-secondary');
    expect(merge.className).not.toContain('btn-primary');
  });

  it('cross_type: renders the kind-mismatch label, no visibility warning, cautious button', () => {
    // A same-tier pair of two different KINDS of thing — the mis-typed-duplicate
    // shape the reconciler now routes to review instead of folding. Same
    // treatment as name_typo: label + no-nudge button, no visibility warning
    // (that one is keyed on the tiers).
    const p = proposal({
      reason: 'cross_type',
      loser: brief({ id: 10, name: 'Pontresina', entity_type: 'person', circle_tier: 0, mention_count: 1 }),
      winner: brief({ id: 20, name: 'Pontresina', entity_type: 'place', circle_tier: 0, mention_count: 9 }),
    });
    renderWithRouter(<MergeProposalCard proposal={p} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText('verschiedene Arten von Dingen')).toBeInTheDocument();
    expect(screen.queryByText(/Sichtbarkeit/)).not.toBeInTheDocument();
    const merge = screen.getByRole('button', { name: 'Zusammenführen' });
    expect(merge.className).toContain('btn-secondary');
    expect(merge.className).not.toContain('btn-primary');
  });

  it('a differing primary type is cautious even on an older gray_zone pair', () => {
    // The nine pairs already in the queue when the guard landed carry reason
    // gray_zone — the types themselves have to drive the caution, not the label.
    const p = proposal({
      reason: 'gray_zone',
      loser: brief({ id: 10, name: 'Korschenbroich', entity_type: 'place', circle_tier: 0, mention_count: 4 }),
      winner: brief({ id: 20, name: 'Beispiel GmbH', entity_type: 'organization', circle_tier: 0, mention_count: 9 }),
    });
    renderWithRouter(<MergeProposalCard proposal={p} onApprove={vi.fn()} onReject={vi.fn()} />);
    const merge = screen.getByRole('button', { name: 'Zusammenführen' });
    expect(merge.className).toContain('btn-secondary');
    expect(merge.className).not.toContain('btn-primary');
    // … and it is CALLED what it is, not "similar but uncertain".
    expect(screen.getByText('verschiedene Arten von Dingen')).toBeInTheDocument();
  });

  it('cross_tier keeps its visibility label even when the types differ too', () => {
    // Precedence matches the reconciler's: the visibility change is the
    // invariant-bearing fact, so it owns the label.
    const p = proposal({
      reason: 'cross_tier',
      loser: brief({ id: 10, name: 'Korschenbroich', entity_type: 'place', circle_tier: 0 }),
      winner: brief({ id: 20, name: 'Beispiel GmbH', entity_type: 'organization', circle_tier: 2, mention_count: 9 }),
    });
    renderWithRouter(<MergeProposalCard proposal={p} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText('unterschiedliche Sichtbarkeit')).toBeInTheDocument();
    expect(screen.getByText(/Sichtbarkeit ändert sich/)).toBeInTheDocument();
  });

  it('cross_tier merge button is de-emphasised (secondary, no primary nudge)', () => {
    renderWithRouter(<MergeProposalCard proposal={proposal({ reason: 'cross_tier' })} onApprove={vi.fn()} onReject={vi.fn()} />);
    const merge = screen.getByRole('button', { name: 'Zusammenführen' });
    expect(merge.className).toContain('btn-secondary');
    expect(merge.className).not.toContain('btn-primary');
  });

  it('approves with the default winner', () => {
    const onApprove = vi.fn();
    renderWithRouter(<MergeProposalCard proposal={proposal()} onApprove={onApprove} onReject={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Zusammenführen' }));
    expect(onApprove).toHaveBeenCalledWith(20); // stored winner
  });

  it('survivor toggle: keeping the other entity approves with its id', () => {
    const onApprove = vi.fn();
    renderWithRouter(<MergeProposalCard proposal={proposal()} onApprove={onApprove} onReject={vi.fn()} />);
    fireEvent.click(screen.getByLabelText('Alice behalten')); // pick the loser as survivor
    fireEvent.click(screen.getByRole('button', { name: 'Zusammenführen' }));
    expect(onApprove).toHaveBeenCalledWith(10);
  });

  it('reject fires onReject', () => {
    const onReject = vi.fn();
    renderWithRouter(<MergeProposalCard proposal={proposal()} onApprove={vi.fn()} onReject={onReject} />);
    fireEvent.click(screen.getByRole('button', { name: 'Ablehnen' }));
    expect(onReject).toHaveBeenCalledTimes(1);
  });
});
