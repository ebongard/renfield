/**
 * AuditReviewRow — the editable Paperless-audit review row.
 * Verifies: suggested values render as editable inputs; a manual edit persists
 * via PATCH with the override; a per-field checkbox toggle persists the
 * field_selection; adding a tag persists the tags override.
 *
 * apiClient is mocked directly (mirrors PaperlessAuditLowQuality.test.tsx).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';

import AuditReviewRow from '../../../../src/frontend/src/components/paperless/AuditReviewRow';
import { renderWithProviders } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';
import type { AuditResult } from '../../../../src/frontend/src/api/resources/paperlessAudit';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
  },
  extractApiError: (_e: unknown, fallback: string) => fallback,
  extractFieldErrors: () => ({}),
}));

const mockedPatch = vi.mocked(apiClient.patch);

function row(overrides: Partial<AuditResult> = {}): AuditResult {
  return {
    id: 7,
    paperless_doc_id: 100,
    current_title: 'Old Title',
    suggested_title: 'New Title',
    current_correspondent: 'Old Corp',
    suggested_correspondent: 'New Corp',
    current_document_type: 'Invoice',
    suggested_document_type: 'Invoice',
    current_date: '2024-01-01',
    suggested_date: '2024-02-02',
    current_storage_path: null,
    suggested_storage_path: null,
    current_tags: [],
    suggested_tags: [],
    confidence: 0.9,
    ...overrides,
  };
}

function renderRow(r: AuditResult) {
  return renderWithProviders(
    <table>
      <tbody>
        <AuditReviewRow
          result={r}
          isBulkSelected={false}
          onToggleBulkSelected={vi.fn()}
          onApprove={vi.fn()}
          onSkip={vi.fn()}
          actionLoading={false}
          colSpan={11}
        />
      </tbody>
    </table>,
  );
}

describe('AuditReviewRow', () => {
  beforeEach(() => {
    mockedPatch.mockClear();
    mockedPatch.mockResolvedValue({ data: {} });
  });

  it('renders suggested values as editable inputs', () => {
    renderRow(row());
    expect(screen.getByDisplayValue('New Title')).toBeInTheDocument();
    expect(screen.getByDisplayValue('New Corp')).toBeInTheDocument();
    expect(screen.getByDisplayValue('2024-02-02')).toBeInTheDocument();
  });

  it('persists a manual edit as an override on blur', async () => {
    renderRow(row());
    const titleInput = screen.getByDisplayValue('New Title');
    fireEvent.change(titleInput, { target: { value: 'Hand Edited' } });
    fireEvent.blur(titleInput);

    await waitFor(() => {
      expect(mockedPatch).toHaveBeenCalledWith(
        '/api/admin/paperless-audit/results/7',
        expect.objectContaining({
          overrides: expect.objectContaining({ title: 'Hand Edited' }),
        }),
      );
    });
  });

  it('reverting an edit back to the suggestion drops the override', async () => {
    renderRow(row());
    const titleInput = screen.getByDisplayValue('New Title');
    fireEvent.change(titleInput, { target: { value: 'New Title' } });
    fireEvent.blur(titleInput);

    await waitFor(() => expect(mockedPatch).toHaveBeenCalled());
    const lastBody = mockedPatch.mock.calls[mockedPatch.mock.calls.length - 1][1] as {
      overrides: Record<string, unknown>;
    };
    expect(lastBody.overrides).not.toHaveProperty('title');
  });

  it('toggling a field checkbox persists field_selection', async () => {
    renderRow(row());
    // The apply-field checkboxes carry the "Apply this field" aria-label; all
    // changed fields (title, correspondent, date here) start selected.
    const applyBoxes = screen.getAllByLabelText('Dieses Feld übernehmen');
    const initial = applyBoxes.length;
    expect(initial).toBeGreaterThan(0);
    fireEvent.click(applyBoxes[0]); // deselect the first changed field

    await waitFor(() => expect(mockedPatch).toHaveBeenCalled());
    const body = mockedPatch.mock.calls[mockedPatch.mock.calls.length - 1][1] as {
      field_selection: string[];
    };
    expect(body.field_selection.length).toBe(initial - 1);
  });

  it('adding a tag persists a tags override', async () => {
    renderRow(row());
    const tagInput = screen.getByPlaceholderText('Tag hinzufügen');
    fireEvent.change(tagInput, { target: { value: 'steuer' } });
    fireEvent.keyDown(tagInput, { key: 'Enter' });

    await waitFor(() => {
      expect(mockedPatch).toHaveBeenCalledWith(
        '/api/admin/paperless-audit/results/7',
        expect.objectContaining({
          overrides: expect.objectContaining({ tags: ['steuer'] }),
        }),
      );
    });
  });
});
