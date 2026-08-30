/**
 * DocumentDuplicateCard — the /brain/review KB near-duplicate pair card (#1170 P2).
 * Presentational: survivor radio + per-pair supersede/delete choice + delete
 * warning. German default.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import DocumentDuplicateCard from '../../../../src/frontend/src/components/DocumentDuplicateCard';
import { renderWithRouter } from '../test-utils';
import type {
  DocumentDuplicateProposal,
  DuplicateDocBrief,
} from '../../../../src/frontend/src/api/resources/documentDuplicates';

function doc(o: Partial<DuplicateDocBrief> = {}): DuplicateDocBrief {
  return { id: 0, name: '', paperless_document_id: null, created_at: null, ...o };
}

function proposal(o: Partial<DocumentDuplicateProposal> = {}): DocumentDuplicateProposal {
  return {
    id: 1,
    signal: 'shared_identifier',
    shared_key: 'invoice_number=1SOGUR2D-0011',
    similarity: 1,
    suggested_survivor_id: 44,
    document_a: doc({ id: 44, name: 'Rechnung A', paperless_document_id: 50 }),
    document_b: doc({ id: 45, name: 'Rechnung B' }),
    ...o,
  };
}

describe('DocumentDuplicateCard', () => {
  it('renders both documents + the shared identifier + Paperless badge', () => {
    renderWithRouter(<DocumentDuplicateCard proposal={proposal()} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText('Rechnung A')).toBeInTheDocument();
    expect(screen.getByText('Rechnung B')).toBeInTheDocument();
    expect(screen.getByText(/1SOGUR2D-0011/)).toBeInTheDocument();
    expect(screen.getByText(/In Paperless #50/)).toBeInTheDocument();
  });

  it('approves with the suggested survivor + supersede by default', () => {
    const onApprove = vi.fn();
    renderWithRouter(<DocumentDuplicateCard proposal={proposal()} onApprove={onApprove} onReject={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Bestätigen' }));
    expect(onApprove).toHaveBeenCalledWith(44, 'supersede');
  });

  it('survivor toggle: keeping the other doc approves with its id', () => {
    const onApprove = vi.fn();
    renderWithRouter(<DocumentDuplicateCard proposal={proposal()} onApprove={onApprove} onReject={vi.fn()} />);
    fireEvent.click(screen.getByLabelText('Rechnung B behalten'));
    fireEvent.click(screen.getByRole('button', { name: 'Bestätigen' }));
    expect(onApprove).toHaveBeenCalledWith(45, 'supersede');
  });

  it('choosing delete shows the warning and approves with delete', () => {
    const onApprove = vi.fn();
    renderWithRouter(<DocumentDuplicateCard proposal={proposal()} onApprove={onApprove} onReject={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(/Löschen/));
    expect(screen.getByText(/endgültig gelöscht/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Bestätigen' }));
    expect(onApprove).toHaveBeenCalledWith(44, 'delete');
  });

  it('reject fires onReject', () => {
    const onReject = vi.fn();
    renderWithRouter(<DocumentDuplicateCard proposal={proposal()} onApprove={vi.fn()} onReject={onReject} />);
    fireEvent.click(screen.getByRole('button', { name: 'Kein Duplikat' }));
    expect(onReject).toHaveBeenCalledTimes(1);
  });
});
