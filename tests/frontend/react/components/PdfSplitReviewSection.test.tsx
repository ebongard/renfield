/**
 * PdfSplitReviewSection — the /brain/review PDF-split review queue (PR2).
 * The resource module is mocked wholesale: the section renders proposals,
 * range editing keeps contiguity by construction (merge-with-next + add
 * boundary), approve sends edited ranges only when dirty, reject fires the
 * treat-as-single mutation. German default.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { renderWithRouter } from '../test-utils';

const approveMutate = vi.fn();
const rejectMutate = vi.fn();

const proposalsData: unknown[] = [];

vi.mock('../../../../src/frontend/src/api/resources/pdfSplit', () => ({
  usePdfSplitProposalsQuery: () => ({ data: proposalsData }),
  usePdfSplitProposalDetailQuery: () => ({ data: undefined, isLoading: true }),
  useApprovePdfSplitProposal: () => ({
    mutate: approveMutate,
    isPending: false,
    isError: false,
    errorMessage: null,
  }),
  useRejectPdfSplitProposal: () => ({
    mutate: rejectMutate,
    isPending: false,
    isError: false,
    errorMessage: null,
  }),
  fetchProposalPageBlob: vi.fn(),
}));

import PdfSplitReviewSection from '../../../../src/frontend/src/components/PdfSplitReviewSection';

function proposal(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    document_id: 7,
    document_filename: 'stapel_scan.pdf',
    status: 'pending',
    page_count: 5,
    overall_confidence: 0.6,
    created_at: '2026-08-18T10:00:00',
    documents: [
      { start_page: 1, end_page: 2, title: 'Rechnung A', doc_type: 'invoice', confidence: 0.9 },
      { start_page: 3, end_page: 5, title: 'Brief B', doc_type: 'letter', confidence: 0.5 },
    ],
    ...over,
  };
}

describe('PdfSplitReviewSection', () => {
  beforeEach(() => {
    approveMutate.mockClear();
    rejectMutate.mockClear();
    proposalsData.length = 0;
  });

  it('renders nothing when disabled or empty', () => {
    proposalsData.push(proposal());
    const { container: off } = renderWithRouter(<PdfSplitReviewSection enabled={false} />);
    expect(off.firstChild).toBeNull();

    proposalsData.length = 0;
    const { container: empty } = renderWithRouter(<PdfSplitReviewSection enabled={true} />);
    expect(empty.firstChild).toBeNull();
  });

  it('renders the proposal with page ranges and titles (de)', () => {
    proposalsData.push(proposal());
    renderWithRouter(<PdfSplitReviewSection enabled={true} />);
    expect(screen.getByText('stapel_scan.pdf')).toBeInTheDocument();
    expect(screen.getByText('Seiten 1–2')).toBeInTheDocument();
    expect(screen.getByText('Seiten 3–5')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Rechnung A')).toBeInTheDocument();
  });

  it('approve without edits sends NO documents override', () => {
    proposalsData.push(proposal());
    renderWithRouter(<PdfSplitReviewSection enabled={true} />);
    fireEvent.click(screen.getByRole('button', { name: 'Aufteilen' }));
    expect(approveMutate).toHaveBeenCalledWith({ id: 1, documents: undefined });
  });

  it('merge-with-next collapses two pieces and approve sends the edit', () => {
    proposalsData.push(proposal());
    renderWithRouter(<PdfSplitReviewSection enabled={true} />);

    fireEvent.click(
      screen.getByRole('button', { name: 'Mit nächstem Teilstück zusammenführen' }),
    );
    // merged into one piece → approve disabled (needs >= 2)
    expect(screen.getByText('Seiten 1–5')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Mit Änderungen aufteilen' }),
    ).toBeDisabled();
  });

  it('add-boundary splits a piece contiguously and approve sends edited ranges', () => {
    proposalsData.push(proposal());
    renderWithRouter(<PdfSplitReviewSection enabled={true} />);

    fireEvent.change(screen.getByLabelText('Neue Dokumentgrenze ab Seite'), {
      target: { value: '4' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Grenze hinzufügen' }));
    expect(screen.getByText('Seiten 4–5')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Mit Änderungen aufteilen' }));
    const sent = approveMutate.mock.calls[0][0];
    expect(sent.id).toBe(1);
    expect(
      sent.documents.map((d: { start_page: number; end_page: number }) => [
        d.start_page,
        d.end_page,
      ]),
    ).toEqual([
      [1, 2],
      [3, 3],
      [4, 5],
    ]);
  });

  it('reject fires the treat-as-single mutation', () => {
    proposalsData.push(proposal());
    renderWithRouter(<PdfSplitReviewSection enabled={true} />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Als EIN Dokument verarbeiten' }),
    );
    expect(rejectMutate).toHaveBeenCalledWith(1);
  });
});
