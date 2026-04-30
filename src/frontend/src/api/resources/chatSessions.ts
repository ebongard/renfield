import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';
import type { ChatMessage, Conversation, ChatSessionsResult } from '../../types/chat';

interface ConversationsResponse {
  conversations: Conversation[];
  total?: number;
}

async function fetchConversations(): Promise<ConversationsResponse> {
  const response = await apiClient.get<ConversationsResponse>('/api/chat/conversations');
  return { conversations: response.data.conversations ?? [], total: response.data.total };
}

async function deleteConversationRequest(sessionId: string): Promise<void> {
  await apiClient.delete(`/api/chat/session/${sessionId}`);
}

async function fetchHistory(sessionId: string): Promise<ChatMessage[]> {
  const response = await apiClient.get<{ messages?: ChatMessage[] }>(`/api/chat/history/${sessionId}`);
  return response.data.messages ?? [];
}

/**
 * Hook for managing chat conversation sessions.
 *
 * Public shape preserved exactly so ChatContext can keep using it as-is:
 *   { conversations, loading, error, refreshConversations,
 *     deleteConversation, loadConversationHistory,
 *     addConversation, updateConversationPreview }
 *
 * `loading` is aliased to React Query's `isLoading`. Mutations use direct
 * `setQueryData` writes (optimistic add/update + immediate filter on delete).
 */
export function useChatSessions(): ChatSessionsResult {
  const queryClient = useQueryClient();
  const queryKey = keys.chatSessions.list();

  const conversationsQuery = useApiQuery(
    {
      queryKey,
      queryFn: fetchConversations,
      staleTime: STALE.DEFAULT,
    },
    'common.error',
  );

  const conversations = conversationsQuery.data?.conversations ?? [];
  const loading = conversationsQuery.isLoading;
  const error = conversationsQuery.error;

  const refreshConversations = useCallback(async () => {
    await conversationsQuery.refetch();
  }, [conversationsQuery]);

  const deleteConversation = useCallback(
    async (sessionId: string): Promise<boolean> => {
      try {
        await deleteConversationRequest(sessionId);
        queryClient.setQueryData<ConversationsResponse | undefined>(queryKey, (prev) =>
          prev ? { ...prev, conversations: prev.conversations.filter((c) => c.session_id !== sessionId) } : prev,
        );
        return true;
      } catch (err) {
        console.error('Error deleting conversation:', err);
        return false;
      }
    },
    [queryClient, queryKey],
  );

  const loadConversationHistory = useCallback(
    async (sessionId: string): Promise<ChatMessage[]> => {
      try {
        return await fetchHistory(sessionId);
      } catch (err) {
        console.error('Error loading conversation history:', err);
        return [];
      }
    },
    [],
  );

  const addConversation = useCallback(
    (conversation: Conversation) => {
      queryClient.setQueryData<ConversationsResponse | undefined>(queryKey, (prev) => {
        const existing = prev?.conversations ?? [];
        if (existing.some((c) => c.session_id === conversation.session_id)) {
          return prev;
        }
        return { conversations: [conversation, ...existing], total: prev?.total };
      });
    },
    [queryClient, queryKey],
  );

  const updateConversationPreview = useCallback(
    (sessionId: string, preview: string, messageCount: number) => {
      queryClient.setQueryData<ConversationsResponse | undefined>(queryKey, (prev) => {
        if (!prev) return prev;
        const conversations = prev.conversations.map((c) =>
          c.session_id === sessionId
            ? { ...c, preview, message_count: messageCount, updated_at: new Date().toISOString() }
            : c,
        );
        return { ...prev, conversations };
      });
    },
    [queryClient, queryKey],
  );

  return {
    conversations,
    loading,
    error: error as Error | null,
    refreshConversations,
    deleteConversation,
    loadConversationHistory,
    addConversation,
    updateConversationPreview,
  };
}
