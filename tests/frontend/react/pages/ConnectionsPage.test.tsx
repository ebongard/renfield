/**
 * ConnectionsPage — the paste-token vs SSO distinction.
 *
 * The catalog marks a provider `credential_type: "sso"` when its credential is
 * minted from the user's own login. The server refuses connect/disconnect for
 * those. Before this, the page ignored the field, so an SSO provider offered a
 * "Connect" button, a paste box, and a "Disconnect" — three actions whose only
 * possible outcome was a 400. These tests pin the distinction down.
 *
 * Assertions are in German: test-utils pins i18n to `de`, matching the
 * production default.
 */
import { describe, it, expect } from 'vitest';
import { http, HttpResponse } from 'msw';
import { fireEvent, screen, waitFor } from '@testing-library/react';

import { renderWithProviders } from '../test-utils';
import { server } from '../mocks/server';
import ConnectionsPage from '../../../../src/frontend/src/pages/ConnectionsPage';
import {
  credentialHealth,
  EXPIRY_WARN_DAYS,
} from '../../../../src/frontend/src/api/resources/connections';
import { TEST_CONFIG } from '../config';

const BASE = TEST_CONFIG.API_BASE_URL;

const PASTE_PROVIDER = {
  provider_key: 'jira',
  display_name: 'Jira',
  descriptor: 'Issues, boards & releases',
  credential_type: 'paste_token',
  read_only: true,
  mint_url: 'https://id.atlassian.com/manage-profile/security/api-tokens',
  help: 'Create an Atlassian API token.',
  connected: false,
};

const SSO_PROVIDER = {
  provider_key: 'release',
  display_name: 'Digital.ai Release',
  descriptor: 'Releases, phases & deployment status',
  credential_type: 'sso',
  sso_source: 'keycloak',
  read_only: false,
  help: 'Connected automatically through your SSO login — no token needed.',
  connected: false,
};

function serve(providers: unknown[]) {
  server.use(http.get(`${BASE}/api/connections`, () => HttpResponse.json(providers)));
}

