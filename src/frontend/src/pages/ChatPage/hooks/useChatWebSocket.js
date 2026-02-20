import { useState, useRef, useCallback, useEffect } from 'react';
import { debug } from '../../../utils/debug';

/**
 * Custom hook for managing WebSocket connection to the chat endpoint.
 * Handles streaming responses, auto-reconnect, and message processing.
 *
 * @param {Object} options - Hook options
 * @param {Function} options.onStreamChunk - Callback for stream chunks
 * @param {Function} options.onStreamDone - Callback when stream completes
 * @param {Function} options.onAction - Callback when action is executed
 * @param {Function} options.onRagContext - Callback for RAG context info
 * @param {Function} options.onIntentFeedbackRequest - Callback for proactive feedback
 * @returns {Object} WebSocket state and methods
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
} = {}) {
  const [wsConnected, setWsConnected] = useState(false);
  const wsRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);

  const connectWebSocket = useCallback(() => {
    // Clear any pending reconnect
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    const wsUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws';
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      debug.log('WebSocket verbunden');
      setWsConnected(true);
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'action') {
        // Action wurde ausgeführt
        debug.log('Action ausgeführt:', data.intent, data.result);
        onAction?.(data);
      } else if (data.type === 'rag_context') {
        // RAG context info received
        debug.log('RAG Context:', data.has_context ? 'found' : 'not found');
        onRagContext?.(data);
      } else if (data.type === 'stream') {
        // Streaming-Antwort
        onStreamChunk?.(data.content);
      } else if (data.type === 'done') {
        // Stream beendet
        onStreamDone?.(data);
      } else if (data.type === 'intent_feedback_request') {
        // Proaktives Feedback vom Backend
        debug.log('Intent feedback request:', data.detected_intent);
        onIntentFeedbackRequest?.(data);
      } else if (data.type === 'document_processing') {
        debug.log('Document processing:', data.filename);
        onDocumentProcessing?.(data);
      } else if (data.type === 'document_ready') {
        debug.log('Document ready:', data.filename, 'doc_id:', data.document_id);
        onDocumentReady?.(data);
      } else if (data.type === 'document_error') {
        debug.log('Document error:', data.filename, data.error);
        onDocumentError?.(data);
      } else if (data.type === 'agent_thinking') {
        debug.log('Agent thinking:', data.content?.substring(0, 80));
        onAgentThinking?.(data);
      } else if (data.type === 'agent_tool_call') {
        debug.log('Agent tool call:', data.tool, data.reason);
        onAgentToolCall?.(data);
      } else if (data.type === 'agent_tool_result') {
        debug.log('Agent tool result:', data.tool, data.success ? 'success' : 'failed');
        onAgentToolResult?.(data);
      }
    };

    ws.onclose = () => {
      debug.log('WebSocket getrennt');
      setWsConnected(false);
      // Automatisch wieder verbinden nach 3 Sekunden
      reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = (error) => {
      console.error('WebSocket Fehler:', error);
    };

    wsRef.current = ws;
  }, [onStreamChunk, onStreamDone, onAction, onRagContext, onIntentFeedbackRequest, onDocumentProcessing, onDocumentReady, onDocumentError, onAgentThinking, onAgentToolCall, onAgentToolResult]);

  // Connect on mount, cleanup on unmount
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

  /**
   * Send a message through the WebSocket
   * @param {Object} message - Message object to send
   * @returns {boolean} Whether the message was sent successfully
   */
  const sendMessage = useCallback((message) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
      return true;
    }
    return false;
  }, []);

  /**
   * Check if WebSocket is ready to send
   * @returns {boolean} Whether the WebSocket is open
   */
  const isReady = useCallback(() => {
    return wsRef.current && wsRef.current.readyState === WebSocket.OPEN;
  }, []);

  return {
    wsConnected,
    sendMessage,
    isReady,
    reconnect: connectWebSocket,
  };
}
