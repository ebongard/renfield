import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import AuthCallback from '../../../../src/frontend/src/pages/AuthCallback';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { unauthenticatedAuthMock } from '../test-auth-mock';
import { storePkce, clearPkce } from '../../../../src/frontend/src/utils/pkce';
import {
  ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY,
} from '../../../../src/frontend/src/utils/authTokens';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';

// Mock AuthContext.useAuth (spy on fetchUser).
vi.mock('../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/context/AuthContext')>(
    '../../../../src/frontend/src/context/AuthContext',
  );
  return { ...actual, useAuth: vi.fn<() => AuthContextValue>() };
});

// Mock react-router: navigate spy + query params fed per-test.
const mockNavigate = vi.fn<(to: string, opts?: { replace?: boolean }) => void>();
let searchParams = new URLSearchParams();
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useSearchParams: () => [searchParams, vi.fn()],
  };
});

const fetchUser = vi.fn(async () => null);

describe('AuthCallback (SSO one-time-code exchange)', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue({ ...unauthenticatedAuthMock, fetchUser });
    mockNavigate.mockClear();
    fetchUser.mockClear();
    localStorage.clear();
    sessionStorage.clear();
    clearPkce();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('exchanges the code, stores tokens, and navigates to the target', async () => {
    storePkce('a'.repeat(64), 'the-state');
    searchParams = new URLSearchParams({ code: 'one-time', state: 'the-state', from: '/brain' });

    let sentBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE_URL}/api/auth/sso/exchange`, async ({ request }) => {
        sentBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          access_token: 'AT', refresh_token: 'RT', token_type: 'bearer', expires_in: 60,
        });
      }),
    );

    renderWithProviders(<AuthCallback />);

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/brain', { replace: true }));
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBe('AT');
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe('RT');
    expect(fetchUser).toHaveBeenCalled();
    // The verifier is sent (proving possession); the token never came via URL.
    expect(sentBody).toMatchObject({ code: 'one-time', code_verifier: 'a'.repeat(64), state: 'the-state' });
    // PKCE stash cleared after exchange.
    expect(sessionStorage.getItem('renfield_pkce_verifier')).toBeNull();
  });

  it('rejects a state mismatch without calling the backend', async () => {
    storePkce('a'.repeat(64), 'my-state');
    searchParams = new URLSearchParams({ code: 'x', state: 'attacker-state' });

    const exchange = vi.fn();
    server.use(
      http.post(`${BASE_URL}/api/auth/sso/exchange`, () => { exchange(); return HttpResponse.json({}); }),
    );

    renderWithProviders(<AuthCallback />);

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/login?error=sso', { replace: true }));
    expect(exchange).not.toHaveBeenCalled();
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });

  it('fails closed when the exchange endpoint rejects the code', async () => {
    storePkce('a'.repeat(64), 'the-state');
    searchParams = new URLSearchParams({ code: 'used', state: 'the-state' });

    server.use(
      http.post(`${BASE_URL}/api/auth/sso/exchange`, () =>
        HttpResponse.json({ detail: 'Invalid or expired authorization code' }, { status: 400 })),
    );

    renderWithProviders(<AuthCallback />);

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/login?error=sso', { replace: true }));
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });
});
