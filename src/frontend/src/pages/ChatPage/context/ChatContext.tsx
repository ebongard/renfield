import { createContext, useContext, useState, useEffect, useRef, useCallback, useMemo } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import { useTranslation } from 'react-i18next';
import apiClient from '../../../utils/axios';
import { debug } from '../../../utils/debug';
import { useWakeWord } from '../../../hooks/useWakeWord';
import { WAKEWORD_CONFIG } from '../../../config/wakeword';
import { useChatSessions } from '../../../hooks/useChatSessions';
import {
  useChatWebSocket,
  useAudioRecording,
  useDocumentUpload,
  useQuickActions,
} from '../hooks';
import type {
  ActionWsMessage,
  AgentFederationProgressMessage,
  AgentThinkingMessage,
  AgentToolCallMessage,
  AgentToolResultMessage,
  CardMessage,
  DocumentErrorMessage,
  DocumentProcessingMessage,
  DocumentReadyMessage,
  DoneMessage,
  IntentFeedbackRequestMessage,
  RagContextMessage,
} from '../hooks/useChatWebSocket';
import type { UploadStates, UploadedDocument } from '../hooks/useDocumentUpload';
import type { Conversation } from '../../../types/chat';
import { useConfirmDialog } from '../../../components/ConfirmDialog';

const SESSION_STORAGE_KEY = 'renfield_current_session';

type WakeWordStatus = 'idle' | 'listening' | 'recording' | 'activated';

type AgentStep =
  | { type: 'thinking'; step?: number; content?: string }
  | { type: 'tool_call'; step?: number; tool: string; parameters?: unknown; reason?: string }
  | { type: 'tool_result'; step?: number; tool: string; success: boolean; message?: string; data?: unknown };

interface FederationProgressEntry {
  peer_display_name: string;
  label: string;
  sequence: number;
}

export interface MessageAttachment {
  id: string;
  filename: string;
  status?: string;
  indexing?: boolean;
  indexed?: boolean;
  document_id?: string;
  indexError?: string;
  file_size?: number;
}

interface IntentInfo {
  intent: string;
  confidence: number;
}

interface ChatUiMessage {
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
  intentInfo?: IntentInfo;
  feedbackRequested?: boolean;
  userQuery?: string;
  agentSteps?: AgentStep[];
  federationProgress?: Record<string, FederationProgressEntry>;
  attachments?: MessageAttachment[];
  card?: Record<string, unknown>;
}

interface EmailDialogState {
  uploadId: string;
  filename: string;
}

interface AudioContextCapableWindow {
  AudioContext?: typeof AudioContext;
  webkitAudioContext?: typeof AudioContext;
}

interface TtsErrorWindow {
  _ttsErrorShown?: boolean;
}

type WakeWordHook = ReturnType<typeof useWakeWord>;
type ActionLoading = Record<string, 'indexing' | 'paperless' | 'email'>;
type ActionResult = ReturnType<typeof useQuickActions>['actionResult'];

export interface ChatContextValue {
  // Messages
  messages: ChatUiMessage[];
  loading: boolean;
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  historyLoading: boolean;
  sendMessage: (text: string, fromVoice?: boolean) => Promise<void>;

  // Session
  sessionId: string | null;
  sidebarOpen: boolean;
  setSidebarOpen: Dispatch<SetStateAction<boolean>>;
  switchConversation: (newSessionId: string) => Promise<void>;
  startNewChat: () => void;
  handleDeleteConversation: (id: string) => Promise<void>;

  // Conversations
  conversations: Conversation[];
  conversationsLoading: boolean;

  // WebSocket
  wsConnected: boolean;

  // Audio
  recording: boolean;
  audioLevel: number;
  silenceTimeRemaining: number;
  toggleRecording: () => void;

  // RAG
  useRag: boolean;
  toggleRag: () => void;
  selectedKnowledgeBase: string | null;
  setSelectedKnowledgeBase: Dispatch<SetStateAction<string | null>>;

  // Document upload
  attachments: MessageAttachment[];
  uploading: boolean;
  uploadError: string | null;
  uploadDocument: (fileOrFiles: File | File[]) => Promise<void>;
  removeAttachment: (id: string) => void;
  uploadStates: UploadStates;

  // Wake word
  wakeWord: WakeWordHook & { status: WakeWordStatus };
  wakeWordStatus: WakeWordStatus;

