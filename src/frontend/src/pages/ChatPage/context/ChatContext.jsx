import React, { createContext, useContext, useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import apiClient from '../../../utils/axios';
import { debug } from '../../../utils/debug';
import { useWakeWord } from '../../../hooks/useWakeWord';
import { WAKEWORD_CONFIG } from '../../../config/wakeword';
import { useChatSessions } from '../../../hooks/useChatSessions';
import { useChatWebSocket, useAudioRecording, useDocumentUpload, useQuickActions } from '../hooks';
import { useConfirmDialog } from '../../../components/ConfirmDialog';

const SESSION_STORAGE_KEY = 'renfield_current_session';

const ChatContext = createContext(null);

export function useChatContext() {
  const context = useContext(ChatContext);
  if (!context) throw new Error('useChatContext must be used within ChatProvider');
  return context;
}

export function ChatProvider({ children }) {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  // Message state
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [input, setInput] = useState('');

  // Session management
  const [sessionId, setSessionId] = useState(() => {
    return localStorage.getItem(SESSION_STORAGE_KEY) || null;
  });

  // Sidebar state
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);

  // RAG State
  const [useRag, setUseRag] = useState(false);
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState(null);
  const [ragSources, setRagSources] = useState([]);

  // Document upload state
  const [attachments, setAttachments] = useState([]);

  // Wake word state
  const [wakeWordStatus, setWakeWordStatus] = useState('idle');
  const wakeWordActivatedRef = useRef(false);
  const wakeWordEnabledRef = useRef(false);
  const audioContextUnlockedRef = useRef(null);

  // Voice input tracking
  const lastInputChannelRef = useRef('text');
  const lastAutoTTSTextRef = useRef('');
  const autoTTSPendingRef = useRef(false);

  // TTS audio ref
  const audioRef = useRef(null);

  // Intent feedback tracking
  const lastUserQueryRef = useRef('');
  const lastIntentInfoRef = useRef(null);

  // Chat sessions hook
  const {
    conversations,
    loading: conversationsLoading,
    refreshConversations,
    deleteConversation,
    loadConversationHistory,
    addConversation,
    updateConversationPreview
  } = useChatSessions();

  // Play activation sound when wake word is detected
  const playActivationSound = useCallback(() => {
    try {
      if (!audioContextUnlockedRef.current || audioContextUnlockedRef.current.state === 'closed') {
        audioContextUnlockedRef.current = new (window.AudioContext || window.webkitAudioContext)();
        debug.log('AudioContext created and unlocked for TTS');
      }
      const audioContext = audioContextUnlockedRef.current;

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
  }, []);

  // Speak text using TTS
  const speakText = useCallback(async (text) => {
    try {
      if (audioRef.current) {
        if (audioRef.current.stop) {
          audioRef.current.stop();
        } else if (audioRef.current.pause) {
          audioRef.current.pause();
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

      const response = await apiClient.post('/api/voice/tts',
        { text },
        { responseType: 'arraybuffer' }
      );

      if (response.data.byteLength < 100) {
        throw new Error('TTS response too small (Piper likely not available)');
      }

      let audioContext = audioContextUnlockedRef.current;
      if (!audioContext || audioContext.state === 'closed') {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        audioContextUnlockedRef.current = audioContext;
        debug.log('Created new AudioContext for TTS');
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

      return new Promise((resolve) => {
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

      if (!window._ttsErrorShown) {
        console.warn('TTS not available. Check Piper in backend.');
        window._ttsErrorShown = true;
      }
    }
  }, []);

  // Ref for startRecording function (used by wake word callback)
  const startRecordingRef = useRef(null);

  // Handle wake word detection
  const handleWakeWordDetected = useCallback(async (keyword, score) => {
    debug.log(`Wake word detected: ${keyword} (score: ${score.toFixed(2)})`);
    setWakeWordStatus('activated');
    wakeWordActivatedRef.current = true;

    playActivationSound();

    await new Promise(r => setTimeout(r, WAKEWORD_CONFIG.activationDelayMs));

    if (startRecordingRef.current) {
      startRecordingRef.current();
    }
  }, [playActivationSound]);

  const handleWakeWordSpeechEnd = useCallback(() => {
    debug.log('Wake word VAD: Speech ended');
  }, []);

  const handleWakeWordError = useCallback((error) => {
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
  const handleAction = useCallback((data) => {
    if (data.intent) {
      lastIntentInfoRef.current = {
        intent: data.intent?.intent || data.intent,
        confidence: data.intent?.confidence || 0,
      };
    }
  }, []);

  // Handle proactive feedback request from backend
  const handleIntentFeedbackRequest = useCallback((data) => {
    setMessages(prev => {
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
  const handleFeedbackSubmit = useCallback(async (messageText, feedbackType, originalValue, correctedValue) => {
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
  const handleStreamDone = useCallback((data) => {
    const ttsHandledByServer = data.tts_handled === true;

    setMessages(prev => {
      const lastMsg = prev[prev.length - 1];
      if (lastMsg && lastMsg.streaming) {
        const intentInfo = data.intent ? {
          intent: data.intent.intent,
          confidence: data.intent.confidence || 0,
        } : lastIntentInfoRef.current;

        const completedMessage = {
          ...lastMsg,
          streaming: false,
          intentInfo: intentInfo || undefined,
          userQuery: lastUserQueryRef.current || undefined,
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
  const handleStreamChunk = useCallback((content) => {
    setMessages(prev => {
      const lastMsg = prev[prev.length - 1];
      if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
        return [
          ...prev.slice(0, -1),
          { ...lastMsg, content: lastMsg.content + content }
        ];
      } else {
        return [...prev, { role: 'assistant', content: content, streaming: true }];
      }
    });
  }, []);

  // Handle RAG context
  const handleRagContext = useCallback((data) => {
    if (!data.has_context) {
      setRagSources([]);
    }
  }, []);

  // WebSocket hook
  const { wsConnected, sendMessage: wsSendMessage, isReady } = useChatWebSocket({
    onStreamChunk: handleStreamChunk,
    onStreamDone: handleStreamDone,
    onAction: handleAction,
    onRagContext: handleRagContext,
    onIntentFeedbackRequest: handleIntentFeedbackRequest,
  });

  // Handle transcription from audio recording
  const handleTranscription = useCallback((text) => {
    debug.log('Transcription received:', text);
    sendMessageInternal(text, true);
  }, []);

  // Handle recording error
  const handleRecordingError = useCallback((errorMessage) => {
    setMessages(prev => [...prev, {
      role: 'assistant',
      content: errorMessage
    }]);
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
    stopRecording,
    toggleRecording,
  } = useAudioRecording({
    onTranscription: handleTranscription,
    onError: handleRecordingError,
    onRecordingStart: handleRecordingStart,
    onRecordingStop: handleRecordingStop,
  });

  // Document upload hook
  const { uploading, uploadError, uploadDocument: doUpload } = useDocumentUpload();

  const handleUploadDocument = useCallback(async (file) => {
    if (!sessionId) return;
    const result = await doUpload(file, sessionId);
    if (result) {
      setAttachments(prev => [...prev, result]);
    }
  }, [sessionId, doUpload]);

  const removeAttachment = useCallback((id) => {
    setAttachments(prev => prev.filter(a => a.id !== id));
  }, []);

  // Quick actions hook
  const { actionLoading, actionResult, clearResult, indexToKb, sendToPaperless } = useQuickActions();

  // Assign startRecording to ref for wake word callback
  startRecordingRef.current = startRecording;

  // Internal send message function
  const sendMessageInternal = useCallback(async (text, fromVoice = false) => {
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
      .filter(a => a.status === 'completed')
      .map(a => a.id);

    const userMessage = {
      role: 'user',
      content: text,
      ...(currentAttachments.length > 0 && { attachments: currentAttachments }),
    };
    setMessages(prev => [...prev, userMessage]);
    setInput('');
    setAttachments([]);
    setLoading(true);

    const previewText = text.length > 50 ? text.substring(0, 50) + '...' : text;
    addConversation({
      session_id: sessionId,
      preview: previewText,
      message_count: messages.length + 1,
      updated_at: new Date().toISOString(),
      created_at: new Date().toISOString()
    });

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
        const response = await apiClient.post('/api/chat/send', {
          message: text,
          session_id: sessionId
        });

        setMessages(prev => [...prev, {
          role: 'assistant',
          content: response.data.message
        }]);
      } catch (error) {
        console.error('Chat error:', error);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: t('errors.couldNotProcess')
        }]);
      } finally {
        setLoading(false);
      }
    }
  }, [sessionId, messages.length, useRag, selectedKnowledgeBase, isReady, wsSendMessage, addConversation, attachments, t]);

  // Summarize handler (must be after sendMessageInternal)
  const handleSummarize = useCallback((uploadId) => {
    let filename = null;
    for (const msg of messages) {
      const att = msg.attachments?.find(a => a.id === uploadId);
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

      const existingConv = conversations.find(c => c.session_id === sessionId);
      if (existingConv && existingConv.message_count > 0 && messages.length === 0) {
        setHistoryLoading(true);
        try {
          const history = await loadConversationHistory(sessionId);
          if (history.length > 0) {
            setMessages(history.map(m => ({
              role: m.role,
              content: m.content,
              ...(m.attachments?.length > 0 && { attachments: m.attachments }),
            })));
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
  const switchConversation = useCallback(async (newSessionId) => {
    if (newSessionId === sessionId) {
      setSidebarOpen(false);
      return;
    }

    setHistoryLoading(true);
    try {
      const history = await loadConversationHistory(newSessionId);
      setMessages(history.map(m => ({
        role: m.role,
        content: m.content,
        ...(m.attachments?.length > 0 && { attachments: m.attachments }),
      })));
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
  const handleDeleteConversation = useCallback(async (id) => {
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
    setUseRag(prev => !prev);
  }, []);

  const value = useMemo(() => ({
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
    attachments, uploading, uploadError, handleUploadDocument, removeAttachment,
    wakeWord, wakeWordStatus,
    actionLoading, actionResult, indexToKb, sendToPaperless, handleSummarize,
    speakText, handleFeedbackSubmit,
  ]);

  return (
    <ChatContext.Provider value={value}>
      {children}
      {ConfirmDialogComponent}
    </ChatContext.Provider>
  );
}