describe('ConnectionsPage — SSO providers', () => {
  it('offers Details instead of Connect for an SSO provider', async () => {
    serve([SSO_PROVIDER]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Digital.ai Release');
    expect(screen.getByRole('button', { name: /details/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^verbinden$/i })).not.toBeInTheDocument();
  });

  it('shows no paste box — there is no token to paste', async () => {
    serve([SSO_PROVIDER]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Digital.ai Release');
    fireEvent.click(screen.getByRole('button', { name: /details/i }));

    await screen.findByText(/über SSO/i);
    expect(screen.queryByLabelText(/zugriffstoken/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /verbindung speichern/i })).not.toBeInTheDocument();
  });

  it('offers no Disconnect for a connected SSO provider', async () => {
    serve([{ ...SSO_PROVIDER, connected: true }]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Digital.ai Release');
    fireEvent.click(screen.getByRole('button', { name: /details/i }));

    await screen.findByText(/über SSO/i);
    expect(screen.queryByRole('button', { name: /^trennen$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /token ersetzen/i })).not.toBeInTheDocument();
  });

  it('names the remedy when an SSO connection is not established', async () => {
    // "Not connected" with no Connect button is a dead end unless the page
    // says what to do instead — sign in again.
    serve([SSO_PROVIDER]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Digital.ai Release');
    fireEvent.click(screen.getByRole('button', { name: /details/i }));

    expect(await screen.findByText(/neu an/i)).toBeInTheDocument();
  });

  it('marks the row so the mechanism is visible before opening it', async () => {
    serve([SSO_PROVIDER]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText(/über deine SSO-Anmeldung/i)).toBeInTheDocument();
  });
});

describe('ConnectionsPage — paste-token providers are unchanged', () => {
  it('still offers Connect and a paste box', async () => {
    serve([PASTE_PROVIDER]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Jira');
    fireEvent.click(screen.getByRole('button', { name: /^verbinden$/i }));

    expect(await screen.findByLabelText(/zugriffstoken/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /verbindung speichern/i })).toBeInTheDocument();
  });

  it('still offers Disconnect once connected', async () => {
    serve([{ ...PASTE_PROVIDER, connected: true }]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Jira');
    fireEvent.click(screen.getByRole('button', { name: /verwalten/i }));

    expect(await screen.findByRole('button', { name: /^trennen$/i })).toBeInTheDocument();
  });

  it('submits the pasted secret to the provider endpoint', async () => {
    let sent: { key?: string; body?: unknown } = {};
    serve([PASTE_PROVIDER]);
    server.use(
      http.put(`${BASE}/api/connections/:key`, async ({ params, request }) => {
        sent = { key: params.key as string, body: await request.json() };
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Jira');
    fireEvent.click(screen.getByRole('button', { name: /^verbinden$/i }));
    fireEvent.change(await screen.findByLabelText(/zugriffstoken/i), {
      target: { value: 'me@example.com:tok' },
    });
    fireEvent.click(screen.getByRole('button', { name: /verbindung speichern/i }));

    await waitFor(() => expect(sent.key).toBe('jira'));
    expect(sent.body).toEqual({ secret: 'me@example.com:tok' });
  });
});

describe('ConnectionsPage — mixed catalog', () => {
  it('renders each provider according to its own credential type', async () => {
    serve([PASTE_PROVIDER, SSO_PROVIDER]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Jira');
    await screen.findByText('Digital.ai Release');
    expect(screen.getByRole('button', { name: /^verbinden$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /details/i })).toBeInTheDocument();
  });
});

describe('ConnectionsPage — stored-token health (reva#345)', () => {
  const KFG = {
    provider_key: 'konfigure',
    display_name: 'konfigure',
    credential_type: 'paste_token',
    connected: true,
  };
  const inDays = (d: number) => new Date(Date.now() + d * 86_400_000).toISOString();

  it('marks a rejected token without SSO as rejected, not connected', async () => {
    serve([{ ...KFG, check_state: 'rejected', sso_active: false }]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText('Token abgelehnt')).toBeInTheDocument();
    expect(screen.queryByText('Verbunden')).not.toBeInTheDocument();
    expect(screen.getByText(/abgelaufen oder widerrufen/i)).toBeInTheDocument();
  });

  it('explains a rejected fallback token when the tool runs on SSO', async () => {
    // The case that motivated the field: konfigure keeps answering via OBO
    // while the stored PAT is expired — without this it reads as a contradiction.
    serve([{ ...KFG, check_state: 'rejected', sso_active: true }]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText(/läuft über deine SSO-Anmeldung/i)).toBeInTheDocument();
    expect(screen.getByText('Verbunden')).toBeInTheDocument();
    expect(screen.queryByText('Token abgelehnt')).not.toBeInTheDocument();
  });

  it('warns before a valid token expires, also in the manage dialog', async () => {
    serve([{ ...KFG, check_state: 'valid', expires_at: inDays(5) }]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText(/läuft am .* ab/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /verwalten/i }));
    await waitFor(() => expect(screen.getAllByText(/läuft am .* ab/i)).toHaveLength(2));
  });

  it('treats a token past its expiry date as rejected', async () => {
    serve([{ ...KFG, check_state: 'valid', expires_at: inDays(-1) }]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText('Token abgelehnt')).toBeInTheDocument();
  });

  it('shows a far expiry date neutrally and says nothing without a verdict', async () => {
    serve([
      { ...KFG, check_state: 'valid', expires_at: inDays(60) },
      { ...PASTE_PROVIDER, connected: true },
    ]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText(/token gültig bis/i)).toBeInTheDocument();
    expect(screen.getAllByText('Verbunden')).toHaveLength(2);
    expect(screen.queryByText(/abgelehnt|läuft am/i)).not.toBeInTheDocument();
  });

  it('does not call a provider error a rejection', async () => {
    serve([{ ...KFG, check_state: 'error' }]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText(/nicht geprüft werden/i)).toBeInTheDocument();
    expect(screen.getByText('Verbunden')).toBeInTheDocument();
  });
});

describe('credentialHealth — verdict rules', () => {
  const NOW = new Date('2026-11-19T12:00:00Z');
  const base = { provider_key: 'gitlab', credential_type: 'paste_token', connected: true } as const;
  const at = (ms: number) => new Date(NOW.getTime() + ms).toISOString();

  it('treats malformed like rejected', () => {
    expect(credentialHealth({ ...base, check_state: 'malformed' }, NOW)).toBe('rejected');
  });

  it('counts the exact expiry instant as expired', () => {
    expect(credentialHealth({ ...base, check_state: 'valid', expires_at: at(0) }, NOW)).toBe('rejected');
    expect(credentialHealth({ ...base, check_state: 'valid', expires_at: at(1) }, NOW)).toBe('expiring');
  });

  it('starts warning exactly at the warn window', () => {
    const day = 86_400_000;
    expect(credentialHealth({ ...base, check_state: 'valid', expires_at: at(EXPIRY_WARN_DAYS * day) }, NOW)).toBe('expiring');
    expect(credentialHealth({ ...base, check_state: 'valid', expires_at: at(EXPIRY_WARN_DAYS * day + 1) }, NOW)).toBeNull();
  });

  it('does not let a provider error hide a known expiry', () => {
    expect(credentialHealth({ ...base, check_state: 'error', expires_at: at(-1) }, NOW)).toBe('rejected');
    expect(credentialHealth({ ...base, check_state: 'error', expires_at: at(86_400_000) }, NOW)).toBe('expiring');
    expect(credentialHealth({ ...base, check_state: 'error' }, NOW)).toBe('unchecked');
  });

  it('ignores an unparseable expiry and says nothing for unchecked or SSO rows', () => {
    expect(credentialHealth({ ...base, check_state: 'valid', expires_at: 'garbage' }, NOW)).toBeNull();
    expect(credentialHealth({ ...base }, NOW)).toBeNull();
    expect(credentialHealth({ ...base, credential_type: 'sso', check_state: 'rejected' }, NOW)).toBeNull();
    expect(credentialHealth({ ...base, connected: false, check_state: 'rejected' }, NOW)).toBeNull();
  });
});

describe('ConnectionsPage — stored-token edge cases', () => {
  it('never renders "Invalid Date" for an unparseable expiry', async () => {
    serve([{ ...PASTE_PROVIDER, connected: true, check_state: 'valid', expires_at: 'garbage' }]);
    renderWithProviders(<ConnectionsPage />);

    await screen.findByText('Jira');
    expect(screen.queryByText(/invalid date/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/gültig bis/i)).not.toBeInTheDocument();
  });

  it('explains an expired-by-date fallback token when SSO is active', async () => {
    serve([{
      ...PASTE_PROVIDER, connected: true, check_state: 'valid', sso_active: true,
      expires_at: new Date(Date.now() - 86_400_000).toISOString(),
    }]);
    renderWithProviders(<ConnectionsPage />);

    expect(await screen.findByText(/läuft über deine SSO-Anmeldung/i)).toBeInTheDocument();
    expect(screen.getByText('Verbunden')).toBeInTheDocument();
  });
});
