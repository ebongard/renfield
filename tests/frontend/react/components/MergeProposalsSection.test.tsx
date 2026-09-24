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

  // Jeder Code, den `resolve_cluster` setzen kann, mit dem Satz, den der
  // Eigentuemer sehen MUSS. Als Tabelle, weil die vorige Fassung genau EINEN
  // Code prueffte — dieselbe Klasse-statt-Instanz-Luecke, gegen die dieser
  // Zweig gebaut ist, eine Ebene hoeher. Ein Tippfehler in einem der sieben
  // Schluessel ginge sonst mit gruener Suite als "Fehler" raus.
  const REFUSALS: [string, RegExp][] = [
    ['cluster_spans_tiers', /mehrere Sichtbarkeitsstufen/i],
    ['cluster_too_small', /mindestens zwei Entit/i],
    ['no_foldable_pair', /kein Paar faltbar/i],
    ['survivor_required', /die bleiben soll/i],
    ['survivor_not_foldable', /kein faltbares Paar/i],
    ['unknown_decision', /Unbekannte Entscheidung/i],
    ['nothing_folded', /bereits aufgel/i],
  ];

  it.each(REFUSALS)('translates the refusal code %s', async (code, expected) => {
    server.use(
      http.get(`${BASE}/api/knowledge-graph/merge-proposals`, () =>
        HttpResponse.json({ proposals: PROPOSALS, total: PROPOSALS.length })),
      http.post(`${BASE}/api/knowledge-graph/merge-proposals/cluster`, () =>
        HttpResponse.json(
          { detail: { code, pairs: [], total: 0, notes: ['an English sentence'] } },
          { status: 400 },
        )),
    );
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    expect(await screen.findByText(expected)).toBeInTheDocument();
    // …und NIE der rohe englische Vermerk aus der Antwort.
    expect(screen.queryByText(/an English sentence/i)).not.toBeInTheDocument();
  });

  it('falls back to a translated sentence for an unknown refusal code', async () => {
    // Die vorige Zusicherung war `toHaveTextContent(/\S/)` — die haelt auch das
    // blosse Warndreieck, den rohen Schluessel oder "[object Object]" fuer
    // bestanden. Jetzt steht da der Satz, den der Eigentuemer sehen soll, und
    // ausdruecklich NICHT der Schluessel und nicht das englische Original.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    server.use(
      http.get(`${BASE}/api/knowledge-graph/merge-proposals`, () =>
        HttpResponse.json({ proposals: PROPOSALS, total: PROPOSALS.length })),
      http.post(`${BASE}/api/knowledge-graph/merge-proposals/cluster`, () =>
        HttpResponse.json(
          { detail: { code: 'something_new', pairs: [], total: 0,
                      notes: ['a brand new English reason'] } },
          { status: 400 },
        )),
    );
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    expect(await screen.findByText(/wurde abgelehnt/i)).toBeInTheDocument();
    expect(screen.queryByText(/refused_|circles\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/a brand new English reason/i)).not.toBeInTheDocument();
    // Der genaue Grund darf nicht verloren gehen — er gehoert in die Konsole,
    // damit er ueberhaupt auswertbar bleibt.
    expect(warn).toHaveBeenCalledWith(
      '[merge-cluster] untranslated refusal', 'something_new', ['a brand new English reason'],
    );
    warn.mockRestore();
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

  it('never echoes a backend note, even on a 200', async () => {
    // Fruehere Fassung dieses Tests: eine 200 TRUG den Vermerk, und die
    // Oberflaeche gab ihn woertlich aus. Seit den Refus-Codes liefert jeder
    // notes-Pfad in `resolve_cluster` eine 400 — diese Form kann der Dienst gar
    // nicht mehr senden. Die Absicht bleibt (ein Refus ist nie stumm), der Weg
    // ist ein anderer: uebersetzt statt englisch. Der Test bewacht die FALLE —
    // wer als Naechstes eine Notiz auf dem Erfolgspfad anhaengt, liefert sonst
    // wieder Englisch in eine deutsche Oberflaeche.
    mockQueue({ ...FULL_SUCCESS, merged: 0, approved: 0, notes: ['cluster spans more than one tier — refusing'] });
    renderWithProviders(<MergeProposalsSection />);

    fireEvent.click(await screen.findByRole('button', { name: /zusammenführen/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/\S/);
    });
    expect(screen.queryByText(/spans more than one tier/i)).not.toBeInTheDocument();
  });
});
