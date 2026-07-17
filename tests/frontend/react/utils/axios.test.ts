import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import apiClient from '../../../../src/frontend/src/utils/axios';
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
