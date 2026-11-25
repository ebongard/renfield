/**
 * Notification bell (#1653) in the header.
 *
 *  - disabled => nothing rendered and no request to /api/notifications;
 *  - the badge shows the unread count (99+ cap) and hides at zero;
 *  - opening lists `mine=true` notifications; acknowledge / mark-all hit the API
 *    and the count refetches; a failed acknowledge is shown;
 *  - focus moves into the panel on open and back to the bell on Escape.
 */
import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithProviders } from '../test-utils';
import NotificationBell from '../../../../src/frontend/src/components/NotificationBell';

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
});
