/**
 * Switching the voice on as a second login factor -- from the user
 * administration.
 *
 * 🛑 This interface is not an accessory. `.claude/rules/auth.md` describes the
 * only way back from a broken microphone: "an administrator switches
 * `voice_second_factor_enabled` off". Without a button that way back would be a
 * claim, and the only means an UPDATE by hand against the production database.
 *
 * What is tested above all is the ASYMMETRY: switching on only for oneself (a
 * voiceprint is biometric data, Art. 9 GDPR -- nobody can consent on somebody
 * else's behalf), switching off for any account.
 */
import { createElement, Fragment } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import UsersPage from '../../../../src/frontend/src/pages/UsersPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { adminAuthMock } from '../test-auth-mock';
import type { ModalProps } from '../../../../src/frontend/src/components/Modal';
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

vi.mock('../../../../src/frontend/src/components/Modal', () => ({
  default: ({ isOpen, title, children }: ModalProps) =>
    isOpen ? <div data-testid="modal"><h2>{title}</h2>{children}</div> : null,
}));

interface Row {
  id: number;
  username: string;
  email: string | null;
  role_id: number;
  role_name: string;
  is_active: boolean;
  speaker_id: number | null;
  last_login: string | null;
  created_at: string;
  voice_second_factor_enabled?: boolean;
}

/** `adminAuthMock` signs in as `id: 1` -- that is "one's own account". */
const SELF = 1;

function listing(
  rows: Row[],
  speakers: Array<{ id: number; name: string; embedding_count: number }> = [
    { id: 7, name: 'stimme', embedding_count: 3 },
  ],
  flags: { voice_auth_enabled?: boolean; speaker_recognition_enabled?: boolean } = {},
) {
  server.use(
    http.get(`${BASE_URL}/api/users`, () =>
      HttpResponse.json({ users: rows, total: rows.length, page: 1, page_size: 50 }),
    ),
    http.get(`${BASE_URL}/api/speakers`, () => HttpResponse.json(speakers)),
    // The instance-wide half of the preconditions. Default: voice path ON, so
    // the other cases check the profile half.
    http.get(`${BASE_URL}/api/config/features`, () =>
      HttpResponse.json({
        voice_auth_enabled: true,
        speaker_recognition_enabled: true,
        ...flags,
      }),
    ),
  );
}

function row(over: Partial<Row> & { id: number; username: string }): Row {
  return {
    email: null,
    role_id: 2,
    role_name: 'User',
    is_active: true,
    speaker_id: 7,
    last_login: null,
    created_at: '2024-01-01T00:00:00Z',
    ...over,
  };
}

/** Intercepts the POST and returns what went to the server. */
function captureToggle(): { calls: Array<{ id: string; enabled: boolean }> } {
  const calls: Array<{ id: string; enabled: boolean }> = [];
  server.use(
    http.post<{ id: string }, { enabled: boolean }>(
      `${BASE_URL}/api/users/:id/voice-second-factor`,
      async ({ params, request }) => {
        const body = await request.json();
        calls.push({ id: params.id, enabled: body.enabled });
        return HttpResponse.json({ id: Number(params.id), username: 'x' });
      },
    ),
  );
  return { calls };
}

