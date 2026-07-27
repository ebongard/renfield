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
import userEvent from '@testing-library/user-event';

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

  const _taxo = (over: Partial<import('../../../../src/frontend/src/api/resources/paperlessAudit').TaxonomyResponse> = {}) => ({
    correspondents: [], document_types: [], tags: [], storage_paths: [],
    allow_create: { correspondent: true, document_type: true, tags: true, storage_path: false },
    ...over,
  });

  it('correspondent is a creatable combobox (filter existing + create row)', async () => {
    const user = userEvent.setup();
    renderRow(row(), _taxo({ correspondents: ['Stadtwerke', 'Finanzamt'] }));
    await user.click(screen.getByText('New Corp')); // open the combobox
    const input = screen.getByRole('combobox', { name: 'Ansprechpartner bearbeiten' });
    await user.click(input);
    await user.type(input, 'stadt');
    expect(await screen.findByRole('option', { name: /Stadtwerke/ })).toBeInTheDocument(); // existing match
    expect(await screen.findByRole('option', { name: /anlegen/i })).toBeInTheDocument();   // create row
  });

  it('picking a combobox option persists it as an override', async () => {
    const user = userEvent.setup();
    renderRow(row(), _taxo({ correspondents: ['Stadtwerke', 'Finanzamt'] }));
    await user.click(screen.getByText('New Corp'));
    const input = screen.getByRole('combobox', { name: 'Ansprechpartner bearbeiten' });
    await user.click(input);
    await user.type(input, 'stadt');
    await user.click(await screen.findByRole('option', { name: /Stadtwerke/ }));
    await waitFor(() => {
      expect(mockedPatch).toHaveBeenCalledWith(
        '/api/admin/paperless-audit/results/7',
        expect.objectContaining({ overrides: expect.objectContaining({ correspondent: 'Stadtwerke' }) }),
      );
    });
  });

  it('the create row persists the typed new value', async () => {
    const user = userEvent.setup();
    renderRow(row(), _taxo({ correspondents: ['Stadtwerke', 'Finanzamt'] }));
    await user.click(screen.getByText('New Corp'));
    const input = screen.getByRole('combobox', { name: 'Ansprechpartner bearbeiten' });
    await user.click(input);
    await user.type(input, 'Stadt'); // exists as 'Stadtwerke' but user creates 'Stadt'
    await user.click(await screen.findByRole('option', { name: /anlegen/i }));
    await waitFor(() => {
      expect(mockedPatch).toHaveBeenCalledWith(
        '/api/admin/paperless-audit/results/7',
        expect.objectContaining({ overrides: expect.objectContaining({ correspondent: 'Stadt' }) }),
      );
    });
  });

  it('calendar reset clears the date override', async () => {
    const user = userEvent.setup();
    renderRow(row({ user_overrides: { date: '2024-09-09' } }));
    await user.click(screen.getByText('2024-09-09'));
    await user.click(await screen.findByRole('button', { name: 'Zurücksetzen' }));
    await waitFor(() => expect(mockedPatch).toHaveBeenCalled());
    const body = mockedPatch.mock.calls[mockedPatch.mock.calls.length - 1][1] as { overrides: Record<string, unknown> };
    expect(body.overrides).not.toHaveProperty('date');
  });

  it('existing-only field (storage_path) offers no create row', async () => {
    const user = userEvent.setup();
    renderRow(row({ suggested_storage_path: 'Finanzen/2024' }),
      _taxo({ storage_paths: ['Finanzen/2024', 'Fahrzeug/Belege'] }));
    await user.click(screen.getByText('Finanzen/2024'));
    const input = screen.getByRole('combobox', { name: 'Ablageort bearbeiten' });
    await user.click(input);
    await user.type(input, 'Neuer Pfad'); // not an existing path
    await waitFor(() => expect(screen.queryByRole('option', { name: /anlegen|create/i })).not.toBeInTheDocument());
  });

  it('date field opens a calendar popover and persists a picked day', async () => {
    const user = userEvent.setup();
    renderRow(row());
    await user.click(screen.getByText('2024-02-02'));
    expect(screen.getByRole('dialog', { name: 'Datum bearbeiten' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /15\. Februar 2024/ })); // Feb 2024 view
    await waitFor(() => {
      expect(mockedPatch).toHaveBeenCalledWith(
        '/api/admin/paperless-audit/results/7',
        expect.objectContaining({ overrides: expect.objectContaining({ date: '2024-02-15' }) }),
      );
    });
  });

  it('adding a tag persists a tags override', async () => {
    const user = userEvent.setup();
    renderRow(row());
    await user.click(screen.getByText('Tag hinzufügen')); // reveal the add combobox
    const input = screen.getByRole('combobox', { name: 'Tag hinzufügen' });
    await user.click(input);
    await user.type(input, 'steuer');
    await user.click(await screen.findByRole('option', { name: /anlegen/i })); // create the tag
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
