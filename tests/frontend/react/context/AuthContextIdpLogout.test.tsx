/**
 * #341: a session that began at an IdP with an end-session URL ends there too,
 * after renfield's own revoke. A password session never leaves the app.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { AuthProvider, useAuth } from '../../../../src/frontend/src/context/AuthContext';
import { leavePage } from '../../../../src/frontend/src/utils/leavePage';

vi.mock('../../../../src/frontend/src/utils/leavePage', () => ({
  leavePage: vi.fn<(url: string) => void>(),
}));

const ACCESS_TOKEN_KEY = 'renfield_access_token';
const LOGOUT_URL = 'https://idp.example/logout?client_id=reva';

function mockBackend(statusBody: Record<string, unknown>): string[] {
  const calls: string[] = [];
  server.use(
    http.get(`${BASE_URL}/api/auth/status`, () =>
      HttpResponse.json({ auth_enabled: true, allow_registration: false, ...statusBody })),
    http.get(`${BASE_URL}/api/auth/me`, () =>
      HttpResponse.json({ id: 1, username: 'u', role: 'Familie', permissions: [] })),
    http.post(`${BASE_URL}/api/auth/logout`, () => {
      calls.push('revoke');
      return HttpResponse.json({ message: 'ok' });
    }),
  );
  return calls;
}

async function loggedIn() {
  localStorage.setItem(ACCESS_TOKEN_KEY, 'tok');
  const hook = renderHook(() => useAuth(), { wrapper: AuthProvider });
  await waitFor(() => expect(hook.result.current.isAuthenticated).toBe(true));
  return hook;
}

const providers = [{ id: 'keycloak', display_name: 'Firmenkonto', logout_url: LOGOUT_URL }];

describe('AuthContext — IdP logout (#341)', () => {
  beforeEach(() => {
    server.resetHandlers();
    localStorage.clear();
  });
  afterEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('exposes providers and the password-form mode from /api/auth/status', async () => {
    mockBackend({ sso_providers: providers, password_form: 'collapsed' });
    const { result } = await loggedIn();
    await waitFor(() => expect(result.current.ssoProviders).toEqual(providers));
    expect(result.current.passwordForm).toBe('collapsed');
  });

  it('collapsed without providers is reported as visible', async () => {
    mockBackend({ sso_providers: [], password_form: 'collapsed' });
    const { result } = await loggedIn();
    await waitFor(() => expect(result.current.authEnabled).toBe(true));
    expect(result.current.passwordForm).toBe('visible');
  });

  it('an SSO session leaves for the IdP logout after the revoke', async () => {
    const calls = mockBackend({ sso_providers: providers });
    localStorage.setItem('renfield_login_provider', 'keycloak');
    const { result } = await loggedIn();
    await waitFor(() => expect(result.current.ssoProviders).toHaveLength(1));

    act(() => result.current.logout());

    await waitFor(() => expect(leavePage).toHaveBeenCalledWith(LOGOUT_URL));
    expect(calls).toEqual(['revoke']);
    expect(localStorage.getItem('renfield_login_provider')).toBeNull();
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });

  it('a password session stays in the app', async () => {
    const calls = mockBackend({ sso_providers: providers });
    const { result } = await loggedIn();
    await waitFor(() => expect(result.current.ssoProviders).toHaveLength(1));

    act(() => result.current.logout());

    await waitFor(() => expect(calls).toEqual(['revoke']));
    await waitFor(() => expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull());
    expect(leavePage).not.toHaveBeenCalled();
  });
  it('a password login clears a stale marker, so its logout stays in the app', async () => {
    const calls = mockBackend({ sso_providers: providers });
    server.use(
      http.post(`${BASE_URL}/api/auth/login`, () =>
        HttpResponse.json({ access_token: 'tok', refresh_token: 'rt', token_type: 'bearer' })),
    );
    localStorage.setItem('renfield_login_provider', 'keycloak');
    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider });
    await waitFor(() => expect(result.current.ssoProviders).toHaveLength(1));

    await act(async () => { await result.current.login('u', 'pw'); });
    expect(localStorage.getItem('renfield_login_provider')).toBeNull();

    act(() => result.current.logout());
    await waitFor(() => expect(calls).toEqual(['revoke']));
    await waitFor(() => expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull());
    expect(leavePage).not.toHaveBeenCalled();
  });

  it('a session that dies at refresh drops the marker', async () => {
    mockBackend({ sso_providers: providers });
    server.use(
      http.get(`${BASE_URL}/api/auth/me`, () => HttpResponse.json({ detail: 'x' }, { status: 401 })),
      http.post(`${BASE_URL}/api/auth/refresh`, () => HttpResponse.json({ detail: 'x' }, { status: 401 })),
    );
    localStorage.setItem(ACCESS_TOKEN_KEY, 'expired');
    localStorage.setItem('renfield_refresh_token', 'rt');
    localStorage.setItem('renfield_login_provider', 'keycloak');
    renderHook(() => useAuth(), { wrapper: AuthProvider });

    await waitFor(() => expect(localStorage.getItem('renfield_login_provider')).toBeNull());
  });
});
