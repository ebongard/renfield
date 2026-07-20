import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import NotesPage from '../../../../src/frontend/src/pages/NotesPage';
import { renderWithProviders } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';
import type { Note } from '../../../../src/frontend/src/api/resources/notes';

function mkNote(over: Partial<Note>): Note {
  return {
    id: 1, title: 'Roadmap', body: 'phoenix migration plan', circle_tier: 0,
    project_id: null, owner_id: 1, atom_id: 'a-1',
    created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
    ...over,
  };
}

describe('NotesPage', () => {
  it('lists notes and creates one', async () => {
    const store: Note[] = [mkNote({ id: 1, title: 'Existing' })];
    let sentTitle: string | null = null;
    server.use(
      http.get(`${BASE_URL}/api/notes`, () => HttpResponse.json(store)),
      http.post(`${BASE_URL}/api/notes`, async ({ request }) => {
        const b = (await request.json()) as { title: string };
        sentTitle = b.title;
        const created = mkNote({ id: 2, title: b.title });
        store.unshift(created);
        return HttpResponse.json(created);
      }),
    );

    renderWithProviders(<NotesPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Existing')).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText(i18n.t('notes.titlePlaceholder')), 'Fresh');
    await user.click(screen.getByRole('button', { name: i18n.t('notes.create') }));

    await waitFor(() => expect(sentTitle).toBe('Fresh'));
    await waitFor(() => expect(screen.getByText('Fresh')).toBeInTheDocument());
  });

  it('shows the empty state', async () => {
    server.use(http.get(`${BASE_URL}/api/notes`, () => HttpResponse.json([])));
    renderWithProviders(<NotesPage />);
    await waitFor(() => expect(screen.getByText(i18n.t('notes.empty'))).toBeInTheDocument());
  });

  it('edits a note inline', async () => {
    let savedBody: string | null = null;
    const store: Note[] = [mkNote({ id: 5, title: 'Draft', body: 'old' })];
    server.use(
      http.get(`${BASE_URL}/api/notes`, () => HttpResponse.json(store)),
      http.put(`${BASE_URL}/api/notes/5`, async ({ request }) => {
        const b = (await request.json()) as { body?: string; title?: string };
        savedBody = b.body ?? null;
        store[0] = mkNote({ id: 5, title: b.title ?? 'Draft', body: b.body ?? 'old' });
        return HttpResponse.json(store[0]);
      }),
    );

    renderWithProviders(<NotesPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('Draft')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: i18n.t('notes.edit') }));

    // Two body textareas now exist (the create form's + the card's edit); the
    // card's is the last one.
    const bodyBoxes = screen.getAllByLabelText(i18n.t('notes.bodyPlaceholder'));
    const bodyBox = bodyBoxes[bodyBoxes.length - 1];
    await user.clear(bodyBox);
    await user.type(bodyBox, 'new content');
    await user.click(screen.getByRole('button', { name: i18n.t('notes.save') }));

    await waitFor(() => expect(savedBody).toBe('new content'));
  });

  it('deletes a note after an inline confirm', async () => {
    let deleted: number | null = null;
    const store: Note[] = [mkNote({ id: 8, title: 'ToDrop' })];
    server.use(
      http.get(`${BASE_URL}/api/notes`, () => HttpResponse.json(store)),
      http.delete(`${BASE_URL}/api/notes/8`, () => {
        deleted = 8;
        store.length = 0;
        return HttpResponse.json({ status: 'deleted', id: 8 });
      }),
    );

    renderWithProviders(<NotesPage />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText('ToDrop')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: i18n.t('notes.delete') }));
    expect(deleted).toBeNull();
    await user.click(screen.getByRole('button', { name: i18n.t('notes.confirmDelete') }));
    await waitFor(() => expect(deleted).toBe(8));
    await waitFor(() => expect(screen.getByText(i18n.t('notes.empty'))).toBeInTheDocument());
  });
});
