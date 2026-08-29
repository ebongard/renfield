/**
 * SimbaSendModal — the doc-page "send to Simba" overlay (xidra).
 * Verifies the natural inline flow + the P0 safety fixes:
 * - opening creates/reuses the proposal and PREFILLS the form (category/type/Bezeichnung),
 * - the irreversible upload is gated by a styled TWO-STEP confirm (not window.confirm),
 * - a successful upload shows an explicit POSITIVE confirmation (no silent vanish),
 * - the confirm POST carries force:false on the normal path.
 *
 * apiClient is mocked directly (mirrors AuditReviewRow.test.tsx).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import SimbaSendModal from '../../../../src/frontend/src/components/simba/SimbaSendModal';
import { renderWithProviders } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
  },
  extractApiError: (_e: unknown, fallback: string) => fallback,
  extractFieldErrors: () => ({}),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);

beforeEach(() => {
  vi.clearAllMocks();
  mockedGet.mockResolvedValue({
    data: { categories: { Belege: ['Eingangsrechnung', 'Ausgangsrechnung'] } },
  } as never);
});

function renderModal() {
  return renderWithProviders(
    <SimbaSendModal documentId={5} filename="Rechnung Arkadon.pdf" enabled onClose={vi.fn()} />,
  );
}

describe('SimbaSendModal', () => {
  it('creates the proposal, prefills, two-step-confirms, and shows a success acknowledgement', async () => {
    const user = userEvent.setup();
    mockedPost.mockImplementation(async (url: string) => {
      if (url === '/api/simba-ingest/from-document/5') {
        return {
          data: {
            success: true,
            message: 'created',
            proposal_id: 42,
            suggested_category: 'Belege',
            suggested_type: 'Eingangsrechnung',
            suggested_description: 'Rechnung Arkadon',
            suggested_month: 3,
            suggested_year: 2026,
          },
        } as never;
      }
      if (url === '/api/simba-ingest/42/confirm') {
        return { data: { success: true, message: 'Übertragen: 1' } } as never;
      }
      throw new Error(`unexpected POST ${url}`);
    });

    renderModal();

    // Proposal is created on open from the document.
    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith('/api/simba-ingest/from-document/5'),
    );

    // Form prefills the Bezeichnung from the suggestion.
    const label = (await screen.findByDisplayValue('Rechnung Arkadon')) as HTMLInputElement;
    expect(label).toBeInTheDocument();

    // First click = arm the styled confirm (NOT an immediate upload).
    await user.click(screen.getByRole('button', { name: /Übertragen/i }));
    expect(mockedPost).not.toHaveBeenCalledWith(
      '/api/simba-ingest/42/confirm',
      expect.anything(),
    );

    // Second click on the explicit yes = perform the irreversible upload.
    await user.click(screen.getByRole('button', { name: /Ja, übertragen/i }));

    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith(
        '/api/simba-ingest/42/confirm',
        expect.objectContaining({
          category: 'Belege',
          type: 'Eingangsrechnung',
          force: false,
          month: 3, // prefilled from the document date (#1167), not the current month
          year: 2026,
        }),
      ),
    );

    // Positive confirmation is shown (the P0 gap): the filename success message.
    expect(
      await screen.findByText(/wurde an die Steuerkanzlei übertragen/i),
    ).toBeInTheDocument();
  });

  it('does not upload when the create step fails', async () => {
    mockedPost.mockResolvedValue({
      data: { success: false, message: 'not_found', proposal_id: null },
    } as never);

    renderModal();

    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith('/api/simba-ingest/from-document/5'),
    );
    // No confirm/upload call is ever made.
    expect(mockedPost).not.toHaveBeenCalledWith(
      expect.stringContaining('/confirm'),
      expect.anything(),
    );
  });
});
