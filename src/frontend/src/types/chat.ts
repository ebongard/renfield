/**
 * Chat-related type definitions
 */

// Message role
export type MessageRole = 'user' | 'assistant' | 'system';

// Provenance source — a knowledge-base document a knowledge-backed answer drew
// on. Surfaced as a "source chip" under the assistant turn. `tier` is the
// circle access-tier (0=self … 4=public); null when the row carried no tier.
export interface MessageSource {
  document_id: number | string;
  filename: string;
  title: string;
  tier?: number | null;
}

// Chat message
// Chat branching (Phase 2): sibling-branch info for the ‹n/m› switcher. Present
// on a history message only when it has >1 sibling under the same parent.
// `sibling_ids` is ordered (timestamp, id); `index` is this message's position.
export interface BranchInfo {
  index: number;
  count: number;
  sibling_ids: number[];
}

export interface ChatMessage {
  id?: number;
  session_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
  metadata?: Record<string, unknown>;
  // Provenance chips. On the live turn these arrive on the `done` frame; on
  // history load they rehydrate from `metadata.sources`.
  sources?: MessageSource[];
  // Chat branching (Phase 2): sibling-branch info (absent when no siblings).
  branch?: BranchInfo;
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
  agent_steps?: number;
  sources?: MessageSource[];
}

// Ephemeral follow-up suggestion chips, pushed AFTER `done` (generated in the
// background so it never delays the turn). Attaches to the last assistant turn.
export interface ChatFollowupsMessage {
  type: 'followups';
  suggested_followups: string[];
}

// Chat artifact (Lane A: typed table/list/keyvalue/chart). The wire payload is
// intentionally loosely typed (`data: unknown`) — the AUTHORITATIVE shape check
// is the frontend `artifactSchema.ts` (zod) inside ArtifactRenderer; a bad shape
// falls back to escaped text. `id` keys streaming patches + the persisted array.
export interface ChatArtifactPayload {
  id: string;
  kind: string;
  title?: string;
  data: unknown;
  partial?: boolean;
}

export interface ChatArtifactMessage {
  type: 'artifact';
  artifact: ChatArtifactPayload;
  replace_text?: string;
}

export interface ChatErrorMessage {
  type: 'error';
  message: string;
}

// Federation progress — emitted while a remote peer is answering a
// query from the local agent loop. The frontend keys live status lines
// by `peer_pubkey` so concurrent fan-out to multiple peers stays
// readable. `label` is from the locked FEDERATION_PROGRESS_LABELS
// vocabulary: waking_up | retrieving | synthesizing | complete | failed.
// `detail` is currently only `{ peer: display_name }` (redundant with
// `peer_display_name` above); reserved for future per-label context
// additions without an i18n shape break.
export interface ChatFederationProgressMessage {
  type: 'agent_federation_progress';
  peer_pubkey: string;
  peer_display_name: string;
  label: 'waking_up' | 'retrieving' | 'synthesizing' | 'complete' | 'failed' | string;
  detail: Record<string, unknown>;
  sequence: number;
}

export type ChatWebSocketMessage =
  | ChatTextMessage
  | ChatStreamMessage
  | ChatActionMessage
  | ChatDoneMessage
  | ChatFollowupsMessage
  | ChatArtifactMessage
  | ChatErrorMessage
  | ChatFederationProgressMessage;
