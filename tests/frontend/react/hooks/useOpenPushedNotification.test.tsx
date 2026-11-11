/**
 * Web Push (#79) -- from the click on a content-free push to the toast.
 *
 * The id arrives on the URL (new window) or as a worker message (open tab);
 * the content is fetched from the caller's own, server-filtered list.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import type { ReactNode } from 'react';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { installPushBrowser, uninstallPushBrowser } from '../pushBrowserMock';
import { useOpenPushedNotification } from '../../../../src/frontend/src/hooks/useOpenPushedNotification';
import { OPEN_NOTIFICATION_MESSAGE } from '../../../../src/frontend/src/sw/pushProtocol';
import type { NotificationData } from '../../../../src/frontend/src/hooks/useNotifications';

const LISTED = {
  id: 42,
  event_type: 'obligation.deadline',
  title: 'Frist Steuerbescheid',
  message: 'morgen fällig',
  urgency: 'critical',
  room_name: null,
  source: 'obligations',
  status: 'delivered',
  tts_delivered: false,
  created_at: '2026-11-11T10:00:00',
};

function mockList(rows: unknown[] = [LISTED]): { requests: number } {
  const seen = { requests: 0 };
  server.use(
    http.get(`${BASE_URL}/api/notifications`, () => {
      seen.requests += 1;
      return HttpResponse.json({ notifications: rows, total: rows.length, limit: 200, offset: 0 });
    }),
  );
  return seen;
}

function captureToasts(): { shown: NotificationData[]; stop: () => void } {
  const shown: NotificationData[] = [];
  const listener = (event: Event) => {
    shown.push((event as CustomEvent<NotificationData>).detail);
  };
  window.addEventListener('renfield-notification', listener);
  return { shown, stop: () => window.removeEventListener('renfield-notification', listener) };
}

function at(url: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>
  );
}

function useUnderTest(enabled: boolean) {
  useOpenPushedNotification(enabled);
  return useLocation();
}

describe('useOpenPushedNotification', () => {
  let toasts: ReturnType<typeof captureToasts>;

  beforeEach(() => {
    server.resetHandlers();
    toasts = captureToasts();
  });
  afterEach(() => {
    toasts.stop();
    uninstallPushBrowser();
  });

  it('a window opened by the click shows the notification and cleans the URL', async () => {
    const list = mockList();
    const { result } = renderHook(() => useUnderTest(true), {
      wrapper: at('/?notification=42&tab=x#frag'),
    });

    await waitFor(() => expect(toasts.shown).toHaveLength(1));
    expect(toasts.shown[0]).toEqual({
      notification_id: 42,
      title: 'Frist Steuerbescheid',
      message: 'morgen fällig',
      urgency: 'critical',
      source: 'obligations',
      room: null,
      tts_handled: true,
      created_at: '2026-11-11T10:00:00',
    });
    expect(list.requests).toBe(1);
    // The id is gone (a reload must not replay it); the rest of the URL stays.
    expect(result.current.search).toBe('?tab=x');
    expect(result.current.hash).toBe('#frag');
  });

  it('an open tab is told the id by the worker', async () => {
    const browser = installPushBrowser();
    mockList();
    renderHook(() => useUnderTest(true), { wrapper: at('/wissen') });

    act(() => browser.postFromWorker({ type: OPEN_NOTIFICATION_MESSAGE, notificationId: 42 }));
    await waitFor(() => expect(toasts.shown).toHaveLength(1));
    expect(toasts.shown[0].notification_id).toBe(42);
  });

  it('a notification the caller may not see (any more) is simply not shown', async () => {
    // The list is the server's answer for THIS account; id 42 is not in it.
    const list = mockList([{ ...LISTED, id: 7 }]);
    renderHook(() => useUnderTest(true), { wrapper: at('/?notification=42') });
    await waitFor(() => expect(list.requests).toBe(1));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(toasts.shown).toEqual([]);
  });

  it('feature off: the parameter is left alone and nothing is fetched', async () => {
    const browser = installPushBrowser();
    const list = mockList();
    const { result } = renderHook(() => useUnderTest(false), {
      wrapper: at('/?notification=42'),
    });
    act(() => browser.postFromWorker({ type: OPEN_NOTIFICATION_MESSAGE, notificationId: 42 }));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(list.requests).toBe(0);
    expect(toasts.shown).toEqual([]);
    expect(result.current.search).toBe('?notification=42');
  });

  it.each(['abc', '-3', '1.5', '0'])('an unusable id (%s) fetches nothing', async (raw) => {
    const list = mockList();
    const { result } = renderHook(() => useUnderTest(true), {
      wrapper: at(`/?notification=${raw}`),
    });
    await waitFor(() => expect(result.current.search).toBe(''));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(list.requests).toBe(0);
  });

  it('ignores worker messages that are not ours', async () => {
    const browser = installPushBrowser();
    const list = mockList();
    renderHook(() => useUnderTest(true), { wrapper: at('/') });
    act(() => {
      browser.postFromWorker({ type: 'something-else', notificationId: 42 });
      browser.postFromWorker({ type: OPEN_NOTIFICATION_MESSAGE, notificationId: 'x' });
      browser.postFromWorker(null);
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(list.requests).toBe(0);
  });

  it('a failed fetch leaves the app as it was', async () => {
    server.use(
      http.get(`${BASE_URL}/api/notifications`, () => HttpResponse.json({}, { status: 500 })),
    );
    const { result } = renderHook(() => useUnderTest(true), { wrapper: at('/?notification=42') });
    await waitFor(() => expect(result.current.search).toBe(''));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(toasts.shown).toEqual([]);
  });
});
