import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL, mockConversations, mockConversationHistory } from '../mocks/handlers.js';
import { useChatSessions, groupConversationsByDate } from '../../../../src/frontend/src/hooks/useChatSessions';

describe('useChatSessions', () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  describe('Initialization', () => {
    it('fetches conversations on mount', async () => {
      const { result } = renderHook(() => useChatSessions());

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
        })
      );

      const { result } = renderHook(() => useChatSessions());

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
        })
      );

      const { result } = renderHook(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.conversations).toHaveLength(0);
      expect(result.current.error).toBeNull();
    });
  });

  describe('refreshConversations', () => {
    it('refreshes the conversation list', async () => {
      const { result } = renderHook(() => useChatSessions());

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
                updated_at: new Date().toISOString()
              }
            ],
            total: mockConversations.length + 1
          });
        })
      );

      await act(async () => {
        await result.current.refreshConversations();
      });

      expect(result.current.conversations.length).toBe(initialLength + 1);
    });
  });

  describe('deleteConversation', () => {
    it('deletes a conversation and updates local state', async () => {
      const { result } = renderHook(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const sessionToDelete = mockConversations[0].session_id;
      const initialLength = result.current.conversations.length;

      await act(async () => {
        const success = await result.current.deleteConversation(sessionToDelete);
        expect(success).toBe(true);
      });

      expect(result.current.conversations.length).toBe(initialLength - 1);
      expect(result.current.conversations.find(c => c.session_id === sessionToDelete)).toBeUndefined();
    });

    it('handles delete error gracefully', async () => {
      server.use(
        http.delete(`${BASE_URL}/api/chat/session/:sessionId`, () => {
          return new HttpResponse(null, { status: 500 });
        })
      );

      const { result } = renderHook(() => useChatSessions());

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
      const { result } = renderHook(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      let history;
      await act(async () => {
        history = await result.current.loadConversationHistory('session-today-1');
      });

      expect(history).toHaveLength(mockConversationHistory['session-today-1'].length);
      expect(history[0].role).toBe('user');
      expect(history[0].content).toBe('Wie ist das Wetter heute?');
    });

    it('returns empty array for unknown session', async () => {
      const { result } = renderHook(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      let history;
      await act(async () => {
        history = await result.current.loadConversationHistory('unknown-session');
      });

      expect(history).toHaveLength(0);
    });
  });

  describe('addConversation', () => {
    it('adds a new conversation to the list', async () => {
      const { result } = renderHook(() => useChatSessions());

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
          updated_at: new Date().toISOString()
        });
      });

      expect(result.current.conversations.length).toBe(initialLength + 1);
      expect(result.current.conversations[0].session_id).toBe('new-local-session');
    });

    it('does not add duplicate conversations', async () => {
      const { result } = renderHook(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const existingSessionId = mockConversations[0].session_id;
      const initialLength = result.current.conversations.length;

      act(() => {
        result.current.addConversation({
          session_id: existingSessionId,
          preview: 'Duplicate',
          message_count: 1,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString()
        });
      });

      expect(result.current.conversations.length).toBe(initialLength);
    });
  });

  describe('updateConversationPreview', () => {
    it('updates the preview of an existing conversation', async () => {
      const { result } = renderHook(() => useChatSessions());

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      const sessionId = mockConversations[0].session_id;
      const newPreview = 'Updated preview text';

      act(() => {
        result.current.updateConversationPreview(sessionId, newPreview, 10);
      });

      const updated = result.current.conversations.find(c => c.session_id === sessionId);
      expect(updated.preview).toBe(newPreview);
      expect(updated.message_count).toBe(10);
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

    const conversations = [
      { session_id: '1', updated_at: now.toISOString() },
      { session_id: '2', updated_at: yesterday.toISOString() },
      { session_id: '3', updated_at: lastWeek.toISOString() },
      { session_id: '4', updated_at: older.toISOString() }
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
    const conversations = [
      { session_id: '1', created_at: now.toISOString() }
    ];

    const grouped = groupConversationsByDate(conversations);

    expect(grouped.today).toHaveLength(1);
  });
});
