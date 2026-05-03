/**
 * Shared no-op `ChatContextValue` factory for page tests.
 *
 * Tests only exercise a small slice of ChatContext at a time, but the real
 * type requires every field. We construct a fully-typed default and let
 * tests override only what they need — no `as any`, no partials.
 */
import { vi } from 'vitest';
import type { ChatContextValue } from '../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import type { UseWakeWordResult } from '../../../src/frontend/src/hooks/useWakeWord';

const wakeWordStub: UseWakeWordResult = {
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
  setThreshold: () => {},
  availableKeywords: [
    { id: 'hey_jarvis', label: 'Hey Jarvis', model: 'hey_jarvis_v0.1', description: 'pre-trained' },
  ],
};

export const defaultChatContextValue: ChatContextValue = {
  // Messages
  messages: [],
  loading: false,
  input: '',
  setInput: () => {},
  historyLoading: false,
  sendMessage: async () => {},

  // Session
  sessionId: null,
  sidebarOpen: false,
  setSidebarOpen: () => {},
  switchConversation: async () => {},
  startNewChat: () => {},
  handleDeleteConversation: async () => {},

  // Conversations
  conversations: [],
  conversationsLoading: false,

  // WebSocket
  wsConnected: false,

  // Audio
  recording: false,
  audioLevel: 0,
  silenceTimeRemaining: 0,
  toggleRecording: () => {},

  // RAG
  useRag: false,
  toggleRag: () => {},
  selectedKnowledgeBase: null,
  setSelectedKnowledgeBase: () => {},

  // Document upload
  attachments: [],
  uploading: false,
  uploadError: null,
  uploadDocument: async () => {},
  removeAttachment: () => {},
  uploadStates: {},

  // Wake word
  wakeWord: { ...wakeWordStub, status: 'idle' },
  wakeWordStatus: 'idle',

  // Quick actions
  actionLoading: {},
  actionResult: null,
  indexToKb: async () => {},
  sendToPaperless: async () => {},
  handleSummarize: () => {},
  handleSendViaEmail: () => {},

  // Email dialog
  emailDialog: null,
  confirmSendViaEmail: async () => {},
  cancelEmailDialog: () => {},

  // Actions
  speakText: async () => {},
  handleFeedbackSubmit: async () => {},
};

/**
 * Build a `ChatContextValue` with overrides applied. Vitest spies on
 * mocked methods are not added by default — tests should pass any
 * `vi.fn()` they want to assert against in `overrides`.
 */
export function buildChatContextValue(overrides: Partial<ChatContextValue> = {}): ChatContextValue {
  return { ...defaultChatContextValue, ...overrides };
}

/**
 * Convenience: full-shape stub with `vi.fn()` for the methods most
 * page-level tests inspect. Keep additions minimal — over-mocking hides
 * regressions.
 */
export function chatContextWithSpies(): ChatContextValue {
  return buildChatContextValue({
    sendMessage: vi.fn(async () => {}),
    setInput: vi.fn(),
  });
}
