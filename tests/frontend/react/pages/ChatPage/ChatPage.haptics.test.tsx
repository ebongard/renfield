/**
 * Haptics in the chat, end to end through the real ChatPage (#84): sending a
 * message taps, the mic button taps, a detected wake word answers with the
 * success pattern -- and none of it happens with the preference off.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import { BASE_URL } from '../../mocks/handlers';
import ChatPage from '../../../../../src/frontend/src/pages/ChatPage';
import { renderWithProviders } from '../../test-utils';
import type { AuthContextValue } from '../../../../../src/frontend/src/context/AuthContext';
import type { UseWakeWordResult } from '../../../../../src/frontend/src/hooks/useWakeWord';
import { adminAuthMock } from '../../test-auth-mock';
import {
  installVibrate,
  resetHapticsEnvironment,
  setHapticsPreference,
} from '../../haptics-test-helpers';

vi.mock('../../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../../src/frontend/src/context/AuthContext')>(
    '../../../../../src/frontend/src/context/AuthContext',
  );
  return {
    ...actual,
    useAuth: (): AuthContextValue => ({
      ...adminAuthMock,
      features: { voice: true },
      isFeatureEnabled: () => true,
    }),
  };
});

const wakeWordMock: UseWakeWordResult = {
  isEnabled: false,
  isListening: false,
  isLoading: false,
  isReady: false,
  isAvailable: false,
  lastDetection: null,
  error: null,
  settings: { enabled: false, keyword: 'hey_jarvis', threshold: 0.5, audioFeedback: false },
  enable: async () => {},
  disable: async () => {},
  toggle: async () => {},
  pause: async () => {},
  resume: async () => {},
  setKeyword: async () => {},
  toggleKeyword: async () => {},
  setThreshold: () => {},
  availableKeywords: [{ id: 'hey_jarvis', label: 'Hey Jarvis', model: 'hey_jarvis_v0.1', description: 'pre-trained' }],
};

// The hook is replaced, but the callbacks ChatContext hands it are kept: that
// is how a test "detects" a wake word.
type WakeWordOptions = { onWakeWordDetected?: (keyword: string, score: number) => void };
const wakeWordOptions: { current: WakeWordOptions } = { current: {} };

vi.mock('../../../../../src/frontend/src/hooks/useWakeWord', () => ({
  useWakeWord: (options: WakeWordOptions = {}): UseWakeWordResult => {
    wakeWordOptions.current = options;
    return wakeWordMock;
  },
}));

// A socket that opens and swallows what it is sent.
class MockWebSocket {
  static OPEN = 1;
  readyState = 1;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  constructor(_url: string | URL) {
    setTimeout(() => this.onopen?.(new Event('open')), 0);
  }
  send(_data: string): void {}
  close(): void {}
}
(globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket =
  MockWebSocket as unknown as typeof WebSocket;

const COMPOSER = /nachricht eingeben/i;

describe('ChatPage haptics', () => {
  beforeEach(() => {
    server.use(
      http.get(`${BASE_URL}/api/knowledge/bases`, () => HttpResponse.json([])),
    );
  });

  afterEach(resetHapticsEnvironment);

  it('sending with Enter taps when the preference is on', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<ChatPage />);
    await userEvent.type(screen.getByPlaceholderText(COMPOSER), 'Hallo{Enter}');
    expect(vibrate.mock.calls).toEqual([[10]]);
  });

  it('sending with the send button taps when the preference is on', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<ChatPage />);
    await userEvent.type(screen.getByPlaceholderText(COMPOSER), 'Hallo');
    await userEvent.click(screen.getByRole('button', { name: 'Nachricht senden' }));
    expect(vibrate.mock.calls).toEqual([[10]]);
  });

  it('sending does not vibrate when the preference is off (the default)', async () => {
    const vibrate = installVibrate();
    renderWithProviders(<ChatPage />);
    const composer = screen.getByPlaceholderText(COMPOSER);
    await userEvent.type(composer, 'Hallo{Enter}');
    // The message did go out (the composer is cleared) -- only the vibration is missing.
    await waitFor(() => expect(composer).toHaveValue(''));
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('Enter on an empty composer sends nothing and does not vibrate', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<ChatPage />);
    await userEvent.type(screen.getByPlaceholderText(COMPOSER), '{Enter}');
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('the mic button taps', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<ChatPage />);
    await userEvent.click(screen.getByRole('button', { name: 'Sprachaufnahme starten' }));
    expect(vibrate.mock.calls[0]).toEqual([10]);
  });

  it('the mic button stays silent with the preference off', async () => {
    const vibrate = installVibrate();
    renderWithProviders(<ChatPage />);
    await userEvent.click(screen.getByRole('button', { name: 'Sprachaufnahme starten' }));
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('a detected wake word answers with the success pattern', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<ChatPage />);
    await act(async () => {
      await wakeWordOptions.current.onWakeWordDetected?.('hey_jarvis', 0.9);
    });
    expect(vibrate.mock.calls[0]).toEqual([[10, 50, 10]]);
  });

  it('a detected wake word stays silent with the preference off', async () => {
    const vibrate = installVibrate();
    renderWithProviders(<ChatPage />);
    await act(async () => {
      await wakeWordOptions.current.onWakeWordDetected?.('hey_jarvis', 0.9);
    });
    expect(vibrate).not.toHaveBeenCalled();
  });
});
