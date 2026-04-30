import { useState, useRef, useCallback, useEffect } from 'react';
import { debug } from '../../../utils/debug';

export interface BaseWsMessage {
  type: string;
  [key: string]: unknown;
}

export interface StreamMessage extends BaseWsMessage {
  type: 'stream';
  content: string;
}

export interface DoneMessage extends BaseWsMessage {
  type: 'done';
  tts_handled?: boolean;
  intent?: { intent: string; confidence?: number };
}

export interface ActionWsMessage extends BaseWsMessage {
  type: 'action';
  intent: { intent?: string; confidence?: number; parameters?: Record<string, unknown> } | string;
  result: unknown;
}

export interface RagContextMessage extends BaseWsMessage {
  type: 'rag_context';
  has_context: boolean;
}

export interface IntentFeedbackRequestMessage extends BaseWsMessage {
  type: 'intent_feedback_request';
  detected_intent: string;
  confidence: number;
  message_text: string;
}

export interface DocumentProcessingMessage extends BaseWsMessage {
  type: 'document_processing';
  filename: string;
  upload_id: string;
}

export interface DocumentReadyMessage extends BaseWsMessage {
  type: 'document_ready';
  filename: string;
  document_id: string;
  upload_id: string;
}

export interface DocumentErrorMessage extends BaseWsMessage {
  type: 'document_error';
  filename: string;
  error: string;
  upload_id: string;
}

export interface AgentThinkingMessage extends BaseWsMessage {
  type: 'agent_thinking';
  step?: number;
  content?: string;
}

export interface AgentToolCallMessage extends BaseWsMessage {
  type: 'agent_tool_call';
  step?: number;
  tool: string;
  parameters?: unknown;
  reason?: string;
}

export interface AgentToolResultMessage extends BaseWsMessage {
  type: 'agent_tool_result';
  step?: number;
  tool: string;
  success: boolean;
  message?: string;
  data?: unknown;
}

export interface AgentFederationProgressMessage extends BaseWsMessage {
  type: 'agent_federation_progress';
  peer_pubkey: string;
  peer_display_name: string;
  label: string;
  sequence: number;
}

export interface CardMessage extends BaseWsMessage {
  type: 'card';
  card?: Record<string, unknown>;
}

interface UseChatWebSocketOptions {
  onStreamChunk?: (content: string) => void;
  onStreamDone?: (data: DoneMessage) => void;
  onAction?: (data: ActionWsMessage) => void;
  onRagContext?: (data: RagContextMessage) => void;
  onIntentFeedbackRequest?: (data: IntentFeedbackRequestMessage) => void;
  onDocumentProcessing?: (data: DocumentProcessingMessage) => void;
  onDocumentReady?: (data: DocumentReadyMessage) => void;
  onDocumentError?: (data: DocumentErrorMessage) => void;
  onAgentThinking?: (data: AgentThinkingMessage) => void;
  onAgentToolCall?: (data: AgentToolCallMessage) => void;
  onAgentToolResult?: (data: AgentToolResultMessage) => void;
  onAgentFederationProgress?: (data: AgentFederationProgressMessage) => void;
  onCard?: (data: CardMessage) => void;
}

/**
 * Custom hook for managing WebSocket connection to the chat endpoint.
 * Handles streaming responses, auto-reconnect, and message processing.
 */
