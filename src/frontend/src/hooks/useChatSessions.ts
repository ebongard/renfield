import { useState, useEffect, useCallback } from 'react';
import apiClient from '../utils/axios';
import type { Conversation, ChatMessage, GroupedConversations, ChatSessionsResult } from '../types/chat';

/**
 * Hook for managing chat conversation sessions.
 * Fetches conversations from the API and provides methods for CRUD operations.
 */
export function useChatSessions(): ChatSessionsResult {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  /**
   * Fetch all conversations from the API
   */
  const refreshConversations = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiClient.get('/api/chat/conversations');
      setConversations(response.data.conversations || []);
    } catch (err) {
      console.error('Error fetching conversations:', err);
      setError(err instanceof Error ? err : new Error(String(err)));
      setConversations([]);
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * Delete a conversation by session ID
   */
  const deleteConversation = useCallback(async (sessionId: string): Promise<boolean> => {
    try {
      await apiClient.delete(`/api/chat/session/${sessionId}`);
      setConversations(prev => prev.filter(c => c.session_id !== sessionId));
      return true;
    } catch (err) {
      console.error('Error deleting conversation:', err);
      setError(err instanceof Error ? err : new Error(String(err)));
      return false;
    }
  }, []);

  /**
   * Load the full message history for a conversation
   */
  const loadConversationHistory = useCallback(async (sessionId: string): Promise<ChatMessage[]> => {
    try {
      const response = await apiClient.get(`/api/chat/history/${sessionId}`);
      return response.data.messages || [];
    } catch (err) {
      console.error('Error loading conversation history:', err);
      setError(err instanceof Error ? err : new Error(String(err)));
      return [];
    }
  }, []);

  /**
   * Add a new conversation to the local list (optimistic update)
   */
  const addConversation = useCallback((conversation: Conversation) => {
    setConversations(prev => {
      // Check if already exists
      if (prev.some(c => c.session_id === conversation.session_id)) {
        return prev;
      }
      return [conversation, ...prev];
    });
  }, []);

  /**
   * Update a conversation's preview text (optimistic update)
   */
  const updateConversationPreview = useCallback((sessionId: string, preview: string, messageCount: number) => {
    setConversations(prev => prev.map(c =>
      c.session_id === sessionId
        ? { ...c, preview, message_count: messageCount, updated_at: new Date().toISOString() }
        : c
    ));
  }, []);

  // Fetch conversations on mount
  useEffect(() => {
    refreshConversations();
  }, [refreshConversations]);

  return {
    conversations,
    loading,
    error,
    refreshConversations,
    deleteConversation,
    loadConversationHistory,
    addConversation,
    updateConversationPreview
  };
}

/**
 * Group conversations by date periods
 */
export function groupConversationsByDate(conversations: Conversation[]): GroupedConversations {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const lastWeek = new Date(today);
  lastWeek.setDate(today.getDate() - 7);

  const groups: GroupedConversations = {
    today: [],
    yesterday: [],
    lastWeek: [],
    older: []
  };

  conversations.forEach(conv => {
    const convDate = new Date(conv.updated_at || conv.created_at);

    if (convDate >= today) {
      groups.today.push(conv);
    } else if (convDate >= yesterday) {
      groups.yesterday.push(conv);
    } else if (convDate >= lastWeek) {
      groups.lastWeek.push(conv);
    } else {
      groups.older.push(conv);
    }
  });

  return groups;
}

export default useChatSessions;
