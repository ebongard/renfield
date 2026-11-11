/**
 * Web Push (#79) -- logging out gives the device's subscription back.
 *
 * Otherwise the browser kept announcing "new notification" for an account that
 * is no longer logged in on it. The removal needs the session, so it has to
 * reach the server BEFORE the logout revokes the tokens. And a logout on a
 * browser that never switched push on must stay the single call it always was.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { installPushBrowser, uninstallPushBrowser, TEST_ENDPOINT } from '../pushBrowserMock';
import { AuthProvider, useAuth } from '../../../../src/frontend/src/context/AuthContext';

const ACCESS_TOKEN_KEY = 'renfield_access_token';

interface Seen {
  calls: string[];
  deleteAuth: string | null;
  deleteBody: unknown;
}

function mockBackend(deleteStatus = 204): Seen {
  const seen: Seen = { calls: [], deleteAuth: null, deleteBody: null };
  server.use(
    http.get(`${BASE_URL}/api/auth/status`, () =>
      HttpResponse.json({ auth_enabled: true, allow_registration: false }),
    ),
    http.get(`${BASE_URL}/api/auth/me`, () =>
      HttpResponse.json({ id: 1, username: 'u', role: 'Familie', permissions: [] }),
    ),
    http.delete(`${BASE_URL}/api/push/subscribe`, async ({ request }) => {
      seen.calls.push('DELETE /api/push/subscribe');
      seen.deleteAuth = request.headers.get('authorization');
      seen.deleteBody = await request.json();
      return new HttpResponse(null, { status: deleteStatus });
    }),
    http.post(`${BASE_URL}/api/auth/logout`, () => {
      seen.calls.push('POST /api/auth/logout');
      return HttpResponse.json({ message: 'ok' });
    }),
  );
  return seen;
}

async function loggedIn() {
  localStorage.setItem(ACCESS_TOKEN_KEY, 'the-access-token');
  const hook = renderHook(() => useAuth(), { wrapper: AuthProvider });
  await waitFor(() => expect(hook.result.current.isAuthenticated).toBe(true));
  return hook;
}

describe('AuthContext logout and Web Push', () => {
  beforeEach(() => {
    server.resetHandlers();
    localStorage.clear();
  });
  afterEach(() => {
    uninstallPushBrowser();
    localStorage.clear();
  });

  it('a browser without push logs out with the one call it always made', async () => {
    const seen = mockBackend();
    const { result } = await loggedIn();

    act(() => result.current.logout());

    await waitFor(() => expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull());
    expect(seen.calls).toEqual(['POST /api/auth/logout']);
  });

  it('permission granted but never subscribed: nothing is sent to /api/push', async () => {
    const browser = installPushBrowser({ permission: 'granted' });
    const seen = mockBackend();
    const { result } = await loggedIn();

    act(() => result.current.logout());

    await waitFor(() => expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull());
    expect(seen.calls).toEqual(['POST /api/auth/logout']);
    expect(browser.unsubscribe).not.toHaveBeenCalled();
  });

  it('a subscribed browser releases its subscription BEFORE the session is revoked', async () => {
    const browser = installPushBrowser({
      permission: 'granted',
      subscribedWithKey: new Uint8Array([1, 2, 3]),
    });
    const seen = mockBackend();
    const { result } = await loggedIn();

    act(() => result.current.logout());

    await waitFor(() => expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull());
    expect(seen.calls).toEqual(['DELETE /api/push/subscribe', 'POST /api/auth/logout']);
    expect(seen.deleteBody).toEqual({ endpoint: TEST_ENDPOINT });
    expect(seen.deleteAuth).toBe('Bearer the-access-token');
    expect(browser.unsubscribe).toHaveBeenCalledTimes(1);
    expect(browser.subscription).toBeNull();
  });

  it('a failing release never blocks the logout', async () => {
    const browser = installPushBrowser({
      permission: 'granted',
      subscribedWithKey: new Uint8Array([1, 2, 3]),
    });
    const seen = mockBackend(500);
    const { result } = await loggedIn();

    act(() => result.current.logout());

    await waitFor(() => expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull());
    expect(seen.calls).toEqual(['DELETE /api/push/subscribe', 'POST /api/auth/logout']);
    // The browser lets go regardless: the push service then answers the next
    // send with 410 and the server removes its row by itself.
    expect(browser.unsubscribe).toHaveBeenCalledTimes(1);
  });
});
