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
import apiClient from '../../../../src/frontend/src/utils/axios';
import { releasePushLocally } from '../../../../src/frontend/src/api/resources/push';

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

/**
 * A session can end without anybody pressing "log out": the token expires or
 * is revoked, the refresh fails, the token disappears, or another account
 * takes the browser over. In each case the device must stop being a channel
 * for the previous account -- on a shared tablet the next person would
 * otherwise be shown that account's alerts. There is no session left for the
 * server call, so the release is the browser's own `unsubscribe()`.
 */
describe('AuthContext: a session that ends without a logout releases Web Push', () => {
  const OWNER_KEY = 'renfield_push_owner';
  const subscribed = () =>
    installPushBrowser({ permission: 'granted', subscribedWithKey: new Uint8Array([1, 2, 3]) });

  function backend(options: { me: number; refresh?: number; userId?: number }): Seen {
    const seen: Seen = { calls: [], deleteAuth: null, deleteBody: null };
    server.use(
      http.get(`${BASE_URL}/api/auth/status`, () =>
        HttpResponse.json({ auth_enabled: true, allow_registration: false }),
      ),
      http.get(`${BASE_URL}/api/auth/me`, () =>
        options.me === 200
          ? HttpResponse.json({ id: options.userId ?? 1, username: 'u', role: 'Familie', permissions: [] })
          : HttpResponse.json({ detail: 'x' }, { status: options.me }),
      ),
      http.post(`${BASE_URL}/api/auth/refresh`, () =>
        HttpResponse.json({ detail: 'revoked' }, { status: options.refresh ?? 401 }),
      ),
      http.delete(`${BASE_URL}/api/push/subscribe`, () => {
        seen.calls.push('DELETE /api/push/subscribe');
        return new HttpResponse(null, { status: 204 });
      }),
    );
    return seen;
  }

  async function mounted() {
    const hook = renderHook(() => useAuth(), { wrapper: AuthProvider });
    await waitFor(() => expect(hook.result.current.loading).toBe(false));
    return hook;
  }

  beforeEach(() => {
    server.resetHandlers();
    localStorage.clear();
  });
  afterEach(() => {
    uninstallPushBrowser();
    localStorage.clear();
  });

  it('expired token and a failed refresh: the browser lets go, without a server call', async () => {
    const browser = subscribed();
    localStorage.setItem(ACCESS_TOKEN_KEY, 'expired');
    localStorage.setItem('renfield_refresh_token', 'revoked');
    localStorage.setItem(OWNER_KEY, '1');
    const seen = backend({ me: 401 });

    const { result } = await mounted();

    expect(result.current.isAuthenticated).toBe(false);
    await waitFor(() => expect(browser.subscription).toBeNull());
    expect(seen.calls).toEqual([]);
    expect(localStorage.getItem(OWNER_KEY)).toBeNull();
  });

  it('expired token and no refresh token at all: the same', async () => {
    const browser = subscribed();
    localStorage.setItem(ACCESS_TOKEN_KEY, 'expired');
    backend({ me: 401 });

    await mounted();

    await waitFor(() => expect(browser.subscription).toBeNull());
  });

  it('/auth/me fails for another reason and the session is dropped: the same', async () => {
    const browser = subscribed();
    localStorage.setItem(ACCESS_TOKEN_KEY, 'token');
    backend({ me: 500 });

    const { result } = await mounted();

    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    await waitFor(() => expect(browser.subscription).toBeNull());
  });

  it('the token is simply gone (cleared elsewhere): the same', async () => {
    const browser = subscribed();
    backend({ me: 401 });

    await mounted();

    await waitFor(() => expect(browser.subscription).toBeNull());
  });

  it('a session revoked WHILE the app is open: the next refusal releases the device', async () => {
    const browser = subscribed();
    localStorage.setItem(ACCESS_TOKEN_KEY, 'good');
    localStorage.setItem('renfield_refresh_token', 'good');
    backend({ me: 200 });
    const { result } = await mounted();
    expect(result.current.isAuthenticated).toBe(true);
    expect(browser.unsubscribe).not.toHaveBeenCalled();

    backend({ me: 401 });
    await act(async () => {
      await result.current.fetchUser();
    });

    expect(result.current.isAuthenticated).toBe(false);
    await waitFor(() => expect(browser.subscription).toBeNull());
  });

  it('any request refused mid-session whose refresh fails: the 401 handler releases the device', async () => {
    const browser = subscribed();
    localStorage.setItem(ACCESS_TOKEN_KEY, 'good');
    localStorage.setItem('renfield_refresh_token', 'good');
    backend({ me: 200 });
    server.use(
      http.get(`${BASE_URL}/api/rooms`, () => HttpResponse.json({ detail: 'revoked' }, { status: 401 })),
    );
    const { result } = await mounted();
    expect(result.current.isAuthenticated).toBe(true);

    // Not /auth/me: this goes through the api client's 401 interceptor only.
    await act(async () => {
      await apiClient.get('/api/rooms').catch(() => undefined);
    });

    await waitFor(() => expect(result.current.isAuthenticated).toBe(false));
    await waitFor(() => expect(browser.subscription).toBeNull());
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });

  it('another account now logged in on this browser: its predecessor\'s subscription goes', async () => {
    const browser = subscribed();
    localStorage.setItem(ACCESS_TOKEN_KEY, 'token-of-the-new-account');
    localStorage.setItem(OWNER_KEY, '7');
    const seen = backend({ me: 200, userId: 1 });

    const { result } = await mounted();

    expect(result.current.isAuthenticated).toBe(true);
    await waitFor(() => expect(browser.subscription).toBeNull());
    expect(localStorage.getItem(OWNER_KEY)).toBeNull();
    expect(seen.calls).toEqual([]);
  });

  it('the same account as before keeps its subscription', async () => {
    const browser = subscribed();
    localStorage.setItem(ACCESS_TOKEN_KEY, 'token');
    localStorage.setItem(OWNER_KEY, '1');
    backend({ me: 200, userId: 1 });

    const { result } = await mounted();

    expect(result.current.isAuthenticated).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(browser.unsubscribe).not.toHaveBeenCalled();
    expect(localStorage.getItem(OWNER_KEY)).toBe('1');
  });

  it('a browser that never switched push on is not touched by any of this', async () => {
    const browser = installPushBrowser({ permission: 'default' });
    localStorage.setItem(ACCESS_TOKEN_KEY, 'expired');
    backend({ me: 401 });

    await mounted();

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(browser.getRegistration).not.toHaveBeenCalled();
  });

  it('a failing unsubscribe never surfaces', async () => {
    const browser = subscribed();
    browser.unsubscribe.mockRejectedValueOnce(new Error('push service unreachable'));
    localStorage.setItem(ACCESS_TOKEN_KEY, 'expired');
    backend({ me: 401 });

    const { result } = await mounted();

    await waitFor(() => expect(browser.unsubscribe).toHaveBeenCalled());
    expect(result.current.isAuthenticated).toBe(false);

    // Asked directly, because a rejection inside the fire-and-forget call
    // above would be invisible here: the promise itself must resolve.
    browser.unsubscribe.mockRejectedValueOnce(new Error('push service unreachable'));
    await expect(releasePushLocally()).resolves.toBeUndefined();
    expect(browser.unsubscribe).toHaveBeenCalledTimes(2);
  });
});
