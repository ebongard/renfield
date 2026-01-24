import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Send, Mic, MicOff, Volume2, Loader, Ear, EarOff, Settings, BookOpen, ChevronDown, Menu } from 'lucide-react';
import apiClient from '../utils/axios';
import { useWakeWord } from '../hooks/useWakeWord';
import { WAKEWORD_CONFIG } from '../config/wakeword';
import ChatSidebar from '../components/ChatSidebar';
import { useChatSessions } from '../hooks/useChatSessions';

// LocalStorage key for current session
const SESSION_STORAGE_KEY = 'renfield_current_session';

export default function ChatPage() {
  const { t } = useTranslation();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [recording, setRecording] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [silenceTimeRemaining, setSilenceTimeRemaining] = useState(0);

  // Session management - restore from localStorage or create new
  const [sessionId, setSessionId] = useState(() => {
    return localStorage.getItem(SESSION_STORAGE_KEY) || null;
  });

  // Sidebar state
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);

  // Chat sessions hook for conversation list
  const {
    conversations,
    loading: conversationsLoading,
    refreshConversations,
    deleteConversation,
    loadConversationHistory,
    addConversation,
    updateConversationPreview
  } = useChatSessions();

  // RAG State
  const [useRag, setUseRag] = useState(false);
  const [knowledgeBases, setKnowledgeBases] = useState([]);
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState(null);
  const [ragSources, setRagSources] = useState([]);
  const [showRagSettings, setShowRagSettings] = useState(false);

  const messagesEndRef = useRef(null);
  const wsRef = useRef(null);
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const audioContextRef = useRef(null);
  const analyserRef = useRef(null);
  const silenceTimerRef = useRef(null);
  const animationFrameRef = useRef(null);
  const audioRef = useRef(null); // Für TTS Playback
  const lastInputChannelRef = useRef('text'); // Ref statt State - vermeidet Closure-Problem!
  const isStoppingRef = useRef(false); // Verhindert doppelte stopRecording() Aufrufe
  const lastAutoTTSTextRef = useRef(''); // Verhindert doppelte Auto-TTS für gleichen Text
  const autoTTSPendingRef = useRef(false); // Verhindert gleichzeitige Auto-TTS Anfragen

  // Wake word state
  const [wakeWordStatus, setWakeWordStatus] = useState('idle'); // idle | listening | activated | recording
  const [showWakeWordSettings, setShowWakeWordSettings] = useState(false);
  const wakeWordActivatedRef = useRef(false); // Track if current recording was triggered by wake word
  const wakeWordEnabledRef = useRef(false); // Ref to track wake word enabled state for async callbacks
  const audioContextUnlockedRef = useRef(null); // Shared AudioContext for TTS (pre-unlocked)

  // Handle wake word detection - triggers recording
  const handleWakeWordDetected = useCallback(async (keyword, score) => {
    console.log(`🎯 Wake word detected: ${keyword} (score: ${score.toFixed(2)})`);
    setWakeWordStatus('activated');
    wakeWordActivatedRef.current = true;

    // Play activation sound (optional)
    playActivationSound();

    // Small delay to let wake word audio finish
    await new Promise(r => setTimeout(r, WAKEWORD_CONFIG.activationDelayMs));

    // Start recording - uses existing startRecording function
    // We need to call it after component has mounted, so we use a ref
    if (startRecordingRef.current) {
      startRecordingRef.current();
    }
  }, []);

  // Handle speech end from wake word VAD
  const handleWakeWordSpeechEnd = useCallback(() => {
    console.log('🤫 Wake word VAD: Speech ended');
  }, []);

  // Handle wake word errors
  const handleWakeWordError = useCallback((error) => {
    console.error('🚨 Wake word error:', error);
    setWakeWordStatus('idle');
  }, []);

  // Initialize wake word hook
  const {
    isEnabled: wakeWordEnabled,
    isListening: wakeWordListening,
    isLoading: wakeWordLoading,
    isReady: wakeWordReady,
    isAvailable: wakeWordAvailable,
    lastDetection,
    error: wakeWordError,
    settings: wakeWordSettings,
    enable: enableWakeWord,
    disable: disableWakeWord,
    toggle: toggleWakeWord,
    pause: pauseWakeWord,
    resume: resumeWakeWord,
    setKeyword: setWakeWordKeyword,
    setThreshold: setWakeWordThreshold,
    availableKeywords,
  } = useWakeWord({
    onWakeWordDetected: handleWakeWordDetected,
    onSpeechEnd: handleWakeWordSpeechEnd,
    onError: handleWakeWordError,
  });

  // Ref to hold startRecording function for wake word callback
  const startRecordingRef = useRef(null);

  // Keep wakeWordEnabledRef in sync with state
  useEffect(() => {
    wakeWordEnabledRef.current = wakeWordEnabled;
  }, [wakeWordEnabled]);

  // Play activation sound when wake word is detected
  // Also unlocks AudioContext for later TTS playback
  const playActivationSound = useCallback(() => {
    try {
      // Reuse or create AudioContext (keeps it unlocked for TTS)
      if (!audioContextUnlockedRef.current || audioContextUnlockedRef.current.state === 'closed') {
        audioContextUnlockedRef.current = new (window.AudioContext || window.webkitAudioContext)();
        console.log('🔓 AudioContext created and unlocked for TTS');
      }
      const audioContext = audioContextUnlockedRef.current;

      // Resume if suspended
      if (audioContext.state === 'suspended') {
        audioContext.resume();
      }

      const oscillator = audioContext.createOscillator();
      const gainNode = audioContext.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(audioContext.destination);

      oscillator.frequency.value = 880; // A5 note
      oscillator.type = 'sine';
      gainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.2);

      oscillator.start(audioContext.currentTime);
      oscillator.stop(audioContext.currentTime + 0.2);
    } catch (e) {
      console.warn('Could not play activation sound:', e);
    }
  }, []);

  useEffect(() => {
    // If no session ID exists, create a new one
    if (!sessionId) {
      const newSessionId = `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
      setSessionId(newSessionId);
      localStorage.setItem(SESSION_STORAGE_KEY, newSessionId);
    }

    // WebSocket verbinden
    connectWebSocket();

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  // Load history when sessionId changes (and is from localStorage)
  useEffect(() => {
    const loadHistory = async () => {
      if (!sessionId) return;

      // Check if this is an existing conversation
      const existingConv = conversations.find(c => c.session_id === sessionId);
      if (existingConv && existingConv.message_count > 0 && messages.length === 0) {
        setHistoryLoading(true);
        try {
          const history = await loadConversationHistory(sessionId);
          if (history.length > 0) {
            setMessages(history.map(m => ({ role: m.role, content: m.content })));
          }
        } catch (err) {
          console.error('Failed to load conversation history:', err);
        } finally {
          setHistoryLoading(false);
        }
      }
    };

    loadHistory();
  }, [sessionId, conversations, loadConversationHistory]);

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // Load knowledge bases when RAG is enabled
  useEffect(() => {
    if (useRag && knowledgeBases.length === 0) {
      loadKnowledgeBases();
    }
  }, [useRag]);

  const loadKnowledgeBases = async () => {
    try {
      const response = await apiClient.get('/api/knowledge/bases');
      setKnowledgeBases(response.data);
    } catch (error) {
      console.error('Fehler beim Laden der Knowledge Bases:', error);
    }
  };

  const connectWebSocket = () => {
    const wsUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws';
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log('WebSocket verbunden');
      setWsConnected(true);
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      
      if (data.type === 'action') {
        // Action wurde ausgeführt - zeige Indikator
        console.log('Action ausgeführt:', data.intent, data.result);
        // Optional: Zeige kurze Notification dass Action ausgeführt wurde
      } else if (data.type === 'rag_context') {
        // RAG context info received
        console.log('RAG Context:', data.has_context ? 'found' : 'not found');
        if (!data.has_context) {
          setRagSources([]);
        }
      } else if (data.type === 'stream') {
        // Streaming-Antwort
        setMessages(prev => {
          const lastMsg = prev[prev.length - 1];
          if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
            return [
              ...prev.slice(0, -1),
              { ...lastMsg, content: lastMsg.content + data.content }
            ];
          } else {
            return [...prev, { role: 'assistant', content: data.content, streaming: true }];
          }
        });
      } else if (data.type === 'done') {
        // Stream beendet
        const ttsHandledByServer = data.tts_handled === true;

        setMessages(prev => {
          const lastMsg = prev[prev.length - 1];
          if (lastMsg && lastMsg.streaming) {
            const completedMessage = { ...lastMsg, streaming: false };

            // Auto-TTS wenn Input via Voice kam UND Server TTS nicht bereits gehandelt hat
            console.log('🔍 Prüfe Auto-TTS: Channel =', lastInputChannelRef.current, ', Role =', completedMessage.role, ', Pending =', autoTTSPendingRef.current, ', ServerHandled =', ttsHandledByServer);

            if (ttsHandledByServer) {
              console.log('🔊 TTS wurde vom Server an Ausgabegerät gesendet - lokale Wiedergabe übersprungen');

              // Resume wake word even though TTS was handled server-side
              // (we don't know when the external playback finishes, so resume immediately)
              if (wakeWordEnabledRef.current && wakeWordActivatedRef.current) {
                // Wait a bit for the audio to start playing on the external device
                setTimeout(() => {
                  console.log('▶️ Resuming wake word detection after server TTS...');
                  resumeWakeWord();
                  setWakeWordStatus('listening');
                  wakeWordActivatedRef.current = false;
                }, 3000); // 3 second delay to allow external playback
              }
            } else if (lastInputChannelRef.current === 'voice' && completedMessage.role === 'assistant') {
              // Prüfe ob bereits ein Auto-TTS Request läuft (verhindert Race Condition)
              if (autoTTSPendingRef.current) {
                console.log('⚠️  Auto-TTS übersprungen: Bereits ein Request aktiv');
              } else if (lastAutoTTSTextRef.current === completedMessage.content) {
                console.log('⚠️  Auto-TTS übersprungen: Gleicher Text bereits abgespielt');
              } else {
                console.log('🔊 Auto-playing TTS response (voice input detected)');
                autoTTSPendingRef.current = true; // Markiere als laufend
                lastAutoTTSTextRef.current = completedMessage.content; // Markiere Text als abgespielt

                // Play TTS nach kurzer Verzögerung (erlaubt DOM Update)
                setTimeout(() => {
                  speakText(completedMessage.content).finally(() => {
                    autoTTSPendingRef.current = false; // Reset nach Abschluss
                    console.log('🔄 TTS finally block - checking wake word resume...');
                    console.log('   wakeWordEnabledRef:', wakeWordEnabledRef.current);
                    console.log('   wakeWordActivatedRef:', wakeWordActivatedRef.current);

                    // Resume wake word after TTS playback completes (use ref to avoid stale closure)
                    if (wakeWordEnabledRef.current && wakeWordActivatedRef.current) {
                      console.log('▶️ Resuming wake word detection after TTS...');
                      resumeWakeWord();
                      setWakeWordStatus('listening');
                      wakeWordActivatedRef.current = false;
                    }
                  });
                }, 200);
              }
            } else {
              console.log('❌ Kein Auto-TTS: Channel ist', lastInputChannelRef.current);

              // Resume wake word if no TTS needed (use ref to avoid stale closure)
              if (wakeWordEnabledRef.current && wakeWordActivatedRef.current) {
                console.log('▶️ Resuming wake word detection (no TTS)...');
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
      }
    };

    ws.onclose = () => {
      console.log('WebSocket getrennt');
      setWsConnected(false);
      // Automatisch wieder verbinden nach 3 Sekunden
      setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = (error) => {
      console.error('WebSocket Fehler:', error);
    };

    wsRef.current = ws;
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  /**
   * Switch to an existing conversation
   */
  const switchConversation = async (newSessionId) => {
    if (newSessionId === sessionId) {
      setSidebarOpen(false);
      return;
    }

    setHistoryLoading(true);
    try {
      const history = await loadConversationHistory(newSessionId);
      setMessages(history.map(m => ({ role: m.role, content: m.content })));
      setSessionId(newSessionId);
      localStorage.setItem(SESSION_STORAGE_KEY, newSessionId);
      setSidebarOpen(false);
    } catch (err) {
      console.error('Failed to switch conversation:', err);
    } finally {
      setHistoryLoading(false);
    }
  };

  /**
   * Start a new chat session
   */
  const startNewChat = () => {
    const newId = `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    setSessionId(newId);
    setMessages([]);
    localStorage.setItem(SESSION_STORAGE_KEY, newId);
    setSidebarOpen(false);
  };

  /**
   * Handle conversation deletion with confirmation
   */
  const handleDeleteConversation = async (id) => {
    if (!window.confirm(t('chat.deleteConversation'))) {
      return;
    }

    const success = await deleteConversation(id);
    if (success && id === sessionId) {
      // If we deleted the active conversation, start a new one
      startNewChat();
    }
  };

  const sendMessage = async (text = input, fromVoice = false) => {
    if (!text.trim()) return;

    // Setze Input Channel basierend auf Eingabemethode
    console.log('📨 sendMessage aufgerufen mit fromVoice:', fromVoice);
    if (!fromVoice) {
      lastInputChannelRef.current = 'text';
      lastAutoTTSTextRef.current = ''; // Reset Auto-TTS Guard bei Text-Eingabe
      console.log('📝 Channel gesetzt auf: text');
    } else {
      console.log('📝 Channel bleibt: voice (fromVoice=true)');
    }
    // Wenn fromVoice=true, wurde channel bereits in startRecording() gesetzt

    const userMessage = { role: 'user', content: text };
    setMessages(prev => [...prev, userMessage]);
    setInput('');
    setLoading(true);

    // Update conversation list with this message as preview
    const previewText = text.length > 50 ? text.substring(0, 50) + '...' : text;
    // Add to conversation list if it's a new conversation
    addConversation({
      session_id: sessionId,
      preview: previewText,
      message_count: messages.length + 1,
      updated_at: new Date().toISOString(),
      created_at: new Date().toISOString()
    });

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      // WebSocket nutzen für Streaming (with session_id for conversation persistence)
      const message = {
        type: 'text',
        content: text,
        session_id: sessionId,
        use_rag: useRag,
        knowledge_base_id: selectedKnowledgeBase
      };
      wsRef.current.send(JSON.stringify(message));

      // Reset RAG sources for new message
      setRagSources([]);
    } else {
      // Fallback auf HTTP
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
  };

  const startRecording = async () => {
    lastInputChannelRef.current = 'voice'; // Markiere als Voice Input
    isStoppingRef.current = false; // Reset stopping flag
    lastAutoTTSTextRef.current = ''; // Reset Auto-TTS Guard für neue Aufnahme
    autoTTSPendingRef.current = false; // Reset Pending Flag
    console.log('📝 Channel gesetzt auf: voice');

    // Pause wake word listening while recording
    if (wakeWordEnabled) {
      console.log('⏸️ Pausing wake word detection for recording...');
      await pauseWakeWord();
    }
    setWakeWordStatus('recording');

    try {
      console.log('🎤 Starte Aufnahme mit Voice Activity Detection...');
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      console.log('✅ Mikrofon-Zugriff erhalten');
      console.log('📊 Stream Tracks:', stream.getTracks().map(t => ({
        kind: t.kind,
        enabled: t.enabled,
        muted: t.muted,
        readyState: t.readyState
      })));
      
      // MediaRecorder setup
      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      // Audio Context für Level-Monitoring
      try {
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        audioContextRef.current = audioContext;
        console.log('✅ AudioContext erstellt, State:', audioContext.state);

        // Resume AudioContext falls suspended
        if (audioContext.state === 'suspended') {
          await audioContext.resume();
          console.log('✅ AudioContext resumed, neuer State:', audioContext.state);
        }

        const source = audioContext.createMediaStreamSource(stream);
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = 512; // Größere FFT für bessere Erkennung
        analyser.smoothingTimeConstant = 0.3; // Weniger Glättung für schnellere Reaktion
        source.connect(analyser);
        analyserRef.current = analyser;
        
        console.log('✅ Analyser konfiguriert:', {
          fftSize: analyser.fftSize,
          frequencyBinCount: analyser.frequencyBinCount,
          smoothingTimeConstant: analyser.smoothingTimeConstant
        });
      } catch (audioError) {
        console.error('⚠️  AudioContext Fehler:', audioError);
        console.log('💡 Fahre ohne Audio-Level-Monitoring fort');
      }

      // Voice Activity Detection
      const bufferLength = analyserRef.current ? analyserRef.current.frequencyBinCount : 0;
      const dataArray = bufferLength > 0 ? new Uint8Array(bufferLength) : null;
      
      const SILENCE_THRESHOLD = 10; // Realistischer Threshold für RMS (war 3, zu niedrig!)
      const SILENCE_DURATION = 1500; // 1.5 Sekunden Stille
      const MIN_RECORDING_TIME = 800; // Mindestens 0.8 Sekunden
      
      let recordingStartTime = Date.now();
      let lastSoundTime = Date.now();
      let hasSoundDetected = false;
      let checkCount = 0;
      let isStillRecording = true; // Lokale Variable statt React State zu prüfen

      const checkAudioLevel = () => {
        if (!analyserRef.current || !dataArray) {
          // Kein Audio-Monitoring verfügbar - zeige statischen Level
          setAudioLevel(50); // Zeige 50% als Indikator dass aufgenommen wird

          if (isStillRecording) {
            animationFrameRef.current = requestAnimationFrame(checkAudioLevel);
          }
          return;
        }

        analyserRef.current.getByteFrequencyData(dataArray);
        
        // Berechne Audio-Level (RMS für bessere Genauigkeit)
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) {
          sum += dataArray[i] * dataArray[i];
        }
        const rms = Math.sqrt(sum / dataArray.length);
        const average = rms; // 0-255 Range
        
        setAudioLevel(Math.round(average));
        
        checkCount++;
        // Logging alle 15 Frames (ca. jede 250ms) für besseres Debugging
        if (checkCount % 15 === 0) {
          const silenceDurationNow = Date.now() - lastSoundTime;
          console.log('🎵 Audio-Level:', Math.round(average),
                      '| Threshold:', SILENCE_THRESHOLD,
                      '| Sound detected:', hasSoundDetected,
                      '| Silence:', Math.round(silenceDurationNow/1000), 'sec');
        }
        
        const currentTime = Date.now();
        const recordingTime = currentTime - recordingStartTime;
        
        // Erkenne Ton vs. Stille
        if (average > SILENCE_THRESHOLD) {
          lastSoundTime = currentTime;
          hasSoundDetected = true;
          setSilenceTimeRemaining(0); // Kein Countdown während Sprechen

          // Clear silence timer
          if (silenceTimerRef.current) {
            clearTimeout(silenceTimerRef.current);
            silenceTimerRef.current = null;
          }

          if (checkCount % 30 === 0) {
            console.log('🔊 Ton erkannt, Level:', Math.round(average));
          }
        } else {
          // Stille erkannt
          const silenceDuration = currentTime - lastSoundTime;

          // Berechne verbleibende Zeit bis Auto-Stop
          if (hasSoundDetected && recordingTime > MIN_RECORDING_TIME) {
            const remaining = Math.max(0, SILENCE_DURATION - silenceDuration);
            setSilenceTimeRemaining(remaining);
          } else {
            setSilenceTimeRemaining(0);
          }

          // Stoppe wenn alle Bedingungen erfüllt
          if (recordingTime > MIN_RECORDING_TIME &&
              hasSoundDetected &&
              silenceDuration > SILENCE_DURATION) {

            console.log('🤫 Stille erkannt für', Math.round(silenceDuration), 'ms - stoppe automatisch');
            console.log('📊 Recording Stats: Zeit:', Math.round(recordingTime), 'ms, Sound detected:', hasSoundDetected);
            isStillRecording = false; // Stoppe Loop
            setSilenceTimeRemaining(0);
            stopRecording();
            return;
          }
        }

        // Weiter monitoren
        if (isStillRecording) {
          animationFrameRef.current = requestAnimationFrame(checkAudioLevel);
        }
      };

      mediaRecorder.ondataavailable = (event) => {
        console.log('📊 Audio-Daten erhalten:', event.data.size, 'bytes');
        audioChunksRef.current.push(event.data);
      };

      mediaRecorder.onstop = async () => {
        console.log('🛑 ====== ONSTOP HANDLER GESTARTET ======');
        console.log('📊 Chunks erhalten:', audioChunksRef.current.length);
        console.log('📝 Channel in onstop:', lastInputChannelRef.current);

        // Cleanup
        if (animationFrameRef.current) {
          cancelAnimationFrame(animationFrameRef.current);
          console.log('✅ AnimationFrame gestoppt');
        }
        if (silenceTimerRef.current) {
          clearTimeout(silenceTimerRef.current);
          console.log('✅ Silence Timer gelöscht');
        }
        if (audioContextRef.current) {
          try {
            await audioContextRef.current.close();
            console.log('✅ AudioContext geschlossen');
          } catch (e) {
            console.warn('⚠️  AudioContext close error:', e);
          }
        }

        setAudioLevel(0);

        const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
        console.log('📦 Audio-Blob erstellt:', audioBlob.size, 'bytes, Type:', audioBlob.type);

        // Stream stoppen
        stream.getTracks().forEach(track => {
          track.stop();
          console.log('✅ Stream Track gestoppt:', track.kind);
        });

        // Nur verarbeiten wenn genug Daten
        if (audioBlob.size > 1000) {
          console.log('✅ Blob groß genug, starte Verarbeitung...');
          await processVoiceInput(audioBlob);
        } else {
          console.warn('⚠️  Audio zu kurz (', audioBlob.size, 'bytes), wird nicht verarbeitet');
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: t('voice.recordingTooShort')
          }]);
          setLoading(false);
        }

        // Reset stopping flag
        isStoppingRef.current = false;
        console.log('🛑 ====== ONSTOP HANDLER BEENDET ======');
      };

      mediaRecorder.start();
      setRecording(true);
      console.log('▶️ Aufnahme läuft... (automatischer Stopp bei Stille)');
      
      // Starte Audio-Level-Monitoring
      checkAudioLevel();
      
    } catch (error) {
      console.error('❌ Mikrofon-Fehler:', error);
      alert(t('voice.microphoneError') + ': ' + error.message);

      // Resume wake word on error
      if (wakeWordEnabled) {
        console.log('▶️ Resuming wake word detection after error...');
        resumeWakeWord();
        setWakeWordStatus('listening');
      }
    }
  };

  // Assign startRecording to ref for wake word callback
  startRecordingRef.current = startRecording;

  const stopRecording = () => {
    // Verhindere doppelte Aufrufe (Race Condition Protection)
    if (isStoppingRef.current) {
      console.warn('⚠️  stopRecording bereits in Ausführung, überspringe doppelten Aufruf');
      return;
    }

    if (!mediaRecorderRef.current) {
      console.warn('⚠️  stopRecording: mediaRecorderRef ist null');
      return;
    }

    // Prüfe MediaRecorder State direkt (nicht React State wegen Timing)
    const mrState = mediaRecorderRef.current.state;
    console.log('⏹️ Stoppe Aufnahme...');
    console.log('📝 Aktueller Channel bei Stop:', lastInputChannelRef.current);
    console.log('📊 MediaRecorder State:', mrState);

    // Nur stoppen wenn MediaRecorder in 'recording' state ist
    if (mrState !== 'recording') {
      console.warn('⚠️  MediaRecorder nicht in recording state:', mrState);
      setRecording(false);
      isStoppingRef.current = false;
      return;
    }

    // Markiere als "stopping" um doppelte Aufrufe zu verhindern
    isStoppingRef.current = true;

    try {
      mediaRecorderRef.current.stop();
      setRecording(false);
      console.log('✅ stop() aufgerufen, warte auf onstop handler...');
    } catch (error) {
      console.error('❌ Fehler beim Stoppen:', error);
      setRecording(false);
      setLoading(false);
      isStoppingRef.current = false;
    }
  };

  const processVoiceInput = async (audioBlob) => {
    console.log('🔄 Verarbeite Spracheingabe...');
    setLoading(true);

    try {
      // Audio zu Text (STT)
      const formData = new FormData();
      formData.append('audio', audioBlob, 'recording.webm');
      
      console.log('📤 Sende Audio an Backend...');
      const sttResponse = await apiClient.post('/api/voice/stt', formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      });

      console.log('✅ STT Response:', sttResponse.data);
      const transcribedText = sttResponse.data.text;
      
      if (!transcribedText || transcribedText.trim() === '') {
        throw new Error(t('voice.noSpeechRecognized'));
      }
      
      console.log('📝 Transkribierter Text:', transcribedText);

      // Text senden (als Voice Input markieren)
      await sendMessage(transcribedText, true); // fromVoice=true
    } catch (error) {
      console.error('❌ Spracheingabe Fehler:', error);
      console.error('Error Details:', error.response?.data);
      
      let errorMessage = t('voice.processingError');
      if (error.response?.data?.detail) {
        errorMessage += ' (' + error.response.data.detail + ')';
      }

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: errorMessage
      }]);
    } finally {
      setLoading(false);
    }
  };

  const speakText = async (text) => {
    try {
      // Stoppe aktuelles Audio falls vorhanden
      if (audioRef.current) {
        if (audioRef.current.stop) {
          audioRef.current.stop();
        } else if (audioRef.current.pause) {
          audioRef.current.pause();
        }
        audioRef.current = null;
      }

      // Validierung
      if (!text || text.trim().length === 0) {
        console.warn('⚠️  Skipping TTS for empty message');
        return;
      }

      // Warne bei sehr langen Nachrichten
      if (text.length > 500) {
        console.warn('⚠️  Long message detected, TTS may take time:', text.length, 'chars');
      }

      console.log('🔊 Requesting TTS for:', text.substring(0, 50) + '...');

      const response = await apiClient.post('/api/voice/tts',
        { text },
        { responseType: 'arraybuffer' }  // Use arraybuffer for AudioContext decoding
      );

      // Prüfe ob Response valide (detect Piper unavailable)
      if (response.data.byteLength < 100) {
        throw new Error('TTS response too small (Piper likely not available)');
      }

      // Use pre-unlocked AudioContext if available, otherwise create new one
      let audioContext = audioContextUnlockedRef.current;
      if (!audioContext || audioContext.state === 'closed') {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        audioContextUnlockedRef.current = audioContext;
        console.log('🔓 Created new AudioContext for TTS');
      }

      // Resume if suspended
      if (audioContext.state === 'suspended') {
        await audioContext.resume();
        console.log('▶️ AudioContext resumed');
      }

      // Decode audio data
      const audioBuffer = await audioContext.decodeAudioData(response.data.slice(0));
      console.log('✅ Audio decoded:', audioBuffer.duration.toFixed(2), 'seconds');

      // Create source and play
      const source = audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(audioContext.destination);

      // Store source for potential stopping
      audioRef.current = source;

      // Return a promise that resolves when playback ends
      return new Promise((resolve) => {
        source.onended = () => {
          audioRef.current = null;
          console.log('✅ TTS playback completed');
          resolve();
        };

        source.start(0);
        console.log('▶️ TTS playback started');
      });

    } catch (error) {
      console.error('❌ TTS Fehler:', error);

      // Einmalige Warnung (don't spam)
      if (!window._ttsErrorShown) {
        console.warn('⚠️  TTS nicht verfügbar. Piper im Backend prüfen.');
        window._ttsErrorShown = true;
      }
    }
  };

  return (
    <div className="h-[calc(100vh-8rem)] flex">
      {/* Mobile Sidebar Toggle Button */}
      <button
        onClick={() => setSidebarOpen(true)}
        className="fixed bottom-24 left-4 z-10 md:hidden p-3 bg-primary-600 hover:bg-primary-700 text-white rounded-full shadow-lg transition-colors"
        aria-label={t('chat.openConversations')}
      >
        <Menu className="w-5 h-5" aria-hidden="true" />
      </button>

      {/* Sidebar */}
      <ChatSidebar
        conversations={conversations}
        activeSessionId={sessionId}
        onSelectConversation={switchConversation}
        onNewChat={startNewChat}
        onDeleteConversation={handleDeleteConversation}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        loading={conversationsLoading}
      />

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <div className="card mb-4 mx-4 mt-4 md:mx-0 md:mt-0">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-gray-900 dark:text-white">{t('chat.title')}</h1>
              <p className="text-gray-500 dark:text-gray-400">{t('chat.subtitle')}</p>
            </div>
          <div className="flex items-center space-x-4">
            {/* Wake Word Controls */}
            <div className="flex items-center space-x-2">
              <button
                onClick={toggleWakeWord}
                disabled={wakeWordLoading || recording}
                className={`p-2 rounded-lg transition-all ${
                  wakeWordEnabled
                    ? 'bg-green-600 hover:bg-green-700 text-white'
                    : wakeWordError
                      ? 'bg-red-100 hover:bg-red-200 text-red-600 dark:bg-red-900/50 dark:hover:bg-red-800/50 dark:text-red-300'
                      : 'bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300'
                } ${wakeWordLoading ? 'opacity-50 cursor-wait' : ''}`}
                title={wakeWordError
                  ? `${t('wakeword.notAvailable')}: ${wakeWordError.message}`
                  : wakeWordEnabled
                    ? t('wakeword.listening', { keyword: availableKeywords.find(k => k.id === wakeWordSettings.keyword)?.label || 'Hey Jarvis' })
                    : t('wakeword.enable')
                }
              >
                {wakeWordLoading ? (
                  <Loader className="w-4 h-4 animate-spin" />
                ) : wakeWordEnabled ? (
                  <Ear className="w-4 h-4" />
                ) : (
                  <EarOff className="w-4 h-4" />
                )}
              </button>

              {/* Wake Word Settings Button */}
              {wakeWordEnabled && (
                <button
                  onClick={() => setShowWakeWordSettings(!showWakeWordSettings)}
                  className="p-2 rounded-lg bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300"
                  title={t('wakeword.settings')}
                >
                  <Settings className="w-4 h-4" />
                </button>
              )}
            </div>

            {/* Connection Status */}
            <div className="flex items-center space-x-2">
              <div className={`w-3 h-3 rounded-full ${wsConnected ? 'bg-green-500' : 'bg-red-500'}`} />
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {wsConnected ? t('common.connected') : t('common.disconnected')}
              </span>
            </div>
          </div>
        </div>

        {/* Wake Word Error Message */}
        {wakeWordError && !wakeWordEnabled && (
          <div className="mt-3 flex items-center px-3 py-2 bg-red-100 dark:bg-red-900/30 rounded-lg border border-red-300 dark:border-red-700/50">
            <div className="flex items-center space-x-2">
              <div className="w-2 h-2 rounded-full bg-red-500" />
              <span className="text-sm text-red-700 dark:text-red-300">
                {wakeWordError.name === 'BrowserNotSupportedError'
                  ? t('wakeword.browserNotSupported')
                  : <>{t('wakeword.notAvailable')}. Run: <code className="bg-red-200 dark:bg-red-900/50 px-1 rounded">docker compose up -d --build</code></>
                }
              </span>
            </div>
          </div>
        )}

        {/* Wake Word Listening Indicator */}
        {wakeWordEnabled && !recording && (
          <div className="mt-3 flex items-center justify-between px-3 py-2 bg-green-100 dark:bg-green-900/30 rounded-lg border border-green-300 dark:border-green-700/50">
            <div className="flex items-center space-x-2">
              <div className={`w-2 h-2 rounded-full ${wakeWordListening ? 'bg-green-500 animate-pulse' : 'bg-yellow-500'}`} />
              <span className="text-sm text-green-700 dark:text-green-300">
                {wakeWordListening
                  ? t('wakeword.listening', { keyword: availableKeywords.find(k => k.id === wakeWordSettings.keyword)?.label || 'Hey Jarvis' })
                  : wakeWordStatus === 'activated'
                    ? t('wakeword.detected')
                    : t('wakeword.paused')
                }
              </span>
            </div>
            {lastDetection && (
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {t('wakeword.lastDetection')}: {lastDetection.keyword} ({(lastDetection.score * 100).toFixed(0)}%)
              </span>
            )}
          </div>
        )}

        {/* Wake Word Settings Dropdown */}
        {showWakeWordSettings && (
          <div className="mt-3 p-4 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
            <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-3">{t('wakeword.settings')}</h3>

            <div className="space-y-3">
              {/* Keyword Selection */}
              <div>
                <label className="text-xs text-gray-500 dark:text-gray-400 mb-1 block">{t('wakeword.keyword')}</label>
                <select
                  value={wakeWordSettings.keyword}
                  onChange={(e) => setWakeWordKeyword(e.target.value)}
                  className="w-full bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-white text-sm rounded-lg px-3 py-2 border border-gray-300 dark:border-gray-600 focus:border-primary-500 focus:outline-none"
                >
                  {availableKeywords.map(kw => (
                    <option key={kw.id} value={kw.id}>{kw.label}</option>
                  ))}
                </select>
              </div>

              {/* Threshold Slider */}
              <div>
                <label className="text-xs text-gray-500 dark:text-gray-400 mb-1 block">
                  {t('wakeword.sensitivity')}: {(wakeWordSettings.threshold * 100).toFixed(0)}%
                </label>
                <input
                  type="range"
                  min="0.3"
                  max="0.8"
                  step="0.05"
                  value={wakeWordSettings.threshold}
                  onChange={(e) => setWakeWordThreshold(parseFloat(e.target.value))}
                  className="w-full accent-primary-600"
                />
                <div className="flex justify-between text-xs text-gray-500">
                  <span>{t('wakeword.moreSensitive')}</span>
                  <span>{t('wakeword.lessFalsePositives')}</span>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Messages */}
      <div
        className="flex-1 overflow-y-auto card space-y-4 mb-4 mx-4 md:mx-0"
        role="log"
        aria-live="polite"
        aria-label={t('chat.conversations')}
        aria-relevant="additions"
      >
        {/* History Loading State */}
        {historyLoading && (
          <div className="flex items-center justify-center py-8">
            <Loader className="w-6 h-6 text-gray-500 dark:text-gray-400 animate-spin mr-2" aria-hidden="true" />
            <span className="text-gray-500 dark:text-gray-400">{t('chat.loadingConversation')}</span>
          </div>
        )}

        {!historyLoading && messages.length === 0 && (
          <div className="text-center py-12">
            <p className="text-gray-500 dark:text-gray-400 mb-4">{t('chat.startConversation')}</p>
            <p className="text-sm text-gray-400 dark:text-gray-500">
              {t('chat.useTextOrMic')}
            </p>
          </div>
        )}

        {messages.map((message, index) => (
          <div
            key={index}
            className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
            role="article"
            aria-label={message.role === 'user' ? t('chat.yourMessage') : t('chat.assistantResponse')}
          >
            <div
              className={`max-w-[70%] px-4 py-2 rounded-lg ${
                message.role === 'user'
                  ? 'bg-primary-600 text-white'
                  : 'bg-gray-200 text-gray-900 dark:bg-gray-700 dark:text-gray-100'
              }`}
            >
              <p className="whitespace-pre-wrap">{message.content}</p>

              {message.role === 'assistant' && !message.streaming && (
                <button
                  onClick={() => speakText(message.content)}
                  className="mt-2 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-white flex items-center space-x-1"
                  aria-label={t('chat.readAloud')}
                >
                  <Volume2 className="w-3 h-3" aria-hidden="true" />
                  <span>{t('chat.readAloud')}</span>
                </button>
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex justify-start" role="status" aria-label="Renfield denkt nach">
            <div className="bg-gray-200 dark:bg-gray-700 px-4 py-2 rounded-lg">
              <Loader className="w-5 h-5 animate-spin text-gray-500 dark:text-gray-400" aria-hidden="true" />
              <span className="sr-only">{t('chat.thinkingStatus')}</span>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="card mx-4 mb-4 md:mx-0 md:mb-0">
        {/* RAG Toggle */}
        <div className="flex items-center justify-between mb-3 pb-3 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center space-x-3">
            <button
              onClick={() => setUseRag(!useRag)}
              className={`flex items-center space-x-2 px-3 py-1.5 rounded-lg text-sm transition-colors ${
                useRag
                  ? 'bg-primary-100 text-primary-700 border border-primary-300 dark:bg-primary-600/30 dark:text-primary-300 dark:border-primary-500/50'
                  : 'bg-gray-200 text-gray-600 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-400 dark:hover:bg-gray-600'
              }`}
              title={useRag ? t('rag.disableKnowledge') : t('rag.enableKnowledge')}
            >
              <BookOpen className="w-4 h-4" />
              <span>{t('rag.knowledge')}</span>
            </button>

            {useRag && (
              <div className="relative">
                <button
                  onClick={() => setShowRagSettings(!showRagSettings)}
                  className="flex items-center space-x-1 px-3 py-1.5 bg-gray-200 dark:bg-gray-700 rounded-lg text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors"
                >
                  <span>
                    {selectedKnowledgeBase
                      ? knowledgeBases.find(kb => kb.id === selectedKnowledgeBase)?.name || t('common.all')
                      : t('rag.allDocuments')}
                  </span>
                  <ChevronDown className={`w-4 h-4 transition-transform ${showRagSettings ? 'rotate-180' : ''}`} />
                </button>

                {showRagSettings && (
                  <div className="absolute bottom-full left-0 mb-2 w-48 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 shadow-lg z-10">
                    <div className="p-2">
                      <button
                        onClick={() => {
                          setSelectedKnowledgeBase(null);
                          setShowRagSettings(false);
                        }}
                        className={`w-full text-left px-3 py-2 rounded text-sm ${
                          selectedKnowledgeBase === null
                            ? 'bg-primary-100 text-primary-700 dark:bg-primary-600/30 dark:text-primary-300'
                            : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
                        }`}
                      >
                        {t('rag.allDocuments')}
                      </button>
                      {knowledgeBases.map(kb => (
                        <button
                          key={kb.id}
                          onClick={() => {
                            setSelectedKnowledgeBase(kb.id);
                            setShowRagSettings(false);
                          }}
                          className={`w-full text-left px-3 py-2 rounded text-sm ${
                            selectedKnowledgeBase === kb.id
                              ? 'bg-primary-100 text-primary-700 dark:bg-primary-600/30 dark:text-primary-300'
                              : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
                          }`}
                        >
                          {kb.name}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {useRag && (
            <span className="text-xs text-gray-500">
              {t('rag.searchesInDocuments')}
            </span>
          )}
        </div>

        {/* Audio Waveform Visualizer während der Aufnahme */}
        {recording && (
          <div className="mb-4 p-4 bg-gradient-to-br from-gray-100/80 to-gray-200/80 dark:from-gray-800/80 dark:to-gray-900/80 rounded-xl border border-gray-300/50 dark:border-gray-700/50 backdrop-blur-sm">
            {/* Header mit Status und Countdown */}
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center space-x-2">
                <div className="w-2.5 h-2.5 bg-red-500 rounded-full animate-pulse"></div>
                <span className="text-sm font-medium text-gray-900 dark:text-white">
                  {audioLevel > 10 ? t('voice.speechDetected') : silenceTimeRemaining > 0 ? t('voice.silenceDetected') : t('voice.listening')}
                </span>
              </div>

              {/* Countdown Timer */}
              {silenceTimeRemaining > 0 && (
                <div className="flex items-center space-x-2 px-3 py-1 bg-yellow-100 dark:bg-yellow-500/20 rounded-full border border-yellow-300 dark:border-yellow-500/30">
                  <div className="w-1.5 h-1.5 bg-yellow-500 dark:bg-yellow-400 rounded-full animate-pulse"></div>
                  <span className="text-xs font-mono text-yellow-700 dark:text-yellow-300">
                    {t('voice.autoStopIn', { seconds: (silenceTimeRemaining / 1000).toFixed(1) })}
                  </span>
                </div>
              )}
            </div>

            {/* Wellenform-Visualisierung */}
            <div className="flex items-center justify-center space-x-1.5 h-16 mb-3">
              {[0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => {
                // Berechne Höhe basierend auf audioLevel mit Variation für Welleneffekt
                const variation = Math.sin((Date.now() / 100) + i) * 0.3 + 0.7;
                const baseHeight = Math.max(10, audioLevel) * variation;
                const height = Math.min(100, baseHeight);

                // Farbe basierend auf Level
                const colorClass = audioLevel > 50 ? 'bg-green-500' :
                                   audioLevel > 10 ? 'bg-primary-500' :
                                   'bg-gray-400 dark:bg-gray-600';

                return (
                  <div
                    key={i}
                    className={`w-2 rounded-full transition-all duration-150 ease-out ${colorClass}`}
                    style={{
                      height: `${height}%`,
                      opacity: audioLevel > 5 ? 1 : 0.3
                    }}
                  />
                );
              })}
            </div>

            {/* Info Text */}
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-500 dark:text-gray-400">
                {t('voice.level')}: {audioLevel} / 10
              </span>
              <span className="text-gray-400 dark:text-gray-500">
                {t('voice.clickToStop')}
              </span>
            </div>
          </div>
        )}
        
        <div className="flex items-center space-x-2">
          <label htmlFor="chat-input" className="sr-only">{t('chat.placeholder')}</label>
          <input
            id="chat-input"
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage(input, false);
              }
            }}
            placeholder={t('chat.placeholder')}
            className="input flex-1"
            disabled={loading || recording}
            aria-describedby={loading ? 'chat-loading-hint' : undefined}
          />
          {loading && <span id="chat-loading-hint" className="sr-only">{t('chat.processingMessage')}</span>}

          <button
            onClick={recording ? stopRecording : startRecording}
            className={`p-3 rounded-lg transition-colors ${
              recording
                ? 'bg-red-600 hover:bg-red-700 text-white animate-pulse'
                : 'bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300'
            }`}
            disabled={loading}
            aria-label={recording ? t('voice.stopRecording') : t('voice.startRecording')}
            aria-pressed={recording}
          >
            {recording ? <MicOff className="w-5 h-5" aria-hidden="true" /> : <Mic className="w-5 h-5" aria-hidden="true" />}
          </button>

          <button
            onClick={() => sendMessage(input, false)}
            disabled={loading || !input.trim()}
            className="btn btn-primary"
            aria-label={t('chat.sendMessage')}
          >
            <Send className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>
      </div>
      </div>{/* End Main Chat Area */}
    </div>
  );
}
