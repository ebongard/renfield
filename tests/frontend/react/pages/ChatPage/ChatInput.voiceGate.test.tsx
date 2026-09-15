/**
 * ChatInput mic button is gated on the `voice` feature (FEATURE_VOICE).
 *
 * The wake-word controls in ChatHeader were already gated; the composer's mic
 * was not, so an instance with voice off (xidra today) still offered a mic that
 * opens a voice-server socket the instance has no route to.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import { BASE_URL } from '../../mocks/handlers';
import ChatPage from '../../../../../src/frontend/src/pages/ChatPage';
import { renderWithProviders } from '../../test-utils';
import type { AuthContextValue } from '../../../../../src/frontend/src/context/AuthContext';
import type { UseWakeWordResult } from '../../../../../src/frontend/src/hooks/useWakeWord';
import { adminAuthMock } from '../../test-auth-mock';

const authState: { voiceEnabled: boolean } = { voiceEnabled: true };

vi.mock('../../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../../src/frontend/src/context/AuthContext')>(
    '../../../../../src/frontend/src/context/AuthContext',
  );
  return {
    ...actual,
    useAuth: (): AuthContextValue => ({
      ...adminAuthMock,
      features: { voice: authState.voiceEnabled },
      isFeatureEnabled: (feature: string) => (feature === 'voice' ? authState.voiceEnabled : true),
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

describe('ChatInput voice gate', () => {
  beforeEach(() => {
    server.use(
      http.get(`${BASE_URL}/api/knowledge/bases`, () => HttpResponse.json([])),
    );
  });

  it('shows the mic button when the voice feature is on', () => {
    authState.voiceEnabled = true;
    renderWithProviders(<ChatPage />);
    expect(screen.getByRole('button', { name: 'Sprachaufnahme starten' })).toBeInTheDocument();
  });

  it('hides the mic button when the voice feature is off', () => {
    authState.voiceEnabled = false;
    renderWithProviders(<ChatPage />);
    expect(screen.queryByRole('button', { name: 'Sprachaufnahme starten' })).not.toBeInTheDocument();
    // The rest of the composer is still there.
    expect(screen.getByPlaceholderText(/nachricht eingeben/i)).toBeInTheDocument();
  });
});
