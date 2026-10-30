/**
 * "Mein Konto" -- the consent belongs to the person.
 *
 * 🛑 Until 2026-09-28 `require_permission(USERS_MANAGE)` stood in front of both
 * directions of the route, and the only interface for it was the user
 * administration. So a household member could neither consent nor see whether
 * the factor applies to their account -- the wrong distribution of ownership
 * for a consent under Art. 9 GDPR.
 */
import { createElement, Fragment } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import AccountSettingsPage from '../../../../src/frontend/src/pages/AccountSettingsPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { adminAuthMock } from '../test-auth-mock';
import type { UseConfirmDialogResult } from '../../../../src/frontend/src/components/ConfirmDialog';

vi.mock('../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/context/AuthContext')>(
    '../../../../src/frontend/src/context/AuthContext',
  );
  return { ...actual, useAuth: vi.fn<() => AuthContextValue>() };
});

vi.mock('../../../../src/frontend/src/components/ConfirmDialog', () => {
  const result: UseConfirmDialogResult = {
    confirm: () => Promise.resolve(true),
    ConfirmDialogComponent: createElement(Fragment),
  };
  return { useConfirmDialog: (): UseConfirmDialogResult => result };
});

/**
 * Since 2026-09-29 the BLOCKER comes from the server, not from the feature
 * flags.
 *
 * 🛑 Before that the page read only the instance-wide half (the flags) and
 * showed a blocker only in the "armed" state as well. Anyone who had not
 * consented yet saw a blank "off" -- measured in the household on 6 of 7
 * accounts, because their blocker was `no_profile`, the account-bound half.
 */
function state(blocker: string | null = null, enabled = false) {
  server.use(
    http.get(`${BASE_URL}/api/users/:id/voice-second-factor`, () =>
      HttpResponse.json({ enabled, blocker }),
    ),
  );
}

function asUser(armed: boolean, fetchUser = vi.fn()) {
  vi.mocked(useAuth).mockReturnValue({
    ...adminAuthMock,
    user: { ...adminAuthMock.user!, voice_second_factor_enabled: armed },
    fetchUser,
  } as AuthContextValue);
}

function captureToggle() {
  const calls: Array<{ id: string; enabled: boolean; current_password?: string }> = [];
  server.use(
    http.post<{ id: string }, { enabled: boolean; current_password?: string | null }>(
      `${BASE_URL}/api/users/:id/voice-second-factor`,
      async ({ params, request }) => {
        const body = await request.json();
        calls.push(
          body.current_password
            ? { id: params.id, enabled: body.enabled, current_password: body.current_password }
            : { id: params.id, enabled: body.enabled },
        );
        return HttpResponse.json({ id: Number(params.id), username: 'x' });
      },
    ),
  );
  return calls;
}

