import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import apiClient, { PASSWORD_CHANGE_REQUIRED_EVENT } from '../../../../src/frontend/src/utils/axios';
import { ACCESS_TOKEN_KEY } from '../../../../src/frontend/src/utils/authTokens';

// Regression guard for the feature-flag auth race: the bearer token is attached
// by a module-scope request interceptor (registered when apiClient is created),
// NOT by an interceptor AuthContext registers in a useEffect. So a request that
// fires before any component mounts (e.g. the first /api/config/features query)
// still carries the token — no 401, no feature flags cached false.
describe('apiClient auth interceptor', () => {
  beforeEach(() => localStorage.clear());

  it('attaches the bearer token from localStorage on every request', async () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'tok-abc');
    let auth: string | null = 'MISSING';
    server.use(
      http.get(`${BASE_URL}/api/_probe`, ({ request }) => {
        auth = request.headers.get('authorization');
        return HttpResponse.json({ ok: true });
      }),
    );
    await apiClient.get('/api/_probe');
    expect(auth).toBe('Bearer tok-abc');
  });

  it('sends no Authorization header when no token is stored', async () => {
    let auth: string | null = 'MISSING';
    server.use(
      http.get(`${BASE_URL}/api/_probe2`, ({ request }) => {
        auth = request.headers.get('authorization');
        return HttpResponse.json({ ok: true });
      }),
    );
    await apiClient.get('/api/_probe2');
    expect(auth).toBeNull();
  });

  it('does not overwrite an explicit Authorization header', async () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'tok-abc');
    let auth: string | null = 'MISSING';
    server.use(
      http.get(`${BASE_URL}/api/_probe3`, ({ request }) => {
        auth = request.headers.get('authorization');
        return HttpResponse.json({ ok: true });
      }),
    );
    await apiClient.get('/api/_probe3', { headers: { Authorization: 'Bearer explicit' } });
    expect(auth).toBe('Bearer explicit');
  });
});

// A forced password rotation flagged DURING a session is invisible to the app:
// the user object is stale, so ProtectedRoute does not redirect, and every call
// 403s `password_change_required` while the sockets reconnect-loop. The
// interceptor turns that 403 into an event AuthContext acts on.
describe('apiClient forced-rotation signal', () => {
  beforeEach(() => localStorage.clear());

  it('fires the event on a 403 password_change_required', async () => {
    let fired = 0;
    const onFired = (): void => { fired += 1; };
    window.addEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, onFired);
    server.use(
      http.get(`${BASE_URL}/api/_probe_rotate`, () =>
        HttpResponse.json({ detail: 'password_change_required' }, { status: 403 })),
    );
    await expect(apiClient.get('/api/_probe_rotate')).rejects.toBeTruthy();
    window.removeEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, onFired);
    expect(fired).toBe(1);
  });

  it('stays silent on an ordinary 403', async () => {
    let fired = 0;
    const onFired = (): void => { fired += 1; };
    window.addEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, onFired);
    server.use(
      http.get(`${BASE_URL}/api/_probe_forbidden`, () =>
        HttpResponse.json({ detail: 'Not enough permissions' }, { status: 403 })),
    );
    await expect(apiClient.get('/api/_probe_forbidden')).rejects.toBeTruthy();
    window.removeEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, onFired);
    expect(fired).toBe(0);
  });

  it('stays silent on a 403 whose body is not inspectable JSON', async () => {
    // A `responseType` of blob/arraybuffer leaves `data` unparsed. Treating
    // that as a rotation candidate was tried and reverted: a CSRF 403 on the
    // arraybuffer TTS POST is indistinguishable from here, and every playback
    // would have fired a pointless /auth/me.
    let fired = 0;
    const onFired = (): void => { fired += 1; };
    window.addEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, onFired);
    server.use(
      http.get(`${BASE_URL}/api/_probe_blob`, () =>
        new HttpResponse(new Blob(['nope']), { status: 403 })),
    );
    await expect(apiClient.get('/api/_probe_blob', { responseType: 'blob' })).rejects.toBeTruthy();
    window.removeEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, onFired);
    expect(fired).toBe(0);
  });
});
