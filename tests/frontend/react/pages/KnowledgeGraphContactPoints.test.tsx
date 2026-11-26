/**
 * Contact points on the graph page (#1240): the entity edit dialog of a person or
 * organization shows "Kontaktdaten" while `contact_points_enabled` is on; a place
 * never does, and with the flag off nothing is fetched. Fictitious numbers only.
 */
import { describe, it, expect } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithRouter } from '../test-utils';
import KnowledgeGraphPage from '../../../../src/frontend/src/pages/KnowledgeGraphPage';
import { ThemeProvider } from '../../../../src/frontend/src/context/ThemeContext';

const page = () => <ThemeProvider><KnowledgeGraphPage /></ThemeProvider>;

const ENTITIES = [
  { id: 42, name: 'Beispiel Person', entity_type: 'person', circle_tier: 2, mention_count: 1 },
  { id: 43, name: 'Beispielstadt', entity_type: 'place', circle_tier: 2, mention_count: 1 },
];

function mockApi(enabled: boolean): string[] {
  const calls: string[] = [];
  server.use(
    http.get(`${BASE_URL}/api/config/features`, () => HttpResponse.json({ contact_points_enabled: enabled })),
    http.get(`${BASE_URL}/api/knowledge-graph/entities`, () => HttpResponse.json({ entities: ENTITIES, total: 2 })),
    http.get(`${BASE_URL}/api/knowledge-graph/stats`, () => HttpResponse.json({ entity_count: 2, relation_count: 0, entity_types: {} })),
    http.get(`${BASE_URL}/api/knowledge-graph/circle-tiers`, () => HttpResponse.json({ tiers: [] })),
    http.get(`${BASE_URL}/api/knowledge-graph/entities/:id/contact-points`, ({ params }) => {
      calls.push(String(params.id));
      return HttpResponse.json({
        contact_points: [{
          id: 1, entity_id: 42, kind: 'mobile', label: null, value: '0170 1234567',
          normalized: '+491701234567', source_type: 'manual', circle_tier: 2, created_at: '',
        }],
        editable: true,
      });
    }),
  );
  return calls;
}

async function openEdit(name: string) {
  const user = userEvent.setup();
  const row = (await screen.findByText(name)).closest('tr, li, div[class*="card"], div') as HTMLElement;
  const editButtons = await screen.findAllByTitle(/Bearbeiten|Edit/);
  // The edit button in the same row as the entity name.
  const own = editButtons.find((b) => row.contains(b))
    ?? editButtons[ENTITIES.findIndex((e) => e.name === name)];
  await user.click(own);
  return screen.findByRole('dialog');
}

describe('KnowledgeGraphPage contact points', () => {
  it('shows them in the edit dialog of a person', async () => {
    const calls = mockApi(true);
    renderWithRouter(page());
    const dialog = await openEdit('Beispiel Person');
    expect(await within(dialog).findByTestId('entity-contact-points')).toBeInTheDocument();
    expect(await within(dialog).findByText('0170 1234567')).toBeInTheDocument();
    expect(calls).toContain('42');
  });

  it('never for a place', async () => {
    const calls = mockApi(true);
    renderWithRouter(page());
    const dialog = await openEdit('Beispielstadt');
    await new Promise((r) => setTimeout(r, 50));
    expect(within(dialog).queryByTestId('entity-contact-points')).toBeNull();
    expect(calls).toEqual([]);
  });

  it('not at all while the flag is off', async () => {
    const calls = mockApi(false);
    renderWithRouter(page());
    const dialog = await openEdit('Beispiel Person');
    await new Promise((r) => setTimeout(r, 50));
    expect(within(dialog).queryByTestId('entity-contact-points')).toBeNull();
    expect(calls).toEqual([]);
  });
});
