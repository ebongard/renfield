/**
 * Notification bell (#1653) in the header.
 *
 *  - disabled => nothing rendered and no request to /api/notifications;
 *  - the badge shows the unread count (99+ cap) and hides at zero;
 *  - opening lists `mine=true` notifications; acknowledge / mark-all hit the API
 *    and the count refetches; a failed acknowledge is shown;
 *  - focus moves into the panel on open and back to the bell on Escape;
 *  - a long message (a voicemail transcript at its end) is clamped and opens on demand;
 *  - "save as note" (only with notes on) creates a tier-0 note; a title taken by ANOTHER note
 *    gets " (2)", the same note counts as saved; a caller's brackets never form a [[link]].
 */
import { afterEach, describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithProviders } from '../test-utils';
import NotificationBell, { noteFromNotification } from '../../../../src/frontend/src/components/NotificationBell';

interface Api {
  calls: string[];
  count: number;
  listParams: URLSearchParams | null;
}

function item(id: number, title: string, status = 'delivered') {
  return {
    id, event_type: 'telephony.voicemail', title, message: `${title} message`, urgency: 'info',
    status, created_at: new Date(Date.now() - 5 * 60_000).toISOString().replace('Z', ''),
    acknowledged_at: null, room_name: null, source: 'telephony', tts_delivered: false, delivered_at: null,
  };
}

function mockApi(count: number, ackStatus = 200): Api {
  const api: Api = { calls: [], count, listParams: null };
  server.use(
    http.get(`${BASE_URL}/api/notifications/unread-count`, () => {
      api.calls.push('GET count');
      return HttpResponse.json({ count: api.count });
    }),
    http.get(`${BASE_URL}/api/notifications`, ({ request }) => {
      api.calls.push('GET list');
      api.listParams = new URL(request.url).searchParams;
      return HttpResponse.json({
        notifications: [item(1, 'Neue Nachricht'), item(2, 'Alter Anruf', 'acknowledged')],
        total: 2, limit: 20, offset: 0,
      });
    }),
    http.patch(`${BASE_URL}/api/notifications/:id/acknowledge`, ({ params }) => {
      api.calls.push(`PATCH ack ${String(params.id)}`);
      if (ackStatus !== 200) return HttpResponse.json({ detail: 'Rate limit' }, { status: ackStatus });
      api.count = Math.max(0, api.count - 1);
      return HttpResponse.json({ success: true });
    }),
    http.post(`${BASE_URL}/api/notifications/acknowledge-all`, () => {
      api.calls.push('POST ack-all');
      api.count = 0;
      return HttpResponse.json({ success: true, acknowledged: 1 });
    }),
  );
  return api;
}

/** jsdom has no layout: a clamped message "overflows" when its text is long. */
function fakeLayout(): () => void {
  const proto = HTMLElement.prototype;
  const sh = Object.getOwnPropertyDescriptor(proto, 'scrollHeight');
  const ch = Object.getOwnPropertyDescriptor(proto, 'clientHeight');
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) { return (this.textContent ?? '').length > 90 ? 60 : 20; },
  });
  Object.defineProperty(proto, 'clientHeight', {
    configurable: true,
    get(this: HTMLElement) { return this.className.includes('line-clamp-2') ? 32 : 60; },
  });
  return () => {
    if (sh) Object.defineProperty(proto, 'scrollHeight', sh);
    if (ch) Object.defineProperty(proto, 'clientHeight', ch);
  };
}

let restoreLayout: (() => void) | null = null;
afterEach(() => {
  restoreLayout?.();
  restoreLayout = null;
});

