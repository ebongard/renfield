/**
 * The review section's cluster path — specifically: a PARTIAL refusal.
 *
 * `resolve_cluster` can legitimately resolve a cluster only in part: pairs that
 * change visibility, cross a type boundary, or do not reach the survivor stay
 * pending on purpose and are reported back in `skipped_*`. The section used to
 * discard that payload, so a partial refusal was indistinguishable from a full
 * success — the cards were optimistically dismissed and the untouched pairs
 * stayed invisible until a page reload.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { server } from '../mocks/server';
import { renderWithProviders } from '../test-utils';
import MergeProposalsSection from '../../../../src/frontend/src/components/MergeProposalsSection';
import { TEST_CONFIG } from '../config';

const BASE = TEST_CONFIG.API_BASE_URL;

function ent(id: number, name: string, mentions: number) {
  return {
    id, name, entity_type: 'person', circle_tier: 2, mention_count: mentions,
    surface_forms: [], relation_count: 0,
  };
}

/** Three same-tier, same-type entities tied by two pairs — one cluster card. */
const A = ent(1, 'Anna', 1);
const B = ent(2, 'Anna', 9);
const C = ent(3, 'Anna', 3);
const PROPOSALS = [
  { id: 10, similarity: 0.9, reason: 'gray_zone', status: 'pending', created_at: '', loser: A, winner: B },
  { id: 11, similarity: 0.9, reason: 'gray_zone', status: 'pending', created_at: '', loser: C, winner: B },
];

function mockQueue(clusterResult: Record<string, unknown>) {
  server.use(
    http.get(`${BASE}/api/knowledge-graph/merge-proposals`, () =>
      HttpResponse.json({ proposals: PROPOSALS, total: PROPOSALS.length })),
    http.post(`${BASE}/api/knowledge-graph/merge-proposals/cluster`, () =>
      HttpResponse.json(clusterResult)),
  );
}

const FULL_SUCCESS = {
  merged: 2, approved: 2, rejected: 0,
  skipped_cross_tier: 0, skipped_cross_type: 0, skipped_unreachable: 0, notes: [],
};

describe('MergeProposalsSection — partial cluster refusal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('says so, and puts the cluster back, when the service left pairs pending', async () => {
    mockQueue({ ...FULL_SUCCESS, merged: 1, approved: 1, skipped_cross_tier: 1 });
    renderWithProviders(<MergeProposalsSection />);

    const merge = await screen.findByRole('button', { name: /zusammenführen/i });
    fireEvent.click(merge);

    // The refusal is named — not swallowed into a silent optimistic dismissal.
    expect(await screen.findByText(/bleibt offen/i)).toBeInTheDocument();
    // …and the card is back, so the still-pending pair is reachable again.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /zusammenführen/i })).toBeInTheDocument();
    });
  });

  it('stays quiet when the whole cluster really was folded', async () => {
    mockQueue(FULL_SUCCESS);
    renderWithProviders(<MergeProposalsSection />);

    const merge = await screen.findByRole('button', { name: /zusammenführen/i });
    fireEvent.click(merge);

    await waitFor(() => {
      expect(screen.queryByText(/bleibt offen/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/bleiben offen/i)).not.toBeInTheDocument();
    });
  });

  it('translates the refusal instead of echoing the backend', async () => {
    // Der Dienst liefert einen CODE plus die Paare; der Satz entsteht hier,
    // sonst steht Englisch in einer deutschen Oberflaeche (CLAUDE.md).
    server.use(
      http.get(`${BASE}/api/knowledge-graph/merge-proposals`, () =>
        HttpResponse.json({ proposals: PROPOSALS, total: PROPOSALS.length })),
      http.post(`${BASE}/api/knowledge-graph/merge-proposals/cluster`, () =>
        HttpResponse.json(
          { detail: { code: 'cluster_has_undecidable_pair', pairs: [['Anna', 'Ana']], total: 1 } },
          { status: 400 },
        )),
    );
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    expect(await screen.findByText(/zwei verschiedene Dinge sein könnte/i)).toBeInTheDocument();
    expect(screen.getByText(/Anna \/ Ana/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /zusammenführen/i })).toBeInTheDocument();
    });
  });

  it('translates EVERY refusal code, not only the undecidable one', async () => {
    // Den einen uebersetzten Fall zu pruefen haette die sechs Geschwister
    // uebersehen — genau der Befund aus dem Review dieses Zweigs.
    server.use(
      http.get(`${BASE}/api/knowledge-graph/merge-proposals`, () =>
        HttpResponse.json({ proposals: PROPOSALS, total: PROPOSALS.length })),
      http.post(`${BASE}/api/knowledge-graph/merge-proposals/cluster`, () =>
        HttpResponse.json(
          { detail: { code: 'cluster_spans_tiers', pairs: [], total: 0,
                      notes: ['cluster spans more than one tier — refusing'] } },
          { status: 400 },
        )),
    );
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    expect(await screen.findByText(/mehrere Sichtbarkeitsstufen/i)).toBeInTheDocument();
    // …und NICHT der rohe englische Vermerk.
    expect(screen.queryByText(/spans more than one tier/i)).not.toBeInTheDocument();
  });

  it('falls back to the generic error for an unknown refusal code', async () => {
    // Ein unbekannter Code darf keine leere Zeile rendern.
    server.use(
      http.get(`${BASE}/api/knowledge-graph/merge-proposals`, () =>
        HttpResponse.json({ proposals: PROPOSALS, total: PROPOSALS.length })),
      http.post(`${BASE}/api/knowledge-graph/merge-proposals/cluster`, () =>
        HttpResponse.json({ detail: { code: 'something_new', pairs: [], total: 0 } },
                          { status: 400 })),
    );
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/\S/);
    });
  });

  it('says how many blocking pairs it is NOT naming', async () => {
    server.use(
      http.get(`${BASE}/api/knowledge-graph/merge-proposals`, () =>
        HttpResponse.json({ proposals: PROPOSALS, total: PROPOSALS.length })),
      http.post(`${BASE}/api/knowledge-graph/merge-proposals/cluster`, () =>
        HttpResponse.json(
          { detail: { code: 'cluster_has_undecidable_pair',
                      pairs: [['A', 'B'], ['C', 'D'], ['E', 'F']], total: 7 } },
          { status: 400 },
        )),
    );
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    // 7 gesamt, 3 genannt -> "und 4 weitere", nie stilles Abschneiden.
    expect(await screen.findByText(/und 4 weitere/i)).toBeInTheDocument();
  });

  it('surfaces a refusal note even when no pair was counted', async () => {
    mockQueue({ ...FULL_SUCCESS, merged: 0, approved: 0, notes: ['cluster spans more than one tier — refusing'] });
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    expect(await screen.findByText(/spans more than one tier/i)).toBeInTheDocument();
  });
});
