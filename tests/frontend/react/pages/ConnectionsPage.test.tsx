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
