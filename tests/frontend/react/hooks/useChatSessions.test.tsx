import { describe, it, expect, beforeEach } from 'vitest';
import {
  renderHook as rawRenderHook,
  waitFor,
  act,
  type RenderHookOptions,
  type RenderHookResult,
} from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import type { ReactNode } from 'react';

import { server } from '../mocks/server';
import { BASE_URL, mockConversations, mockConversationHistory } from '../mocks/handlers';
import {
  useChatSessions,
  groupConversationsByDate,
} from '../../../../src/frontend/src/hooks/useChatSessions';
import { createTestQueryClient } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';
import type { ChatSessionsResult, Conversation } from '../../../../src/frontend/src/types/chat';

interface RenderHookExtraOptions {
  queryClient?: QueryClient;
}

// Wrap renderHook with QueryClientProvider so the new RQ-backed hook works.
function renderHook<TResult, TProps>(
  hook: (props: TProps) => TResult,
  options: RenderHookOptions<TProps> & RenderHookExtraOptions = {} as RenderHookOptions<TProps> &
    RenderHookExtraOptions,
): RenderHookResult<TResult, TProps> {
  const { queryClient: overrideClient, ...rest } = options;
  const queryClient = overrideClient ?? createTestQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </I18nextProvider>
  );
  return rawRenderHook<TResult, TProps>(hook, { wrapper, ...rest });
}

describe('useChatSessions', () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  describe('Initialization', () => {
    it('fetches conversations on mount', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      // Initially loading
      expect(result.current.loading).toBe(true);

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.conversations).toHaveLength(mockConversations.length);
      expect(result.current.error).toBeNull();
    });

    it('handles API error gracefully', async () => {
      server.use(
        http.get(`${BASE_URL}/api/chat/conversations`, () => {
          return new HttpResponse(null, { status: 500 });
        }),
      );

      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.conversations).toHaveLength(0);
      expect(result.current.error).not.toBeNull();
    });

    it('handles empty conversations list', async () => {
      server.use(
        http.get(`${BASE_URL}/api/chat/conversations`, () => {
          return HttpResponse.json({ conversations: [], total: 0 });
        }),
      );

      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.conversations).toHaveLength(0);
      expect(result.current.error).toBeNull();
    });
  });

  describe('refreshConversations', () => {
    it('refreshes the conversation list', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const initialLength = result.current.conversations.length;

      // Mock a new conversation being added
      server.use(
        http.get(`${BASE_URL}/api/chat/conversations`, () => {
          return HttpResponse.json({
            conversations: [
              ...mockConversations,
              {
                session_id: 'new-session',
                preview: 'New conversation',
                message_count: 1,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
              },
            ],
            total: mockConversations.length + 1,
          });
        }),
      );

      await act(async () => {
        await result.current.refreshConversations();
      });

      await waitFor(() => {
        expect(result.current.conversations.length).toBe(initialLength + 1);
      });
    });
  });

  describe('deleteConversation', () => {
    it('deletes a conversation and updates local state', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const sessionToDelete: string = mockConversations[0].session_id;
      const initialLength = result.current.conversations.length;

      await act(async () => {
        const success = await result.current.deleteConversation(sessionToDelete);
        expect(success).toBe(true);
      });

      await waitFor(() => {
        expect(result.current.conversations.length).toBe(initialLength - 1);
      });
      expect(
        result.current.conversations.find((c: Conversation) => c.session_id === sessionToDelete),
      ).toBeUndefined();
    });

    it('handles delete error gracefully', async () => {
      server.use(
        http.delete(`${BASE_URL}/api/chat/session/:sessionId`, () => {
          return new HttpResponse(null, { status: 500 });
        }),
      );

      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const initialLength = result.current.conversations.length;

      await act(async () => {
        const success = await result.current.deleteConversation('some-session');
        expect(success).toBe(false);
      });

      // List should remain unchanged
      expect(result.current.conversations.length).toBe(initialLength);
    });
  });

  describe('loadConversationHistory', () => {
    it('loads conversation history for a session', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const history = await act(async () => {
        return result.current.loadConversationHistory('session-today-1');
      });

      expect(history).toHaveLength(mockConversationHistory['session-today-1'].length);
      expect(history[0].role).toBe('user');
      expect(history[0].content).toBe('Wie ist das Wetter heute?');
    });

    it('returns empty array for unknown session', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const history = await act(async () => {
        return result.current.loadConversationHistory('unknown-session');
      });

      expect(history).toHaveLength(0);
    });
  });

  describe('addConversation', () => {
    it('adds a new conversation to the list', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const initialLength = result.current.conversations.length;

      act(() => {
        result.current.addConversation({
          session_id: 'new-local-session',
          preview: 'New local conversation',
          message_count: 1,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        });
      });

      await waitFor(() => {
        expect(result.current.conversations.length).toBe(initialLength + 1);
      });
      expect(result.current.conversations[0].session_id).toBe('new-local-session');
    });

    it('does not add duplicate conversations', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const existingSessionId: string = mockConversations[0].session_id;
      const initialLength = result.current.conversations.length;

      act(() => {
        result.current.addConversation({
          session_id: existingSessionId,
          preview: 'Duplicate',
          message_count: 1,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        });
      });

      expect(result.current.conversations.length).toBe(initialLength);
    });
  });

  describe('updateConversationPreview', () => {
    it('updates the preview of an existing conversation', async () => {
      const { result } = renderHook<ChatSessionsResult, void>(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const sessionId: string = mockConversations[0].session_id;
      const newPreview = 'Updated preview text';

      act(() => {
        result.current.updateConversationPreview(sessionId, newPreview, 10);
      });

      await waitFor(() => {
        const updated = result.current.conversations.find(
          (c: Conversation) => c.session_id === sessionId,
        );
        expect(updated?.preview).toBe(newPreview);
        expect(updated?.message_count).toBe(10);
      });
    });
  });
});