  // Quick actions
  actionLoading: ActionLoading;
  actionResult: ActionResult;
  indexToKb: (uploadId: string, kbId: string | number) => Promise<void>;
  sendToPaperless: (uploadId: string) => Promise<void>;
  handleSummarize: (uploadId: string) => void;
  handleSendViaEmail: (uploadId: string) => void;

  // Email dialog
  emailDialog: EmailDialogState | null;
  confirmSendViaEmail: (to: string, subject: string, body: string) => Promise<void>;
  cancelEmailDialog: () => void;

  // Actions
  speakText: (text: string) => Promise<void>;
  handleFeedbackSubmit: (
    messageText: string,
    feedbackType: string,
    originalValue: string,
    correctedValue: string,
  ) => Promise<void>;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function useChatContext(): ChatContextValue {
  const context = useContext(ChatContext);
  if (!context) throw new Error('useChatContext must be used within ChatProvider');
  return context;
}

interface ChatProviderProps {
  children: ReactNode;
}

export function ChatProvider({ children }: ChatProviderProps) {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  // Message state
  const [messages, setMessages] = useState<ChatUiMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [input, setInput] = useState('');

  // Session management
  const [sessionId, setSessionId] = useState<string | null>(() => {
    return localStorage.getItem(SESSION_STORAGE_KEY) || null;
  });

  // Sidebar state
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);

  // RAG State
  const [useRag, setUseRag] = useState(false);
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState<string | null>(null);
  const [, setRagSources] = useState<unknown[]>([]);

  // Document upload state
  const [attachments, setAttachments] = useState<MessageAttachment[]>([]);

  // Wake word state
  const [wakeWordStatus, setWakeWordStatus] = useState<WakeWordStatus>('idle');
  const wakeWordActivatedRef = useRef(false);
  const wakeWordEnabledRef = useRef(false);
  const audioContextUnlockedRef = useRef<AudioContext | null>(null);

  // Voice input tracking
  const lastInputChannelRef = useRef<'text' | 'voice'>('text');
  const lastAutoTTSTextRef = useRef('');
  const autoTTSPendingRef = useRef(false);

  // TTS audio ref
  const audioRef = useRef<AudioBufferSourceNode | null>(null);

  // Intent feedback tracking
  const lastUserQueryRef = useRef('');
  const lastIntentInfoRef = useRef<IntentInfo | null>(null);

  // Chat sessions hook
  const {
    conversations,
    loading: conversationsLoading,
    deleteConversation,
    loadConversationHistory,
    addConversation,
  } = useChatSessions();

  const getAudioContext = useCallback((): AudioContext | null => {
    const win = window as unknown as AudioContextCapableWindow;
    const Ctor = win.AudioContext ?? win.webkitAudioContext;
    if (!Ctor) return null;
    if (!audioContextUnlockedRef.current || audioContextUnlockedRef.current.state === 'closed') {
      audioContextUnlockedRef.current = new Ctor();
      debug.log('AudioContext created and unlocked for TTS');
    }
    return audioContextUnlockedRef.current;
  }, []);