describe('UsersPage — Stimme als zweiter Faktor', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);
  });
  afterEach(() => vi.clearAllMocks());

  it('🛑 übersetzt die Gerätekonto-Absage, statt den Code roh zu zeigen', async () => {
    // The ONLY place `device_account` occurs: a device account signs in
    // nowhere itself, so the refusal is reachable only from here. In the
    // 2026-09-29 rework it was left standing as a hard-coded ENGLISH sentence
    // -- four lines from the three that were replaced.
    listing([row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1 })]);
    server.use(
      http.post(`${BASE_URL}/api/users/:id/voice-second-factor`, () =>
        HttpResponse.json({ detail: 'device_account' }, { status: 409 }),
      ),
    );

    renderWithProviders(<UsersPage />);
    await userEvent.click(await screen.findByTitle('Stimme als zweiten Faktor einschalten'));

    expect(await screen.findByText(/Gerätekonto, kein Mensch/)).toBeInTheDocument();
    expect(screen.queryByText('device_account')).not.toBeInTheDocument();
    expect(screen.queryByText(/device account has no voice/)).not.toBeInTheDocument();
  });

  it('bietet das Einschalten für das eigene Konto an und meldet es dem Server', async () => {
    listing([row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1 })]);
    const { calls } = captureToggle();

    renderWithProviders(<UsersPage />);
    const button = await screen.findByTitle('Stimme als zweiten Faktor einschalten');
    await userEvent.click(button);

    await waitFor(() => expect(calls).toEqual([{ id: String(SELF), enabled: true }]));
  });

  it('🛑 bietet es für ein FREMDES Konto gar nicht erst an', async () => {
    // This is the point: an administrator must not impose a biometric
    // capture. The refusal lives in the backend (403), but a button that only
    // produces a 403 would be a trap.
    listing([
      row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1 }),
      row({ id: 2, username: 'anna' }),
    ]);

    renderWithProviders(<UsersPage />);
    await screen.findByText('anna');
    expect(screen.getAllByTitle('Stimme als zweiten Faktor einschalten')).toHaveLength(1);
  });

  it('bietet das ABSCHALTEN auch für ein fremdes Konto an — der Rückweg', async () => {
    listing([
      row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1 }),
      row({ id: 2, username: 'anna', voice_second_factor_enabled: true }),
    ]);
    const { calls } = captureToggle();

    renderWithProviders(<UsersPage />);
    const button = await screen.findByTitle('Stimme als zweiten Faktor abschalten');
    await userEvent.click(button);

    await waitFor(() => expect(calls).toEqual([{ id: '2', enabled: false }]));
  });

  it('zeigt am fremden Konto an, dass der zweite Faktor scharf ist', async () => {
    listing([row({ id: 2, username: 'anna', voice_second_factor_enabled: true })]);
    renderWithProviders(<UsersPage />);
    expect(await screen.findByText('Stimme als 2. Faktor')).toBeInTheDocument();
  });

  it('sperrt das Einschalten, solange kein Sprecherprofil verknüpft ist', async () => {
    // Switching it on without a profile would lock oneself out:
    // `POST /auth/voice` checks 1:1 against it and refuses fail-closed when
    // there is none.
    listing([row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1, speaker_id: null })]);
    renderWithProviders(<UsersPage />);
    expect(await screen.findByTitle('Erst ein Sprecherprofil verknüpfen')).toBeDisabled();
  });

  it('🛑 sperrt das Einschalten auch bei einem Profil OHNE Stimmproben', async () => {
    // The backend refuses BOTH cases with a 409 -- the interface blocked only
    // the first. The second ran into the 409 and showed the English server
    // message in the German form.
    listing(
      [row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1, speaker_id: 7 })],
      [{ id: 7, name: 'leer', embedding_count: 0 }],
    );
    renderWithProviders(<UsersPage />);
    expect(
      await screen.findByTitle('Das verknüpfte Profil hat noch keine Stimmproben'),
    ).toBeDisabled();
  });

  it('sperrt NICHT, wenn die Sprecherliste gar nicht geladen werden konnte', async () => {
    // 🛑 `fetchSpeakers` swallows errors and returns `[]`. Concluding "no
    // embeddings" from an empty list would block every switch whenever
    // /api/speakers is dead -- the absence of a profile is not proof.
    listing([row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1, speaker_id: 7 })], []);
    renderWithProviders(<UsersPage />);
    expect(await screen.findByTitle('Stimme als zweiten Faktor einschalten')).toBeEnabled();
  });

  it('am eigenen Konto mit scharfem Faktor wird das ABSCHALTEN angeboten', async () => {
    // The own+on quadrant: otherwise the owner would have armed themselves and
    // seen no way back.
    listing([row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1,
                   voice_second_factor_enabled: true })]);
    const { calls } = captureToggle();
    renderWithProviders(<UsersPage />);
    await userEvent.click(await screen.findByTitle('Stimme als zweiten Faktor abschalten'));
    await waitFor(() => expect(calls).toEqual([{ id: String(SELF), enabled: false }]));
  });

  it('🛑 unterscheidet „scharf" von „ruht"', async () => {
    // If a precondition is missing, the login does NOT hold the tokens back:
    // the consent stands but has no effect. A badge that shows both the same
    // way would be a display that lies -- and an administrator would have
    // believed the account was protected.
    listing(
      [row({ id: 2, username: 'anna', speaker_id: null, voice_second_factor_enabled: true })],
      [],
    );
    renderWithProviders(<UsersPage />);
    expect(await screen.findByText('2. Faktor ruht (kein Stimmprofil)')).toBeInTheDocument();
    expect(screen.queryByText('Stimme als 2. Faktor')).not.toBeInTheDocument();
  });

  it('🛑 meldet „ruht", wenn der SPRACHWEG der Instanz aus ist', async () => {
    // The finding from the browser acceptance run of 2026-09-28: a consented
    // row showed "armed" while VOICE_AUTH_ENABLED=false left the hurdle
    // dormant. The form knew only the profile half of the preconditions.
    listing(
      [row({ id: 2, username: 'anna', voice_second_factor_enabled: true })],
      [{ id: 7, name: 'stimme', embedding_count: 3 }],
      { voice_auth_enabled: false },
    );
    renderWithProviders(<UsersPage />);
    expect(await screen.findByText('2. Faktor ruht (Sprachweg aus)')).toBeInTheDocument();
    expect(screen.queryByText('Stimme als 2. Faktor')).not.toBeInTheDocument();
  });

  it('meldet „ruht", wenn die SPRECHERKENNUNG aus ist', async () => {
    listing(
      [row({ id: 2, username: 'anna', voice_second_factor_enabled: true })],
      [{ id: 7, name: 'stimme', embedding_count: 3 }],
      { speaker_recognition_enabled: false },
    );
    renderWithProviders(<UsersPage />);
    expect(await screen.findByText('2. Faktor ruht (Sprachweg aus)')).toBeInTheDocument();
  });

  it('🛑 sperrt das EINSCHALTEN NICHT, nur weil der Sprachweg aus ist', async () => {
    // The counter-check against the regression I nearly built into the fix.
    // The backend deliberately leaves `voice_auth_enabled` out of the 409: the
    // cutover order is to collect the consents first, THEN flip the flag. If
    // the form blocked here, exactly that would be impossible.
    listing(
      [row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1 })],
      [{ id: 7, name: 'stimme', embedding_count: 3 }],
      { voice_auth_enabled: false },
    );
    const { calls } = captureToggle();

    renderWithProviders(<UsersPage />);
    const button = await screen.findByTitle('Stimme als zweiten Faktor einschalten');
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(calls).toEqual([{ id: String(SELF), enabled: true }]));
  });

  it('sperrt das Einschalten SEHR WOHL, wenn die Sprecherkennung aus ist', async () => {
    // The other half: the backend refuses THIS case with a 409, so the form
    // must not let it run into the 409.
    listing(
      [row({ id: SELF, username: 'admin', role_name: 'Admin', role_id: 1 })],
      [{ id: 7, name: 'stimme', embedding_count: 3 }],
      { speaker_recognition_enabled: false },
    );
    renderWithProviders(<UsersPage />);
    expect(
      await screen.findByTitle('Sprecherkennung ist auf dieser Instanz aus'),
    ).toBeDisabled();
  });
});
