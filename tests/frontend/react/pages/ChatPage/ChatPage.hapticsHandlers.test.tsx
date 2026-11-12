/**
 * Two ChatContext handlers no button reaches directly (#84):
 *
 *  - a VOICE turn sends itself (`sendMessageInternal(text, true)`) and must not
 *    buzz the device the person is speaking into;
 *  - a failed quick action (the result toast under the messages) buzzes with
 *    the error pattern, a successful one does not.
 *
 * The real ChatPage and ChatContext run. Only the two hooks at the edge are
 * replaced: the recorder (to hand over a transcript without a microphone) and
 * the quick-action result (to produce an outcome without an upload flow).
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

type HooksModule = typeof import('../../../../../src/frontend/src/pages/ChatPage/hooks');
type RecorderOptions = NonNullable<Parameters<HooksModule['useAudioRecording']>[0]>;
type QuickActionResult = ReturnType<HooksModule['useQuickActions']>['actionResult'];

const recorder: { options: RecorderOptions } = { options: {} };
const quickAction: { result: QuickActionResult } = { result: null };

vi.mock('../../../../../src/frontend/src/pages/ChatPage/hooks', async () => {
  const actual = await vi.importActual<HooksModule>(
    '../../../../../src/frontend/src/pages/ChatPage/hooks',
  );
  const useAudioRecording: HooksModule['useAudioRecording'] = (options = {}) => {
    recorder.options = options;
    return {
      recording: false,
      audioLevel: 0,
      silenceTimeRemaining: 0,
      processing: false,
      startRecording: async () => {},
      stopRecording: () => {},
      toggleRecording: () => {},
    };
  };
  const useQuickActions: HooksModule['useQuickActions'] = () => ({
    ...actual.useQuickActions(),
    actionResult: quickAction.result,
  });
  return { ...actual, useAudioRecording, useQuickActions };
});

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

vi.mock('../../../../../src/frontend/src/hooks/useWakeWord', () => ({
  useWakeWord: (): UseWakeWordResult => wakeWordMock,
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

const TRANSCRIPT = 'Wie wird das Wetter am Donnerstag';

describe('ChatPage haptics — voice turn and quick-action result', () => {
  beforeEach(() => {
    quickAction.result = null;
    server.use(
      http.get(`${BASE_URL}/api/knowledge/bases`, () => HttpResponse.json([])),
    );
  });

  afterEach(resetHapticsEnvironment);

  it('a transcribed voice turn is sent without a vibration, although haptics are on', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<ChatPage />);

    act(() => recorder.options.onTranscription?.(TRANSCRIPT));

    // The turn really went through the send path: its bubble is in the chat.
    await waitFor(() => expect(screen.getAllByText(TRANSCRIPT).length).toBeGreaterThan(0));
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('control: the same harness does vibrate for a typed send', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<ChatPage />);
    await userEvent.type(screen.getByPlaceholderText(/nachricht eingeben/i), 'Hallo{Enter}');
    expect(vibrate.mock.calls).toEqual([[10]]);
  });

  it('a failed quick action buzzes with the error pattern', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    quickAction.result = { type: 'paperless', success: false, message: 'boom' };
    renderWithProviders(<ChatPage />);
    expect(screen.getByText('Senden an Paperless fehlgeschlagen')).toBeInTheDocument();
    expect(vibrate.mock.calls).toEqual([[200]]);
  });

  it('a successful quick action does not buzz', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    quickAction.result = { type: 'paperless', success: true, message: 'ok' };
    renderWithProviders(<ChatPage />);
    expect(screen.getByText('An Paperless gesendet')).toBeInTheDocument();
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('a failed quick action stays silent with the preference off', () => {
    const vibrate = installVibrate();
    quickAction.result = { type: 'paperless', success: false, message: 'boom' };
    renderWithProviders(<ChatPage />);
    expect(screen.getByText('Senden an Paperless fehlgeschlagen')).toBeInTheDocument();
    expect(vibrate).not.toHaveBeenCalled();
  });
});