  // Play activation sound when wake word is detected
  const playActivationSound = useCallback(() => {
    try {
      const audioContext = getAudioContext();
      if (!audioContext) {
        console.warn('AudioContext not available for activation sound');
        return;
      }

      if (audioContext.state === 'suspended') {
        audioContext.resume();
      }

      const oscillator = audioContext.createOscillator();
      const gainNode = audioContext.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(audioContext.destination);

      oscillator.frequency.value = 880;
      oscillator.type = 'sine';
      gainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.2);

      oscillator.start(audioContext.currentTime);
      oscillator.stop(audioContext.currentTime + 0.2);
    } catch (e) {
      console.warn('Could not play activation sound:', e);
    }
  }, [getAudioContext]);

  // Speak text using TTS
  const speakText = useCallback(async (text: string): Promise<void> => {
    try {
      if (audioRef.current) {
        try {
          audioRef.current.stop();
        } catch {
          /* may already be stopped */
        }
        audioRef.current = null;
      }

      if (!text || text.trim().length === 0) {
        console.warn('Skipping TTS for empty message');
        return;
      }

      if (text.length > 500) {
        console.warn('Long message detected, TTS may take time:', text.length, 'chars');
      }

      debug.log('Requesting TTS for:', text.substring(0, 50) + '...');

      const response = await apiClient.post<ArrayBuffer>(
        '/api/voice/tts',
        { text },
        { responseType: 'arraybuffer' },
      );

      if (response.data.byteLength < 100) {
        throw new Error('TTS response too small (Piper likely not available)');
      }

      const audioContext = getAudioContext();
      if (!audioContext) {
        throw new Error('AudioContext is not supported');
      }

      if (audioContext.state === 'suspended') {
        await audioContext.resume();
        debug.log('AudioContext resumed');
      }

      const audioBuffer = await audioContext.decodeAudioData(response.data.slice(0));
      debug.log('Audio decoded:', audioBuffer.duration.toFixed(2), 'seconds');

      const source = audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(audioContext.destination);

      audioRef.current = source;

      return new Promise<void>((resolve) => {
        source.onended = () => {
          audioRef.current = null;
          debug.log('TTS playback completed');
          resolve();
        };

        source.start(0);
        debug.log('TTS playback started');
      });
    } catch (error) {
      console.error('TTS error:', error);

      const w = window as TtsErrorWindow;
      if (!w._ttsErrorShown) {
        console.warn('TTS not available. Check Piper in backend.');
        w._ttsErrorShown = true;
      }
    }
  }, [getAudioContext]);

  // Ref for startRecording function (used by wake word callback)
  const startRecordingRef = useRef<(() => void) | null>(null);

  // Ref for sendMessageInternal (used by handleTranscription before
  // sendMessageInternal is declared below).
  const sendMessageInternalRef = useRef<(text: string, fromVoice?: boolean) => Promise<void>>(
    async () => undefined,
  );

  // Handle wake word detection
  const handleWakeWordDetected = useCallback(async (keyword: string, score: number) => {
    debug.log(`Wake word detected: ${keyword} (score: ${score.toFixed(2)})`);
    setWakeWordStatus('activated');
    wakeWordActivatedRef.current = true;

    playActivationSound();

    await new Promise((r) => setTimeout(r, WAKEWORD_CONFIG.activationDelayMs));

    if (startRecordingRef.current) {
      startRecordingRef.current();
    }
  }, [playActivationSound]);

  const handleWakeWordSpeechEnd = useCallback(() => {
    debug.log('Wake word VAD: Speech ended');
  }, []);

  const handleWakeWordError = useCallback((error: Error) => {
    console.error('Wake word error:', error);
    setWakeWordStatus('idle');
  }, []);

  // Wake word hook
  const wakeWord = useWakeWord({
    onWakeWordDetected: handleWakeWordDetected,
    onSpeechEnd: handleWakeWordSpeechEnd,
    onError: handleWakeWordError,
  });

  const { pause: pauseWakeWord, resume: resumeWakeWord, isEnabled: wakeWordEnabled } = wakeWord;

  // Keep wakeWordEnabledRef in sync
  useEffect(() => {
    wakeWordEnabledRef.current = wakeWordEnabled;
  }, [wakeWordEnabled]);

  // Handle action — capture intent info for feedback
  const handleAction = useCallback((data: ActionWsMessage) => {
    if (data.intent) {
      const intentObj = typeof data.intent === 'string' ? null : data.intent;
      lastIntentInfoRef.current = {
        intent: intentObj?.intent ?? (typeof data.intent === 'string' ? data.intent : ''),
        confidence: intentObj?.confidence ?? 0,
      };
    }
  }, []);

  // Handle proactive feedback request from backend
  const handleIntentFeedbackRequest = useCallback((data: IntentFeedbackRequestMessage) => {
    setMessages((prev) => {
      const lastMsg = prev[prev.length - 1];
      if (lastMsg && lastMsg.role === 'assistant') {
        return [
          ...prev.slice(0, -1),
          {
            ...lastMsg,
            intentInfo: {
              intent: data.detected_intent,
              confidence: data.confidence,
            },
            feedbackRequested: true,
            userQuery: data.message_text,
          },
        ];
      }
      return prev;
    });
  }, []);

  // Submit feedback correction to backend
  const handleFeedbackSubmit = useCallback(async (
    messageText: string,
    feedbackType: string,
    originalValue: string,
    correctedValue: string,
  ) => {
    try {
      await apiClient.post('/api/feedback/correction', {
        message_text: messageText,
        feedback_type: feedbackType,
        original_value: originalValue,
        corrected_value: correctedValue,
      });
      debug.log('Feedback submitted:', feedbackType, originalValue, '→', correctedValue);
    } catch (error) {
      console.error('Failed to submit feedback:', error);
    }
  }, []);

  // Handle stream done - process TTS and wake word resume
  const handleStreamDone = useCallback((data: DoneMessage) => {
    const ttsHandledByServer = data.tts_handled === true;

    setMessages((prev) => {
      const lastMsg = prev[prev.length - 1];
      if (lastMsg && lastMsg.streaming) {
        const intentInfo: IntentInfo | undefined = data.intent
          ? { intent: data.intent.intent, confidence: data.intent.confidence ?? 0 }
          : lastIntentInfoRef.current ?? undefined;

        const completedMessage: ChatUiMessage = {
          ...lastMsg,
          streaming: false,
          intentInfo,
          userQuery: lastUserQueryRef.current || undefined,
          // F4c — any lingering per-peer progress lines belong only to
          // the live streaming phase; drop them when the message finalizes.
          federationProgress: undefined,
        };
        lastIntentInfoRef.current = null;

        debug.log('Check Auto-TTS: Channel =', lastInputChannelRef.current, ', ServerHandled =', ttsHandledByServer);

        if (ttsHandledByServer) {
          debug.log('TTS handled by server - skipping local playback');

          if (wakeWordEnabledRef.current && wakeWordActivatedRef.current) {
            setTimeout(() => {
              debug.log('Resuming wake word detection after server TTS...');
              resumeWakeWord();
              setWakeWordStatus('listening');
              wakeWordActivatedRef.current = false;
            }, 3000);
          }
        } else if (lastInputChannelRef.current === 'voice' && completedMessage.role === 'assistant') {
          if (autoTTSPendingRef.current) {
            debug.log('Auto-TTS skipped: Request already active');
          } else if (lastAutoTTSTextRef.current === completedMessage.content) {
            debug.log('Auto-TTS skipped: Same text already played');
          } else {
            debug.log('Auto-playing TTS response (voice input detected)');
            autoTTSPendingRef.current = true;
            lastAutoTTSTextRef.current = completedMessage.content;

            setTimeout(() => {
              speakText(completedMessage.content).finally(() => {
                autoTTSPendingRef.current = false;

                if (wakeWordEnabledRef.current && wakeWordActivatedRef.current) {
                  debug.log('Resuming wake word detection after TTS...');
                  resumeWakeWord();
                  setWakeWordStatus('listening');
                  wakeWordActivatedRef.current = false;
                }
              });
            }, 200);
          }
        } else {
          debug.log('No Auto-TTS: Channel is', lastInputChannelRef.current);

          if (wakeWordEnabledRef.current && wakeWordActivatedRef.current) {
            debug.log('Resuming wake word detection (no TTS)...');
            resumeWakeWord();
            setWakeWordStatus('listening');
            wakeWordActivatedRef.current = false;
          }
        }

        return [...prev.slice(0, -1), completedMessage];
      }
      return prev;
    });
    setLoading(false);
  }, [speakText, resumeWakeWord]);

  // Handle stream chunk
  const handleStreamChunk = useCallback((content: string) => {
    setMessages((prev) => {
      const lastMsg = prev[prev.length - 1];
      if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
        return [
          ...prev.slice(0, -1),
          { ...lastMsg, content: lastMsg.content + content },
        ];
      }
      return [...prev, { role: 'assistant', content, streaming: true }];
    });
  }, []);

  // Handle RAG context
  const handleRagContext = useCallback((data: RagContextMessage) => {
    if (!data.has_context) {
      setRagSources([]);
    }
  }, []);

  // Handle agent steps (tool calls and results shown inline)
  const handleAgentThinking = useCallback((data: AgentThinkingMessage) => {
    setMessages((prev) => {
      const lastMsg = prev[prev.length - 1];
      const newStep: AgentStep = { type: 'thinking', step: data.step, content: data.content };
      if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
        const steps = [...(lastMsg.agentSteps ?? []), newStep];
        return [...prev.slice(0, -1), { ...lastMsg, agentSteps: steps }];
      }
      // No streaming message yet — create one with just agent steps
      return [...prev, { role: 'assistant', content: '', streaming: true, agentSteps: [newStep] }];
    });
  }, []);

  const handleAgentToolCall = useCallback((data: AgentToolCallMessage) => {
    setMessages((prev) => {
      const lastMsg = prev[prev.length - 1];
      const newStep: AgentStep = {
        type: 'tool_call',
        step: data.step,
        tool: data.tool,
        parameters: data.parameters,
        reason: data.reason,
      };
      if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
        const steps = [...(lastMsg.agentSteps ?? []), newStep];
        return [...prev.slice(0, -1), { ...lastMsg, agentSteps: steps }];
      }
      return [...prev, { role: 'assistant', content: '', streaming: true, agentSteps: [newStep] }];
    });
  }, []);

  const handleAgentToolResult = useCallback((data: AgentToolResultMessage) => {
    setMessages((prev) => {
      const lastMsg = prev[prev.length - 1];
      if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
        const newStep: AgentStep = {
          type: 'tool_result',
          step: data.step,
          tool: data.tool,
          success: data.success,
          message: data.message,
          data: data.data,
        };
        const steps = [...(lastMsg.agentSteps ?? []), newStep];
        return [...prev.slice(0, -1), { ...lastMsg, agentSteps: steps }];
      }
      return prev;
    });
  }, []);

  // F4c — live federation progress per remote peer. Keyed by pubkey so
  // fan-out to multiple peers renders one status line per peer. On a
  // terminal chunk (`complete`/`failed`) we remove that peer's entry;
  // `handleStreamDone` wipes anything still lingering (e.g., agent
  // aborted mid-tool). We deliberately do NOT clear on agent_tool_result
  // — parallel tool dispatch means other peers may still be mid-flight
  // when one completes.
  //
  // Note on the terminal branch: today's FederationQueryAsker only emits
  // `waking_up` / `retrieving` / `synthesizing` as ProgressChunks and
  // transitions to a FinalResult on terminal status — so the `isTerminal`
  // delete path is defense-in-depth against a future asker revision that
  // emits a terminal chunk. Today, cleanup rides entirely on handleStreamDone.
  //
  // Out-of-order chunks are ignored by `sequence`: only advance when
  // seq > stored seq (drops stale late arrivals but keeps terminal
  // chunks regardless, since losing a `complete` would strand the line).
  const handleAgentFederationProgress = useCallback((data: AgentFederationProgressMessage) => {
    const { peer_pubkey, peer_display_name, label, sequence } = data;
    setMessages((prev) => {
      const lastMsg = prev[prev.length - 1];
      if (!lastMsg || lastMsg.role !== 'assistant' || !lastMsg.streaming) {
        // Chunk arrived before any assistant message — attach to a new
        // streaming message so the user sees something while waiting.
        return [...prev, {
          role: 'assistant',
          content: '',
          streaming: true,
          federationProgress: { [peer_pubkey]: { peer_display_name, label, sequence } },
        }];
      }
      const current = lastMsg.federationProgress ?? {};
      const next = { ...current };
      const isTerminal = label === 'complete' || label === 'failed';
      if (isTerminal) {
        delete next[peer_pubkey];
      } else {
        const existing = current[peer_pubkey];
        if (existing && sequence <= existing.sequence) {
          return prev; // stale chunk, drop it
        }
        next[peer_pubkey] = { peer_display_name, label, sequence };
      }
      return [...prev.slice(0, -1), { ...lastMsg, federationProgress: next }];
    });
  }, []);

  // Handle document processing notifications from backend
  const handleDocumentProcessing = useCallback((data: DocumentProcessingMessage) => {
    setMessages((prev) => prev.map((msg) => {
      if (!msg.attachments) return msg;
      const updated = msg.attachments.map((att) =>
        att.id === data.upload_id ? { ...att, indexing: true } : att,
      );
      return updated !== msg.attachments ? { ...msg, attachments: updated } : msg;
    }));
  }, []);

  const handleDocumentReady = useCallback((data: DocumentReadyMessage) => {
    setMessages((prev) => prev.map((msg) => {
      if (!msg.attachments) return msg;
      const updated = msg.attachments.map((att) =>
        att.id === data.upload_id
          ? { ...att, indexing: false, indexed: true, document_id: data.document_id }
          : att,
      );
      return updated !== msg.attachments ? { ...msg, attachments: updated } : msg;
    }));
  }, []);

  const handleDocumentError = useCallback((data: DocumentErrorMessage) => {
    setMessages((prev) => prev.map((msg) => {
      if (!msg.attachments) return msg;
      const updated = msg.attachments.map((att) =>
        att.id === data.upload_id
          ? { ...att, indexing: false, indexError: data.error }
          : att,
      );
      return updated !== msg.attachments ? { ...msg, attachments: updated } : msg;
    }));
  }, []);

  // Adaptive Card from server (sent after orchestrated/single-role response)
  const handleCard = useCallback((data: CardMessage) => {
    if (!data.card) return;
    setMessages((prev) => {
      const updated = [...prev];
      // Attach to most recent assistant message
      for (let i = updated.length - 1; i >= 0; i--) {
        if (updated[i].role === 'assistant') {
          updated[i] = { ...updated[i], card: data.card };
          break;
        }
      }
      return updated;
    });
  }, []);

  // WebSocket hook
  const { wsConnected, sendMessage: wsSendMessage, isReady } = useChatWebSocket({
    onStreamChunk: handleStreamChunk,
    onStreamDone: handleStreamDone,
    onAction: handleAction,
    onRagContext: handleRagContext,
    onIntentFeedbackRequest: handleIntentFeedbackRequest,
    onDocumentProcessing: handleDocumentProcessing,
    onDocumentReady: handleDocumentReady,
    onDocumentError: handleDocumentError,
    onAgentThinking: handleAgentThinking,
    onAgentToolCall: handleAgentToolCall,
    onAgentToolResult: handleAgentToolResult,
    onAgentFederationProgress: handleAgentFederationProgress,
    onCard: handleCard,
  });

  // Handle transcription from audio recording
  const handleTranscription = useCallback((text: string) => {
    debug.log('Transcription received:', text);
    sendMessageInternalRef.current(text, true);
  }, []);

  // Handle recording error
  const handleRecordingError = useCallback((errorMessage: string) => {
    setMessages((prev) => [...prev, { role: 'assistant', content: errorMessage }]);
    setLoading(false);
  }, []);

  // Handle recording start
  const handleRecordingStart = useCallback(async () => {
    lastInputChannelRef.current = 'voice';
    lastAutoTTSTextRef.current = '';
    autoTTSPendingRef.current = false;
    debug.log('Channel set to: voice');

    if (wakeWordEnabled) {
      debug.log('Pausing wake word detection for recording...');
      await pauseWakeWord();
    }
    setWakeWordStatus('recording');
  }, [wakeWordEnabled, pauseWakeWord]);

  // Handle recording stop
  const handleRecordingStop = useCallback(() => {
    if (wakeWordEnabled && !wakeWordActivatedRef.current) {
      debug.log('Resuming wake word detection after recording...');
      resumeWakeWord();
      setWakeWordStatus('listening');
    }
  }, [wakeWordEnabled, resumeWakeWord]);

  // Audio recording hook
  const {
    recording,
    audioLevel,
    silenceTimeRemaining,
    startRecording,
    toggleRecording,
  } = useAudioRecording({
    onTranscription: handleTranscription,
    onError: handleRecordingError,
    onRecordingStart: handleRecordingStart,
    onRecordingStop: handleRecordingStop,
  });

  // Document upload hook
  const {
    uploading,
    uploadError,
    uploadDocuments: doUploadMultiple,
    uploadStates,
  } = useDocumentUpload();

  const handleUploadDocument = useCallback(async (fileOrFiles: File | File[]) => {
    if (!sessionId) return;
    const files = Array.isArray(fileOrFiles) ? fileOrFiles : [fileOrFiles];
    const results = await doUploadMultiple(files, sessionId);
    const successful = results.filter((r): r is UploadedDocument => Boolean(r));
    if (successful.length > 0) {
      // Server returns full attachment shape (id, filename, status, …) under
      // UploadedDocument's index signature; surface it as MessageAttachment.
      setAttachments((prev) => [...prev, ...(successful as unknown as MessageAttachment[])]);
    }
  }, [sessionId, doUploadMultiple]);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  // Quick actions hook
  const { actionLoading, actionResult, clearResult, indexToKb, sendToPaperless, sendViaEmail } = useQuickActions();

  // Email dialog state
  const [emailDialog, setEmailDialog] = useState<EmailDialogState | null>(null);

  const handleSendViaEmail = useCallback((uploadId: string) => {
    let filename: string | null = null;
    for (const msg of messages) {
      const att = msg.attachments?.find((a) => a.id === uploadId);
      if (att) {
        filename = att.filename;
        break;
      }
    }
    // Also check pending attachments
    if (!filename) {
      const att = attachments.find((a) => a.id === uploadId);
      if (att) filename = att.filename;
    }
    if (!filename) return;
    setEmailDialog({ uploadId, filename });
  }, [messages, attachments]);

  const confirmSendViaEmail = useCallback(async (to: string, subject: string, body: string) => {
    if (!emailDialog) return;
    await sendViaEmail(emailDialog.uploadId, to, subject, body);
    setEmailDialog(null);
  }, [emailDialog, sendViaEmail]);

  const cancelEmailDialog = useCallback(() => {
    setEmailDialog(null);
  }, []);

  // Assign startRecording to ref for wake word callback
  startRecordingRef.current = startRecording;

  // Internal send message function
  const sendMessageInternal = useCallback(async (text: string, fromVoice = false): Promise<void> => {
    if (!text.trim()) return;

    if (!fromVoice) {
      lastInputChannelRef.current = 'text';
      lastAutoTTSTextRef.current = '';
      debug.log('Channel set to: text');
    }

    lastUserQueryRef.current = text;
    lastIntentInfoRef.current = null;

    // Capture current attachments before clearing
    const currentAttachments = [...attachments];
    const completedIds = currentAttachments
      .filter((a) => a.status === 'completed')
      .map((a) => a.id);

    const userMessage: ChatUiMessage = {
      role: 'user',
      content: text,
      ...(currentAttachments.length > 0 && { attachments: currentAttachments }),
    };
    setMessages((prev) => [...prev, userMessage]);
    setInput('');
    setAttachments([]);
    setLoading(true);

    const previewText = text.length > 50 ? text.substring(0, 50) + '...' : text;
    if (sessionId) {
      addConversation({
        session_id: sessionId,
        preview: previewText,
        message_count: messages.length + 1,
        updated_at: new Date().toISOString(),
        created_at: new Date().toISOString(),
      });
    }

    if (isReady()) {
      const message = {
        type: 'text',
        content: text,
        session_id: sessionId,
        use_rag: useRag,
        knowledge_base_id: selectedKnowledgeBase,
        ...(completedIds.length > 0 && { attachment_ids: completedIds }),
      };
      wsSendMessage(message);
      setRagSources([]);
    } else {
      try {
        const response = await apiClient.post<{ message: string }>('/api/chat/send', {
          message: text,
          session_id: sessionId,
        });

        setMessages((prev) => [...prev, { role: 'assistant', content: response.data.message }]);
      } catch (error) {
        console.error('Chat error:', error);
        setMessages((prev) => [...prev, { role: 'assistant', content: t('errors.couldNotProcess') }]);
      } finally {
        setLoading(false);
      }
    }
  }, [sessionId, messages.length, useRag, selectedKnowledgeBase, isReady, wsSendMessage, addConversation, attachments, t]);

  // Wire ref so handleTranscription (declared above) can call sendMessageInternal
  useEffect(() => {
    sendMessageInternalRef.current = sendMessageInternal;
  }, [sendMessageInternal]);

  // Summarize handler (must be after sendMessageInternal)
  const handleSummarize = useCallback((uploadId: string) => {
    let filename: string | null = null;
    for (const msg of messages) {
      const att = msg.attachments?.find((a) => a.id === uploadId);
      if (att) {
        filename = att.filename;
        break;
      }
    }
    if (!filename) return;
    const prompt = t('chat.summarizePrompt', { filename });
    sendMessageInternal(prompt, false);
  }, [messages, t, sendMessageInternal]);

  // Auto-clear action result after 3s
  useEffect(() => {
    if (!actionResult) return;
    const timer = setTimeout(clearResult, 3000);
    return () => clearTimeout(timer);
  }, [actionResult, clearResult]);

  // Session initialization
  useEffect(() => {
    if (!sessionId) {
      const newSessionId = `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
      setSessionId(newSessionId);
      localStorage.setItem(SESSION_STORAGE_KEY, newSessionId);
    }
  }, [sessionId]);

  // Load history when sessionId changes
  useEffect(() => {
    const loadHistory = async () => {
      if (!sessionId) return;

      const existingConv = conversations.find((c) => c.session_id === sessionId);
      if (existingConv && existingConv.message_count > 0 && messages.length === 0) {
        setHistoryLoading(true);
        try {
          const history = await loadConversationHistory(sessionId);
          if (history.length > 0) {
            setMessages(history.map((m) => {
              const meta = m.metadata as { attachments?: MessageAttachment[] } | undefined;
              return {
                role: m.role === 'system' ? 'assistant' : m.role,
                content: m.content,
                ...(meta?.attachments && meta.attachments.length > 0 && { attachments: meta.attachments }),
              };
            }));
          }
        } catch (err) {
          console.error('Failed to load conversation history:', err);
        } finally {
          setHistoryLoading(false);
        }
      }
    };

    loadHistory();
  }, [sessionId, conversations, loadConversationHistory, messages.length]);

  // Switch to existing conversation
  const switchConversation = useCallback(async (newSessionId: string) => {
    if (newSessionId === sessionId) {
      setSidebarOpen(false);
      return;
    }

    setHistoryLoading(true);
    try {
      const history = await loadConversationHistory(newSessionId);
      setMessages(history.map((m) => {
        const meta = m.metadata as { attachments?: MessageAttachment[] } | undefined;
        return {
          role: m.role === 'system' ? 'assistant' : m.role,
          content: m.content,
          ...(meta?.attachments && meta.attachments.length > 0 && { attachments: meta.attachments }),
        };
      }));
      setSessionId(newSessionId);
      localStorage.setItem(SESSION_STORAGE_KEY, newSessionId);
      setSidebarOpen(false);
    } catch (err) {
      console.error('Failed to switch conversation:', err);
    } finally {
      setHistoryLoading(false);
    }
  }, [sessionId, loadConversationHistory]);

  // Start new chat
  const startNewChat = useCallback(() => {
    const newId = `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    setSessionId(newId);
    setMessages([]);
    localStorage.setItem(SESSION_STORAGE_KEY, newId);
    setSidebarOpen(false);
  }, []);

  // Delete conversation
  const handleDeleteConversation = useCallback(async (id: string) => {
    const confirmed = await confirm({
      title: t('chat.deleteConversationTitle'),
      message: t('chat.deleteConversation'),
      confirmLabel: t('chat.deleteConversationConfirm'),
      variant: 'danger',
    });
    if (!confirmed) return;

    const success = await deleteConversation(id);
    if (success && id === sessionId) {
      startNewChat();
    }
  }, [deleteConversation, sessionId, startNewChat, t, confirm]);

  // Toggle RAG
  const toggleRag = useCallback(() => {
    setUseRag((prev) => !prev);
  }, []);

  const value = useMemo<ChatContextValue>(() => ({
    // Messages
    messages,
    loading,
    input,
    setInput,
    historyLoading,
    sendMessage: sendMessageInternal,

    // Session
    sessionId,
    sidebarOpen,
    setSidebarOpen,
    switchConversation,
    startNewChat,
    handleDeleteConversation,

    // Conversations (from useChatSessions)
    conversations,
    conversationsLoading,

    // WebSocket
    wsConnected,

    // Audio
    recording,
    audioLevel,
    silenceTimeRemaining,
    toggleRecording,

    // RAG
    useRag,
    toggleRag,
    selectedKnowledgeBase,
    setSelectedKnowledgeBase,

    // Document upload
    attachments,
    uploading,
    uploadError,
    uploadDocument: handleUploadDocument,
    removeAttachment,
    uploadStates,

    // Wake word
    wakeWord: {
      ...wakeWord,
      status: wakeWordStatus,
    },
    wakeWordStatus,

    // Quick actions
    actionLoading,
    actionResult,
    indexToKb,
    sendToPaperless,
    handleSummarize,
    handleSendViaEmail,

    // Email dialog
    emailDialog,
    confirmSendViaEmail,
    cancelEmailDialog,

    // Actions
    speakText,
    handleFeedbackSubmit,
  }), [
    messages, loading, input, historyLoading, sendMessageInternal,
    sessionId, sidebarOpen, switchConversation, startNewChat, handleDeleteConversation,
    conversations, conversationsLoading,
    wsConnected,
    recording, audioLevel, silenceTimeRemaining, toggleRecording,
    useRag, toggleRag, selectedKnowledgeBase,
    attachments, uploading, uploadError, handleUploadDocument, removeAttachment, uploadStates,
    wakeWord, wakeWordStatus,
    actionLoading, actionResult, indexToKb, sendToPaperless, handleSummarize, handleSendViaEmail,
    emailDialog, confirmSendViaEmail, cancelEmailDialog,
    speakText, handleFeedbackSubmit,
  ]);

  return (
    <ChatContext.Provider value={value}>
      {children}
      {ConfirmDialogComponent}
    </ChatContext.Provider>
  );
}