describe('AccountSettingsPage', () => {
  beforeEach(() => { server.resetHandlers(); state(); });
  afterEach(() => vi.clearAllMocks());

  it('zeigt den eigenen Zustand — ohne Verwaltungsrecht', async () => {
    asUser(false);
    renderWithProviders(<AccountSettingsPage />);
    expect(await screen.findByText('Aus — Ihr Passwort genügt')).toBeInTheDocument();
  });

  it('die Person kann fuer sich selbst einwilligen', async () => {
    asUser(false);
    const calls = captureToggle();
    renderWithProviders(<AccountSettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Stimme verlangen' }));
    await waitFor(() => expect(calls).toEqual([{ id: '1', enabled: true }]));
  });

  it('🛑 verlangt zum Zuruecknehmen erst das Passwort', async () => {
    // The server refuses to switch off ONE'S OWN factor without a password
    // (400): a token from BEFORE the consent outlives it and would otherwise be
    // able to withdraw it. Without the field the button would run into the 400.
    asUser(true);
    const calls = captureToggle();
    renderWithProviders(<AccountSettingsPage />);

    await userEvent.click(await screen.findByRole('button', { name: 'Nicht mehr verlangen' }));
    // The first click only opens the field -- nothing sent yet.
    expect(calls).toEqual([]);
    const feld = await screen.findByLabelText('Passwort zur Bestätigung');
    expect(screen.getByRole('button', { name: 'Nicht mehr verlangen' })).toBeDisabled();

    await userEvent.type(feld, 'geheim');
    await userEvent.click(screen.getByRole('button', { name: 'Nicht mehr verlangen' }));
    await waitFor(() =>
      expect(calls).toEqual([{ id: '1', enabled: false, current_password: 'geheim' }]),
    );
  });

  it('🛑 liest `/auth/me` neu, statt der Antwort der Mutation zu glauben', async () => {
    // Otherwise the page shows something other than what the session believes
    // -- and this page's state IS the session state.
    const fetchUser = vi.fn().mockResolvedValue(null);
    asUser(false, fetchUser);
    captureToggle();
    renderWithProviders(<AccountSettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Stimme verlangen' }));
    await waitFor(() => expect(fetchUser).toHaveBeenCalled());
  });

  it('unterscheidet „scharf\" von „ruht\", wenn der Sprachweg aus ist', async () => {
    asUser(true);
    state('voice_path_off', true);
    renderWithProviders(<AccountSettingsPage />);
    expect(
      await screen.findByText('Eingewilligt, ruht aber (Sprachweg auf dieser Instanz aus)'),
    ).toBeInTheDocument();
  });

  it('🛑 nennt den Grund AUCH ohne Einwilligung', async () => {
    // The actual finding of 2026-09-29: in the "off" state the page stayed
    // completely silent, no matter how many preconditions were missing.
    asUser(false);
    state('no_profile');
    renderWithProviders(<AccountSettingsPage />);
    expect(await screen.findByText('Aus — Ihr Passwort genügt')).toBeInTheDocument();
    expect(await screen.findByText(/noch kein Sprecherprofil verknüpft/)).toBeInTheDocument();
  });

  it('🛑 bietet kein Einschalten an, das der Server ablehnen MUSS', async () => {
    asUser(false);
    state('no_profile');
    renderWithProviders(<AccountSettingsPage />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Stimme verlangen' })).toBeDisabled(),
    );
  });

  it('🛑 `voice_path_off` blockiert das Einschalten NICHT', async () => {
    // Otherwise the cutover order would not be workable: collect the consent
    // first, then flip the flag.
    asUser(false);
    state('voice_path_off');
    renderWithProviders(<AccountSettingsPage />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Stimme verlangen' })).toBeEnabled(),
    );
  });

  it('🛑 uebersetzt den 409-Code, statt ihn roh anzuzeigen', async () => {
    // Since 2026-09-29 the server sends the CODE. Without a translation
    // "no_embeddings" would stand in the interface -- before that there was a
    // hard-coded ENGLISH sentence.
    asUser(false);
    state(null);
    server.use(
      http.post(`${BASE_URL}/api/users/:id/voice-second-factor`, () =>
        HttpResponse.json({ detail: 'no_embeddings' }, { status: 409 }),
      ),
    );
    renderWithProviders(<AccountSettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Stimme verlangen' }));
    expect(await screen.findByText(/noch keine Stimmprobe/)).toBeInTheDocument();
    expect(screen.queryByText('no_embeddings')).not.toBeInTheDocument();
  });

  it('nennt bei scharfem Faktor, dass es keinen Rueckfall gibt', async () => {
    asUser(true);
    renderWithProviders(<AccountSettingsPage />);
    expect(await screen.findByText(/Rückfall auf das Passwort allein/)).toBeInTheDocument();
  });

  it('beim EINSCHALTEN wird kein Passwort verlangt', async () => {
    // Only removing it is the attack path. When granting it, the input would
    // be friction with no gain.
    asUser(false);
    const calls = captureToggle();
    renderWithProviders(<AccountSettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Stimme verlangen' }));
    await waitFor(() => expect(calls).toEqual([{ id: '1', enabled: true }]));
    expect(screen.queryByLabelText('Passwort zur Bestätigung')).not.toBeInTheDocument();
  });
});
