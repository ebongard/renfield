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
import type { AuditResult, TaxonomyResponse } from '../../../../src/frontend/src/api/resources/paperlessAudit';

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

function renderRow(r: AuditResult, taxonomy?: TaxonomyResponse) {
  return renderWithProviders(
    <table>
      <tbody>
        <AuditReviewRow
          result={r}
          isBulkSelected={false}
          onToggleBulkSelected={vi.fn()}
          onApprove={vi.fn()}
          onSkip={vi.fn()}
          onRegisterPending={vi.fn()}
          taxonomy={taxonomy}
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

  it('renders suggested values as read-only text (Jira click-to-edit)', () => {
    renderRow(row());
    // values shown as text, NOT permanent inputs, until clicked
    expect(screen.getByText('New Title')).toBeInTheDocument();
    expect(screen.getByText('New Corp')).toBeInTheDocument();
    expect(screen.getByText('2024-02-02')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('New Title')).not.toBeInTheDocument();
  });

  it('persists a manual edit as an override on blur', async () => {
    renderRow(row());
    fireEvent.click(screen.getByText('New Title')); // click-to-edit
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

  it('editing an existing override back to the suggestion drops it', async () => {
    renderRow(row({ user_overrides: { title: 'Hand Edited' } }));
    fireEvent.click(screen.getByText('Hand Edited')); // the persisted override value
    const titleInput = screen.getByDisplayValue('Hand Edited');
    fireEvent.change(titleInput, { target: { value: 'New Title' } }); // back to suggestion
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

  it('editing a scalar back to the current value does not freeze the row', async () => {
    // only title changes (correspondent/date == current)
    renderRow(row({ suggested_correspondent: 'Old Corp', suggested_date: '2024-01-01' }));
    fireEvent.click(screen.getByText('New Title'));
    const titleInput = screen.getByDisplayValue('New Title');
    fireEvent.change(titleInput, { target: { value: 'Old Title' } }); // == current
    fireEvent.blur(titleInput);

    await waitFor(() => expect(mockedPatch).toHaveBeenCalled());
    const body = mockedPatch.mock.calls[mockedPatch.mock.calls.length - 1][1] as {
      field_selection: string[];
    };
    // a no-op edit deselects the field (not applied); the row is not stuck.
    expect(body.field_selection).not.toContain('title');
    const approveBtn = screen.getByTitle('Übernehmen');
    expect(approveBtn).not.toBeDisabled();
  });

  it('preserves the JSON type of an edited custom field (no string coercion)', async () => {
    renderRow(row({ suggested_custom_fields: { amount: 5 } }));
    // expand the custom-fields drawer, then click-to-edit the value
    fireEvent.click(screen.getByTitle('Benutzerdefinierte Felder'));
    fireEvent.click(screen.getByText('5'));
    const valueInput = screen.getByDisplayValue('5');
    fireEvent.change(valueInput, { target: { value: '10' } });
    fireEvent.blur(valueInput);

    await waitFor(() => {
      const bodies = mockedPatch.mock.calls.map((c) => c[1] as { overrides: { custom_fields?: Record<string, unknown> } });
      const withCf = bodies.find((b) => b.overrides.custom_fields);
      expect(withCf?.overrides.custom_fields).toEqual({ amount: 10 }); // number, not "10"
    });
  });

  it('offers Paperless correspondents as a datalist lookup (pick or create)', () => {
    renderRow(row(), { correspondents: ['Stadtwerke', 'Finanzamt'], document_types: [], tags: [], storage_paths: [] });
    fireEvent.click(screen.getByText('New Corp')); // click-to-edit the correspondent
    const input = screen.getByDisplayValue('New Corp');
    const listId = input.getAttribute('list');
    expect(listId).toBeTruthy();
    const dl = document.getElementById(listId as string);
    const values = [...(dl?.querySelectorAll('option') ?? [])].map((o) => o.getAttribute('value'));
    expect(values).toEqual(['Stadtwerke', 'Finanzamt']); // free text still allowed = create
  });

  it('date field opens a native date input (calendar widget)', () => {
    renderRow(row());
    fireEvent.click(screen.getByText('2024-02-02'));
    const input = screen.getByDisplayValue('2024-02-02');
    expect(input).toHaveAttribute('type', 'date');
  });

  it('adding a tag persists a tags override', async () => {
    renderRow(row());
    fireEvent.click(screen.getByText('Tag hinzufügen')); // reveal the add input
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
