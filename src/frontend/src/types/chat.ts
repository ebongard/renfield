/**
 * Chat-related type definitions
 */

// Message role
export type MessageRole = 'user' | 'assistant' | 'system';

// Chat message
export interface ChatMessage {
  id?: number;
  session_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
  metadata?: Record<string, unknown>;
}

// Conversation summary
export interface Conversation {
  session_id: string;
  preview: string;
  message_count: number;
  created_at: string;
  updated_at: string;
  first_message?: string;
  last_message?: string;
}

// Grouped conversations by date
export interface GroupedConversations {
  today: Conversation[];
  yesterday: Conversation[];
  lastWeek: Conversation[];
  older: Conversation[];
}

// Chat sessions hook result
export interface ChatSessionsResult {
  conversations: Conversation[];
  loading: boolean;
  error: Error | null;
  refreshConversations: () => Promise<void>;
  deleteConversation: (sessionId: string) => Promise<boolean>;
  loadConversationHistory: (sessionId: string) => Promise<ChatMessage[]>;
  addConversation: (conversation: Conversation) => void;
  updateConversationPreview: (sessionId: string, preview: string, messageCount: number) => void;
}

// Chat WebSocket message types
export interface ChatTextMessage {
  type: 'text';
  content: string;
  session_id?: string;
  use_rag?: boolean;
  knowledge_base_id?: string | null;
}

export interface ChatStreamMessage {
  type: 'stream';
  content: string;
}

export interface ChatActionMessage {
  type: 'action';
  intent: {
    intent: string;
    parameters: Record<string, unknown>;
    confidence: number;
  };
  result: {
    success: boolean;
    message?: string;
    data?: Record<string, unknown>;
  };
}

export interface ChatDoneMessage {
  type: 'done';
  tts_handled: boolean;
}

export interface ChatErrorMessage {
  type: 'error';
  message: string;
}

export type ChatWebSocketMessage =
  | ChatTextMessage
  | ChatStreamMessage
  | ChatActionMessage
  | ChatDoneMessage
  | ChatErrorMessage;