export function useChatWebSocket({
  onStreamChunk,
  onStreamDone,
  onAction,
  onRagContext,
  onIntentFeedbackRequest,
  onDocumentProcessing,
  onDocumentReady,
  onDocumentError,
  onAgentThinking,
  onAgentToolCall,
  onAgentToolResult,
  onAgentFederationProgress,
  onCard,
}: UseChatWebSocketOptions = {}) {
  const [wsConnected, setWsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Pending whenReady() resolvers, drained on the next onopen/onclose.
  const readyResolversRef = useRef<Array<(ok: boolean) => void>>([]);

  const connectWebSocket = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    let wsUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws';
    // Append JWT token for WebSocket authentication (falls back to device-token cookie if absent)
    const accessToken = localStorage.getItem('renfield_access_token');
    if (accessToken) {
      const sep = wsUrl.includes('?') ? '&' : '?';
      wsUrl = `${wsUrl}${sep}token=${accessToken}`;
    }
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      debug.log('WebSocket verbunden');
      setWsConnected(true);
      // Wake any callers waiting on whenReady().
      const pending = readyResolversRef.current.splice(0);
      pending.forEach((resolve) => resolve(true));
    };

    ws.onmessage = (event: MessageEvent) => {
      const data = JSON.parse(event.data as string) as BaseWsMessage;

      if (data.type === 'action') {
        const msg = data as ActionWsMessage;
        debug.log('Action ausgeführt:', msg.intent, msg.result);
        onAction?.(msg);
      } else if (data.type === 'rag_context') {
        const msg = data as RagContextMessage;
        debug.log('RAG Context:', msg.has_context ? 'found' : 'not found');
        onRagContext?.(msg);
      } else if (data.type === 'stream') {
        onStreamChunk?.((data as StreamMessage).content);
      } else if (data.type === 'done') {
        onStreamDone?.(data as DoneMessage);
      } else if (data.type === 'intent_feedback_request') {
        const msg = data as IntentFeedbackRequestMessage;
        debug.log('Intent feedback request:', msg.detected_intent);
        onIntentFeedbackRequest?.(msg);
      } else if (data.type === 'document_processing') {
        const msg = data as DocumentProcessingMessage;
        debug.log('Document processing:', msg.filename);
        onDocumentProcessing?.(msg);
      } else if (data.type === 'document_ready') {
        const msg = data as DocumentReadyMessage;
        debug.log('Document ready:', msg.filename, 'doc_id:', msg.document_id);
        onDocumentReady?.(msg);
      } else if (data.type === 'document_error') {
        const msg = data as DocumentErrorMessage;
        debug.log('Document error:', msg.filename, msg.error);
        onDocumentError?.(msg);
      } else if (data.type === 'agent_thinking') {
        const msg = data as AgentThinkingMessage;
        debug.log('Agent thinking:', msg.content?.substring(0, 80));
        onAgentThinking?.(msg);
      } else if (data.type === 'agent_tool_call') {
        const msg = data as AgentToolCallMessage;
        debug.log('Agent tool call:', msg.tool, msg.reason);
        onAgentToolCall?.(msg);
      } else if (data.type === 'agent_tool_result') {
        const msg = data as AgentToolResultMessage;
        debug.log('Agent tool result:', msg.tool, msg.success ? 'success' : 'failed');
        onAgentToolResult?.(msg);
      } else if (data.type === 'agent_federation_progress') {
        const msg = data as AgentFederationProgressMessage;
        debug.log('Federation progress:', msg.peer_display_name, msg.label, `seq=${msg.sequence}`);
        onAgentFederationProgress?.(msg);
      } else if (data.type === 'card') {
        debug.log('Card received');
        onCard?.(data as CardMessage);
      }
    };

    ws.onclose = () => {
      debug.log('WebSocket getrennt');
      setWsConnected(false);
      // Don't leave whenReady() callers hanging across a closed socket; the
      // next reconnect will create a fresh ws with its own onopen drain.
      const pending = readyResolversRef.current.splice(0);
      pending.forEach((resolve) => resolve(false));
      reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = (error: Event) => {
      console.error('WebSocket error:', error);
    };

    wsRef.current = ws;
  }, [onStreamChunk, onStreamDone, onAction, onRagContext, onIntentFeedbackRequest, onDocumentProcessing, onDocumentReady, onDocumentError, onAgentThinking, onAgentToolCall, onAgentToolResult, onAgentFederationProgress, onCard]);

  useEffect(() => {
    connectWebSocket();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connectWebSocket]);

  const sendMessage = useCallback((message: unknown): boolean => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
      return true;
    }
    return false;
  }, []);

  const isReady = useCallback((): boolean => {
    return Boolean(wsRef.current && wsRef.current.readyState === WebSocket.OPEN);
  }, []);

  /**
   * Resolve `true` once the WebSocket reaches OPEN, or `false` on timeout
   * or abort. Resolves immediately if already OPEN. Used by callers that
   * need to wait through the page-load handshake before falling back to
   * a non-WebSocket path.
   */
  const whenReady = useCallback(
    (timeoutMs: number = 3000, signal?: AbortSignal): Promise<boolean> => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        return Promise.resolve(true);
      }
      if (signal?.aborted) {
        return Promise.resolve(false);
      }
      return new Promise<boolean>((resolve) => {
        let settled = false;
        const settle = (ok: boolean) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (signal && onAbort) signal.removeEventListener('abort', onAbort);
          resolve(ok);
        };
        const onAbort = () => settle(false);
        const timer = setTimeout(() => settle(false), timeoutMs);
        if (signal) signal.addEventListener('abort', onAbort, { once: true });
        // The resolver registry is drained by ws.onopen (true) and
        // ws.onclose (false). Already-settled entries are no-ops.
        readyResolversRef.current.push(settle);
      });
    },
    [],
  );

  return {
    wsConnected,
    sendMessage,
    isReady,
    whenReady,
    reconnect: connectWebSocket,
  };
}
