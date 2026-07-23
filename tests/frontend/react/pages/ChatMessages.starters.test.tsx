/**
 * Chat starter prompts — instance-dependent via runtime config.
 *
 * The empty chat page shows starter prompts. Household defaults (weather/light/
 * music) don't fit a business instance, so the backend serves per-instance
 * `chat_starters` via /api/config/features (runtime, so ONE shared frontend image
 * differs per instance). Resolution order: runtime config → build-time
 * VITE_CHAT_STARTERS → household i18n defaults.
 *
 * Renders ChatMessages with an EMPTY messages array (the starter state) and a
 * stubbed ChatContext, driving chat_starters via an MSW override.
 */
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

import ChatMessages from '../../../../src/frontend/src/pages/ChatPage/ChatMessages';
import { renderWithRouter } from '../test-utils';
import {
  useChatContext,
  type ChatContextValue,
} from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import { buildChatContextValue } from '../test-chat-mock';
import { server } from '../mocks/server';
import i18n from '../../../../src/frontend/src/i18n';

const BASE_URL = 'http://localhost:8000';

vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual<
    typeof import('../../../../src/frontend/src/pages/ChatPage/context/ChatContext')
  >('../../../../src/frontend/src/pages/ChatPage/context/ChatContext');
  return { ...actual, useChatContext: vi.fn<() => ChatContextValue>() };
});

function featuresWith(chat_starters: string[]): void {
  server.use(
    http.get(`${BASE_URL}/api/config/features`, () =>
      HttpResponse.json({
        schicht_a_extraction_enabled: false,
        wissen_workspace_enabled: false,
        chat_starters,
        command_palette_enabled: false,
        role_surfacing_enabled: false,
        message_search_enabled: false,
        artifacts_typed_enabled: false,
        room_handoff_enabled: false,
        chat_branching_enabled: false,
        projects_enabled: false,
        notes_enabled: false,
        meeting_transcription_enabled: false,
        meeting_minutes_enabled: false,
      }),
    ),
  );
}

function emptyChat(): void {
  vi.mocked(useChatContext).mockReturnValue(buildChatContextValue({ messages: [] }));
}

afterEach(() => vi.clearAllMocks());

describe('ChatMessages — starter prompts', () => {
  it('renders instance-provided starters when chat_starters is set (business instance)', async () => {
    emptyChat();
    featuresWith(['Fasse die letzte Besprechung zusammen', 'Welche Fristen stehen an?']);
    renderWithRouter(<ChatMessages />);

    await waitFor(() =>
      expect(screen.getByText('Fasse die letzte Besprechung zusammen')).toBeInTheDocument(),
    );
    expect(screen.getByText('Welche Fristen stehen an?')).toBeInTheDocument();
    // The household defaults must NOT appear when the instance overrides them.
    expect(screen.queryByText(i18n.t('chat.exampleWeather'))).not.toBeInTheDocument();
  });

  it('falls back to the household i18n defaults when chat_starters is empty', async () => {
    emptyChat();
    featuresWith([]);
    renderWithRouter(<ChatMessages />);

    await waitFor(() =>
      expect(screen.getByText(i18n.t('chat.exampleWeather'))).toBeInTheDocument(),
    );
    expect(screen.getByText(i18n.t('chat.exampleLight'))).toBeInTheDocument();
    expect(screen.getByText(i18n.t('chat.exampleMusic'))).toBeInTheDocument();
  });
});
