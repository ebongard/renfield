/**
 * Contact points section of the Wissen drawer (#1240).
 *
 *  - lists the entity's numbers with kind, label and source;
 *  - adding posts kind/value/label and refetches; the field clears on success;
 *  - a backend error code (duplicate / invalid_number) is shown translated;
 *  - removing asks for an inline confirmation, then deletes by id;
 *  - a caller who may not edit sees no add form and no remove buttons;
 *  - a fax number is plain text, not a tel: link;
 *  - the drawer renders the section only while `contact_points_enabled` is on.
 * Fictitious numbers only.
 */
import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithProviders, renderWithRouter } from '../test-utils';
import EntityContactPoints from '../../../../src/frontend/src/components/wissen/EntityContactPoints';
import WissenDetailDrawer from '../../../../src/frontend/src/components/wissen/WissenDetailDrawer';
import type { AtomMatch } from '../../../../src/frontend/src/api/resources/brain';

const LIST_URL = `${BASE_URL}/api/knowledge-graph/entities/42/contact-points`;

function point(id: number, value: string, kind = 'mobile', label: string | null = 'private') {
  return {
    id, entity_id: 42, kind, label, value, normalized: '+491701234567',
    source_type: 'manual', circle_tier: 2, created_at: '2026-11-26T10:00:00',
  };
}

interface Api { calls: string[]; posted: unknown[] }

function mockApi(
  initial = [point(1, '0170 1234567')], postStatus = 201, postDetail = '', editable = true,
  deleteStatus = 200,
): Api {
  const api: Api = { calls: [], posted: [] };
  let rows = [...initial];
  server.use(
    http.get(LIST_URL, () => {
      api.calls.push('GET');
      return HttpResponse.json({ contact_points: rows, editable });
    }),
    http.post(LIST_URL, async ({ request }) => {
      const body = await request.json();
      api.posted.push(body);
      if (postStatus !== 201) return HttpResponse.json({ detail: postDetail }, { status: postStatus });
      const created = point(9, (body as { value: string }).value, 'phone', null);
      rows = [...rows, created];
      return HttpResponse.json(created, { status: 201 });
    }),
    http.delete(`${BASE_URL}/api/knowledge-graph/contact-points/:id`, ({ params }) => {
      api.calls.push(`DELETE ${String(params.id)}`);
      if (deleteStatus !== 200) return HttpResponse.json({ detail: 'not_owner' }, { status: deleteStatus });
      rows = rows.filter((r) => String(r.id) !== String(params.id));
      return HttpResponse.json({ success: true });
    }),
  );
  return api;
}

describe('EntityContactPoints', () => {
  it('lists the stored numbers', async () => {
    mockApi();
    renderWithProviders(<EntityContactPoints entityId={42} />);
    const rows = await screen.findAllByTestId('contact-point');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('0170 1234567');
    expect(screen.getByRole('link', { name: '0170 1234567' })).toHaveAttribute('href', 'tel:+491701234567');
  });

  it('shows an empty state', async () => {
    mockApi([]);
    renderWithProviders(<EntityContactPoints entityId={42} />);
    expect(await screen.findByText(/Keine Kontaktdaten|No contact details/)).toBeInTheDocument();
  });

  it('adds a number and clears the field', async () => {
    const api = mockApi([]);
    const user = userEvent.setup();
    renderWithProviders(<EntityContactPoints entityId={42} />);
    const input = await screen.findByRole('textbox');
    await user.type(input, '030 1234567');
    await user.click(screen.getByRole('button', { name: /Hinzufügen|^Add$/ }));
    await waitFor(() => expect(api.posted).toEqual([{ kind: 'mobile', value: '030 1234567', label: null }]));
    expect(await screen.findAllByTestId('contact-point')).toHaveLength(1);
    expect(input).toHaveValue('');
  });

  it('translates a duplicate', async () => {
    mockApi([], 409, 'duplicate');
    const user = userEvent.setup();
    renderWithProviders(<EntityContactPoints entityId={42} />);
    await user.type(await screen.findByRole('textbox'), '0170 1234567');
    await user.click(screen.getByRole('button', { name: /Hinzufügen|^Add$/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/schon gespeichert|already stored/);
  });

  it('removes a number by id after confirming', async () => {
    const api = mockApi();
    const user = userEvent.setup();
    renderWithProviders(<EntityContactPoints entityId={42} />);
    await screen.findAllByTestId('contact-point');
    await user.click(screen.getByRole('button', { name: /^0170 1234567 entfernen$|^Remove 0170 1234567$/ }));
    // First click only asks; cancelling sends nothing.
    await user.click(screen.getByRole('button', { name: /Abbrechen|Cancel/ }));
    expect(api.calls).not.toContain('DELETE 1');
    await user.click(screen.getByRole('button', { name: /^0170 1234567 entfernen$|^Remove 0170 1234567$/ }));
    await user.click(screen.getByRole('button', { name: /wirklich entfernen|Really remove/ }));
    await waitFor(() => expect(api.calls).toContain('DELETE 1'));
    await waitFor(() => expect(screen.queryAllByTestId('contact-point')).toHaveLength(0));
  });

  it('shows a read-only list to a caller who may not edit', async () => {
    mockApi([point(1, '0170 1234567')], 201, '', false);
    renderWithProviders(<EntityContactPoints entityId={42} />);
    await screen.findAllByTestId('contact-point');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: /entfernen|Remove/ })).toBeNull();
  });

  it('translates a refused delete', async () => {
    mockApi([point(1, '0170 1234567')], 201, '', true, 403);
    const user = userEvent.setup();
    renderWithProviders(<EntityContactPoints entityId={42} />);
    await screen.findAllByTestId('contact-point');
    await user.click(screen.getByRole('button', { name: /^0170 1234567 entfernen$|^Remove 0170 1234567$/ }));
    await user.click(screen.getByRole('button', { name: /wirklich entfernen|Really remove/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Eigentümer|owner/);
  });

  it('renders a fax number as text, not as a tel: link', async () => {
    mockApi([point(3, '030 7654321', 'fax', 'work')]);
    renderWithProviders(<EntityContactPoints entityId={42} />);
    const row = await screen.findByTestId('contact-point');
    expect(row).toHaveTextContent('030 7654321');
    expect(screen.queryByRole('link')).toBeNull();
  });
});

describe('WissenDetailDrawer contact points gate', () => {
  const kgNode: AtomMatch = {
    atom: { atom_id: 'kg_node:42', atom_type: 'kg_node', tier: 1, payload: { entity_id: 42, name: 'Beispiel GmbH', entity_type: 'organization' } },
    score: 1, snippet: 'Beispiel GmbH', rank: 1,
  };

  function features(enabled: boolean) {
    server.use(http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json({
      schicht_a_extraction_enabled: false, wissen_workspace_enabled: true, contact_points_enabled: enabled,
    })));
  }

  it('shows the section when enabled', async () => {
    features(true);
    mockApi();
    renderWithRouter(<WissenDetailDrawer atom={kgNode} onClose={() => {}} />);
    expect(await screen.findByTestId('entity-contact-points')).toBeInTheDocument();
  });

  it('renders nothing of it and fetches nothing when disabled', async () => {
    features(false);
    const api = mockApi();
    renderWithRouter(<WissenDetailDrawer atom={kgNode} onClose={() => {}} />);
    await screen.findByText('Beispiel GmbH');
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('entity-contact-points')).toBeNull();
    expect(api.calls).toEqual([]);
  });
});