describe('groupConversationsByDate', () => {
  it('groups conversations by date period', () => {
    const now = new Date();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const lastWeek = new Date(now);
    lastWeek.setDate(now.getDate() - 5);
    const older = new Date(now);
    older.setDate(now.getDate() - 14);

    const conversations: Conversation[] = [
      {
        session_id: '1',
        preview: '',
        message_count: 0,
        created_at: now.toISOString(),
        updated_at: now.toISOString(),
      },
      {
        session_id: '2',
        preview: '',
        message_count: 0,
        created_at: yesterday.toISOString(),
        updated_at: yesterday.toISOString(),
      },
      {
        session_id: '3',
        preview: '',
        message_count: 0,
        created_at: lastWeek.toISOString(),
        updated_at: lastWeek.toISOString(),
      },
      {
        session_id: '4',
        preview: '',
        message_count: 0,
        created_at: older.toISOString(),
        updated_at: older.toISOString(),
      },
    ];

    const grouped = groupConversationsByDate(conversations);

    expect(grouped.today).toHaveLength(1);
    expect(grouped.today[0].session_id).toBe('1');

    expect(grouped.yesterday).toHaveLength(1);
    expect(grouped.yesterday[0].session_id).toBe('2');

    expect(grouped.lastWeek).toHaveLength(1);
    expect(grouped.lastWeek[0].session_id).toBe('3');

    expect(grouped.older).toHaveLength(1);
    expect(grouped.older[0].session_id).toBe('4');
  });

  it('handles empty conversation list', () => {
    const grouped = groupConversationsByDate([]);

    expect(grouped.today).toHaveLength(0);
    expect(grouped.yesterday).toHaveLength(0);
    expect(grouped.lastWeek).toHaveLength(0);
    expect(grouped.older).toHaveLength(0);
  });

  it('uses created_at if updated_at is missing', () => {
    const now = new Date();
    // The runtime hook accepts conversations without `updated_at`. Mirror that
    // by widening the array element here while keeping the rest of the object
    // strongly typed.
    const conversations: Array<Omit<Conversation, 'updated_at'> & { updated_at?: string }> = [
      {
        session_id: '1',
        preview: '',
        message_count: 0,
        created_at: now.toISOString(),
      },
    ];

    const grouped = groupConversationsByDate(conversations as Conversation[]);

    expect(grouped.today).toHaveLength(1);
  });
});
