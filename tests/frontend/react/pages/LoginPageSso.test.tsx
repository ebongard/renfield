/**
 * #341: redirect-provider buttons, the collapsed password form and the D3 note.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import LoginPage from '../../../../src/frontend/src/pages/LoginPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { unauthenticatedAuthMock } from '../test-auth-mock';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { leavePage } from '../../../../src/frontend/src/utils/leavePage';

vi.mock('../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/context/AuthContext')>(
    '../../../../src/frontend/src/context/AuthContext',
  );
  return { ...actual, useAuth: vi.fn<() => AuthContextValue>() };
});

vi.mock('../../../../src/frontend/src/utils/leavePage', () => ({
  leavePage: vi.fn<(url: string) => void>(),
}));

let search = '';
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return {
    ...actual,
    useNavigate: () => vi.fn(),
    useLocation: () => ({ state: null, pathname: '/login', search }),
  };
});

const keycloak = { id: 'keycloak', display_name: 'Firmenkonto', logout_url: null };

function withAuth(over: Partial<AuthContextValue>): void {
  vi.mocked(useAuth).mockReturnValue({ ...unauthenticatedAuthMock, ...over });
}

describe('LoginPage — redirect providers (#341)', () => {
  beforeEach(() => {
    server.resetHandlers();
    search = '';
    sessionStorage.clear();
    localStorage.clear();
    withAuth({});
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('without providers: no buttons, no note, the form is shown', () => {
    renderWithProviders(<LoginPage />);
    expect(screen.queryByRole('button', { name: /mit firmenkonto anmelden/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/meldest du dich nur bei/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/benutzername/i)).toBeInTheDocument();
  });

  it('a provider button starts the login and leaves for the IdP', async () => {
    withAuth({ ssoProviders: [keycloak] });
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE_URL}/api/auth/sso/start`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ authorize_url: 'https://idp.example/auth?x=1' });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<LoginPage />);

    await user.click(screen.getByRole('button', { name: /mit firmenkonto anmelden/i }));

    await waitFor(() => expect(leavePage).toHaveBeenCalledWith('https://idp.example/auth?x=1'));
    expect(body).toMatchObject({ provider: 'keycloak' });
    expect(typeof (body as unknown as Record<string, unknown>).code_challenge).toBe('string');
    expect(sessionStorage.getItem('renfield_sso_pending_provider')).toBe('keycloak');
  });

  it('a refused start shows an error and stays on the page', async () => {
    withAuth({ ssoProviders: [keycloak] });
    server.use(
      http.post(`${BASE_URL}/api/auth/sso/start`, () =>
        HttpResponse.json({ detail: 'Not found' }, { status: 404 })),
    );
    const user = userEvent.setup();
    renderWithProviders(<LoginPage />);

    await user.click(screen.getByRole('button', { name: /mit firmenkonto anmelden/i }));

    expect(await screen.findByText(/konnte nicht gestartet werden/i)).toBeInTheDocument();
    expect(leavePage).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('renfield_sso_pending_provider')).toBeNull();
    expect(screen.getByRole('button', { name: /mit firmenkonto anmelden/i })).toBeEnabled();
  });

  it('with providers the password form carries the D3 note', () => {
    withAuth({ ssoProviders: [keycloak] });
    renderWithProviders(<LoginPage />);
    expect(screen.getByText(/meldest du dich nur bei renfield an/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/benutzername/i)).toBeInTheDocument();
  });

  it('collapsed: the form sits behind a link that opens it', async () => {
    withAuth({ ssoProviders: [keycloak], passwordForm: 'collapsed' });
    const user = userEvent.setup();
    renderWithProviders(<LoginPage />);

    expect(screen.queryByLabelText(/benutzername/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /ohne firmenkonto anmelden/i }));
    expect(screen.getByLabelText(/benutzername/i)).toBeInTheDocument();
  });

  const follows = (a: Node, b: Node): boolean =>
    Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

  it('visible form: the provider button sits below the form, directly above Microsoft', () => {
    vi.stubEnv('VITE_OIDC_ENABLED', 'true');
    try {
      withAuth({ ssoProviders: [keycloak] });
      renderWithProviders(<LoginPage />);
      const submit = screen.getByRole('button', { name: /^anmelden$/i });
      const provider = screen.getByRole('button', { name: /mit firmenkonto anmelden/i });
      const microsoft = screen.getByRole('link', { name: /mit sso anmelden/i });
      expect(follows(submit, provider)).toBe(true);
      expect(follows(provider, microsoft)).toBe(true);
      // One "oder" divider for the whole group, not one per button.
      expect(screen.getAllByText(/^oder$/i)).toHaveLength(1);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it('collapsed form: the provider button leads, the link follows it', () => {
    withAuth({ ssoProviders: [keycloak], passwordForm: 'collapsed' });
    renderWithProviders(<LoginPage />);
    const provider = screen.getByRole('button', { name: /mit firmenkonto anmelden/i });
    const link = screen.getByRole('button', { name: /ohne firmenkonto anmelden/i });
    expect(follows(provider, link)).toBe(true);
  });

  it('a failed callback (?error=sso) shows a notice', () => {
    search = '?error=sso';
    withAuth({ ssoProviders: [keycloak] });
    renderWithProviders(<LoginPage />);
    expect(screen.getByText(/anmeldung fehlgeschlagen/i)).toBeInTheDocument();
  });
});
