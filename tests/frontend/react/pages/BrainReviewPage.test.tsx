/**
 * BrainReviewPage — the tier change sends a policy the backend accepts.
 *
 * Documents and their facts carry a tier-only policy: PATCH /api/atoms/{id}/tier
 * answers 422 for any other key (#1463). The page used to spread the stored
 * policy into the request, so one stray key turned every tier change on such an
 * atom into an error. Other atom types keep their policy dimensions.
 */
import { describe, it, expect } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../test-utils';
import { server } from '../mocks/server';
import { TEST_CONFIG } from '../config';
import BrainReviewPage from '../../../../src/frontend/src/pages/BrainReviewPage';

const BASE_URL = TEST_CONFIG.API_BASE_URL;

function mockReview(atoms: unknown[]): Record<string, unknown> {
  const patched: Record<string, unknown> = {};
  server.use(
    http.get(`${BASE_URL}/api/circles/me/atoms-for-review`, () => HttpResponse.json(atoms)),
    http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json({})),
    http.patch(`${BASE_URL}/api/atoms/:atomId/tier`, async ({ params, request }) => {
      patched[String(params.atomId)] = ((await request.json()) as { policy: unknown }).policy;
      return HttpResponse.json({});
    }),
  );
  return patched;
}

const extra = { tier: 0, legacy_dimension: 'x' };

describe('BrainReviewPage tier change', () => {
  it('sends only the tier for documents and facts, and keeps other keys for a note', async () => {
    const patched = mockReview([
      { atom_id: 'doc-1', atom_type: 'kb_document', tier: 0, policy: extra, title: 'Dok' },
      { atom_id: 'fact-1', atom_type: 'document_fact', tier: 0, policy: extra, title: 'Fakt' },
      { atom_id: 'note-1', atom_type: 'note', tier: 0, policy: extra, title: 'Notiz' },
    ]);
    renderWithRouter(<BrainReviewPage />);

    const pickers = await screen.findAllByRole('combobox');
    expect(pickers).toHaveLength(3);
    pickers.forEach((picker) => fireEvent.change(picker, { target: { value: '2' } }));

    await waitFor(() => expect(Object.keys(patched)).toHaveLength(3));
    expect(patched['doc-1']).toEqual({ tier: 2 });
    expect(patched['fact-1']).toEqual({ tier: 2 });
    expect(patched['note-1']).toEqual({ tier: 2, legacy_dimension: 'x' });
  });
});