describe('NotificationBell', () => {
  it('renders nothing and fetches nothing when disabled', async () => {
    const api = mockApi(3);
    renderWithProviders(<NotificationBell enabled={false} />);
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('notification-bell')).toBeNull();
    expect(api.calls).toEqual([]);
  });

  it('shows the unread badge and hides it at zero', async () => {
    mockApi(3);
    renderWithProviders(<NotificationBell enabled />);
    expect(await screen.findByTestId('notification-bell-badge')).toHaveTextContent('3');
  });

  it('caps the badge at 99+', async () => {
    mockApi(250);
    renderWithProviders(<NotificationBell enabled />);
    expect(await screen.findByTestId('notification-bell-badge')).toHaveTextContent('99+');
  });

  it('no badge when nothing is unread', async () => {
    const api = mockApi(0);
    renderWithProviders(<NotificationBell enabled />);
    await waitFor(() => expect(api.calls).toContain('GET count'));
    expect(screen.queryByTestId('notification-bell-badge')).toBeNull();
  });

  it('lists own notifications on open and acknowledges one', async () => {
    const api = mockApi(1);
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell enabled />);
    await screen.findByTestId('notification-bell-badge');
    expect(api.calls).not.toContain('GET list'); // list only loads when opened

    await user.click(screen.getByTestId('notification-bell'));
    const items = await screen.findAllByTestId('notification-bell-item');
    expect(items).toHaveLength(2);
    expect(api.listParams?.get('mine')).toBe('true');

    // Only the unread one offers "mark as read".
    const markRead = screen.getAllByRole('button', { name: /gelesen markieren|mark as read/i });
    expect(markRead).toHaveLength(1);
    await user.click(markRead[0]);
    await waitFor(() => expect(api.calls).toContain('PATCH ack 1'));
    await waitFor(() => expect(screen.queryByTestId('notification-bell-badge')).toBeNull());
  });

  it('mark all read clears the badge', async () => {
    const api = mockApi(2);
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell enabled />);
    await screen.findByTestId('notification-bell-badge');
    await user.click(screen.getByTestId('notification-bell'));
    await user.click(await screen.findByRole('button', { name: /alle gelesen|mark all read/i }));
    await waitFor(() => expect(api.calls).toContain('POST ack-all'));
    await waitFor(() => expect(screen.queryByTestId('notification-bell-badge')).toBeNull());
  });

  it('shows an error when acknowledging fails', async () => {
    mockApi(1, 429);
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell enabled />);
    await user.click(await screen.findByTestId('notification-bell'));
    await screen.findAllByTestId('notification-bell-item');
    await user.click(screen.getByRole('button', { name: /gelesen markieren|mark as read/i }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByTestId('notification-bell-badge')).toHaveTextContent('1');
  });

  it('moves focus into the panel and back to the bell on Escape', async () => {
    mockApi(1);
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell enabled />);
    const bell = await screen.findByTestId('notification-bell');
    await user.click(bell);
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(bell).toHaveFocus();
  });

  it('a long message opens and closes; a short one offers no toggle', async () => {
    restoreLayout = fakeLayout();
    const long = 'Neue Nachricht auf dem Anrufbeantworter von 0301234567 an 021611234567 (0:09): '
      + '„Bitte rufen Sie zurück.“ (automatisch verschriftet, kann Fehler enthalten)';
    server.use(
      http.get(`${BASE_URL}/api/notifications/unread-count`, () => HttpResponse.json({ count: 1 })),
      http.get(`${BASE_URL}/api/notifications`, () => HttpResponse.json({
        notifications: [{ ...item(1, 'Neue Nachricht'), message: long }, item(2, 'Kurz')],
        total: 2, limit: 20, offset: 0,
      })),
    );
    const user = userEvent.setup();
    renderWithProviders(<NotificationBell enabled />);
    await user.click(await screen.findByTestId('notification-bell'));
    const messages = await screen.findAllByTestId('notification-bell-message');
    expect(messages[0]).toHaveTextContent('Bitte rufen Sie zurück.');
    expect(messages[0].className).toContain('line-clamp-2');

    const toggles = screen.getAllByRole('button', { name: /mehr anzeigen|show more/i });
    expect(toggles).toHaveLength(1); // only the long one
    expect(toggles[0]).toHaveAttribute('aria-expanded', 'false');
    expect(toggles[0]).toHaveAttribute('aria-controls', messages[0].id);

    await user.click(toggles[0]);
    expect(messages[0].className).not.toContain('line-clamp-2');
    const less = screen.getByRole('button', { name: /weniger anzeigen|show less/i });
    expect(less).toHaveAttribute('aria-expanded', 'true');
    // Opening a message keeps the panel open (the click stays inside it).
    expect(screen.getAllByTestId('notification-bell-item')).toHaveLength(2);

    await user.click(less);
    expect(messages[0].className).toContain('line-clamp-2');
  });

  describe('save as note', () => {
    interface NotesApi { posted: Array<Record<string, unknown>>; existing: Array<{ title: string; body: string }> }
    function mockNotes(status = 200, existing: Array<{ title: string; body: string }> = []): NotesApi {
      const api: NotesApi = { posted: [], existing };
      server.use(
        http.get(`${BASE_URL}/api/notifications/unread-count`, () => HttpResponse.json({ count: 1 })),
        http.get(`${BASE_URL}/api/notifications`, () => HttpResponse.json({
          notifications: [item(1, 'Neue Nachricht')], total: 1, limit: 20, offset: 0,
        })),
        http.get(`${BASE_URL}/api/notes`, () => HttpResponse.json(api.existing.map((e, i) => ({
          id: i + 1, ...e, circle_tier: 0, project_id: null, owner_id: 1, atom_id: 'a', created_at: '', updated_at: '',
        })))),
        http.post(`${BASE_URL}/api/notes`, async ({ request }) => {
          const body = (await request.json()) as { title: string; body: string };
          api.posted.push(body);
          if (status !== 200) return HttpResponse.json({ detail: 'boom' }, { status });
          if (api.existing.some((e) => e.title === body.title)) {
            return HttpResponse.json({ detail: 'A note with this title already exists' }, { status: 409 });
          }
          api.existing.push({ title: body.title, body: body.body });
          return HttpResponse.json({ id: 7, ...body, circle_tier: 0, project_id: null, owner_id: 1,
            atom_id: 'a', created_at: '', updated_at: '' });
        }),
      );
      return api;
    }
    const saveButton = () => screen.getByRole('button', { name: /als notiz speichern|save as note/i });

    async function openAndSave() {
      const user = userEvent.setup();
      renderWithProviders(<NotificationBell enabled notesEnabled />);
      await user.click(await screen.findByTestId('notification-bell'));
      await user.click(await screen.findByRole('button', { name: /als notiz speichern|save as note/i }));
    }

    it('creates a private note from the notification', async () => {
      const api = mockNotes();
      await openAndSave();
      const saved = await screen.findByRole('button', { name: /als notiz gespeichert|saved as note/i });
      expect(saved).toBeDisabled(); // stays mounted: focus is not lost
      expect(api.posted).toHaveLength(1);
      expect(api.posted[0].title).toMatch(/^Neue Nachricht – /);
      expect(api.posted[0].body).toBe('Neue Nachricht message');
      expect(api.posted[0].circle_tier).toBe(0);
    });

    it('the same note already there counts as saved', async () => {
      const first = noteFromNotification(item(1, 'Neue Nachricht'), 'de');
      const api = mockNotes(200, [{ title: first.title, body: first.body }]);
      await openAndSave();
      await screen.findByRole('button', { name: /als notiz gespeichert|saved as note/i });
      expect(api.posted).toHaveLength(1); // no second copy
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('a title taken by ANOTHER note gets " (2)" -- never a false "saved"', async () => {
      const first = noteFromNotification(item(1, 'Neue Nachricht'), 'de');
      const api = mockNotes(200, [{ title: first.title, body: 'eine andere Nachricht' }]);
      await openAndSave();
      await screen.findByRole('button', { name: /als notiz gespeichert|saved as note/i });
      expect(api.posted.map((p) => p.title)).toEqual([first.title, `${first.title} (2)`]);
      expect(api.existing.at(-1)?.body).toBe('Neue Nachricht message');
    });

    it('a server error is shown and the button stays', async () => {
      mockNotes(500);
      await openAndSave();
      expect(await screen.findByRole('alert')).toBeInTheDocument();
      expect(saveButton()).toBeEnabled();
    });

    it('no button with notes off', async () => {
      mockNotes();
      const user = userEvent.setup();
      renderWithProviders(<NotificationBell enabled />);
      await user.click(await screen.findByTestId('notification-bell'));
      await screen.findAllByTestId('notification-bell-item');
      expect(screen.queryByRole('button', { name: /als notiz speichern|save as note/i })).toBeNull();
    });

    it("a caller's brackets never form a [[link]], however many", () => {
      const note = noteFromNotification(
        { title: 'Neue [[Nachricht]]', message: '„Siehe [[Geheim]], [[[x]]] und ]]y[[“', created_at: '2026-11-27T10:04:00' },
        'de',
      );
      expect(note.body).not.toMatch(/[[\]]/);
      expect(note.title).not.toMatch(/[[\]]/);
      expect(note.body).toContain('Geheim');
    });

    it('a long title is cut, the date never', () => {
      const n = { title: 'x'.repeat(400), message: 'm', created_at: '2026-11-27T10:04:05' };
      const note = noteFromNotification(n, 'de');
      expect(note.title.length).toBeLessThanOrEqual(255);
      expect(note.title).toMatch(/ – .*2026/);
      expect(noteFromNotification(n, 'de', 2).title).toMatch(/\(2\)$/);
    });
  });
});
