import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Send, Mic, MicOff, Volume2, Loader, Ear, EarOff, Settings } from 'lucide-react';
import apiClient from '../utils/axios';
import { useWakeWord } from '../hooks/useWakeWord';
import { WAKEWORD_CONFIG } from '../config/wakeword';

export default function ChatPage() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [recording, setRecording] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [silenceTimeRemaining, setSilenceTimeRemaining] = useState(0);

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

  // Play activation sound when wake word is detected
  const playActivationSound = useCallback(() => {
    try {
      // Create a simple beep using Web Audio API
      const audioContext = new (window.AudioContext || window.webkitAudioContext)();
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
    // Session ID generieren
    const newSessionId = `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    setSessionId(newSessionId);

    // WebSocket verbinden
    connectWebSocket();

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

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
        setMessages(prev => {
          const lastMsg = prev[prev.length - 1];
          if (lastMsg && lastMsg.streaming) {
            const completedMessage = { ...lastMsg, streaming: false };

            // Auto-TTS wenn Input via Voice kam
            console.log('🔍 Prüfe Auto-TTS: Channel =', lastInputChannelRef.current, ', Role =', completedMessage.role, ', Pending =', autoTTSPendingRef.current);

            if (lastInputChannelRef.current === 'voice' && completedMessage.role === 'assistant') {
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

                    // Resume wake word after TTS playback completes
                    if (wakeWordEnabled && wakeWordActivatedRef.current) {
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

              // Resume wake word if no TTS needed
              if (wakeWordEnabled && wakeWordActivatedRef.current) {
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

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      // WebSocket nutzen für Streaming
      wsRef.current.send(JSON.stringify({
        type: 'text',
        content: text
      }));
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
        console.error('Chat Fehler:', error);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: 'Entschuldigung, es gab einen Fehler bei der Verarbeitung deiner Anfrage.'
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
            content: 'Aufnahme war zu kurz. Bitte spreche mindestens 1 Sekunde.'
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
      alert('Konnte nicht auf das Mikrofon zugreifen: ' + error.message);

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
        throw new Error('Keine Sprache erkannt');
      }
      
      console.log('📝 Transkribierter Text:', transcribedText);

      // Text senden (als Voice Input markieren)
      await sendMessage(transcribedText, true); // fromVoice=true
    } catch (error) {
      console.error('❌ Spracheingabe Fehler:', error);
      console.error('Error Details:', error.response?.data);
      
      let errorMessage = 'Entschuldigung, ich konnte die Spracheingabe nicht verarbeiten.';
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
        audioRef.current.pause();
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
        { responseType: 'blob' }
      );

      // Prüfe ob Response valide (detect Piper unavailable)
      if (response.data.size < 100) {
        throw new Error('TTS response too small (Piper likely not available)');
      }

      const audioUrl = URL.createObjectURL(response.data);
      const audio = new Audio(audioUrl);
      audioRef.current = audio;

      // Cleanup bei Ende
      audio.onended = () => {
        URL.revokeObjectURL(audioUrl);
        audioRef.current = null;
        console.log('✅ TTS playback completed');
      };

      audio.onerror = (e) => {
        console.error('❌ Audio playback error:', e);
        URL.revokeObjectURL(audioUrl);
        audioRef.current = null;
      };

      await audio.play();
      console.log('▶️ TTS playback started');

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
    <div className="h-[calc(100vh-12rem)] flex flex-col">
      {/* Header */}
      <div className="card mb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-white">Chat</h1>
            <p className="text-gray-400">Unterhalte dich mit Renfield</p>
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
                      ? 'bg-red-900/50 hover:bg-red-800/50 text-red-300'
                      : 'bg-gray-700 hover:bg-gray-600 text-gray-300'
                } ${wakeWordLoading ? 'opacity-50 cursor-wait' : ''}`}
                title={wakeWordError
                  ? `Wake word not available: ${wakeWordError.message}`
                  : wakeWordEnabled
                    ? `Wake word active - say "${availableKeywords.find(k => k.id === wakeWordSettings.keyword)?.label || 'Hey Jarvis'}"`
                    : 'Enable wake word detection'
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
                  className="p-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300"
                  title="Wake word settings"
                >
                  <Settings className="w-4 h-4" />
                </button>
              )}
            </div>

            {/* Connection Status */}
            <div className="flex items-center space-x-2">
              <div className={`w-3 h-3 rounded-full ${wsConnected ? 'bg-green-500' : 'bg-red-500'}`} />
              <span className="text-sm text-gray-400">
                {wsConnected ? 'Verbunden' : 'Getrennt'}
              </span>
            </div>
          </div>
        </div>

        {/* Wake Word Error Message */}
        {wakeWordError && !wakeWordEnabled && (
          <div className="mt-3 flex items-center px-3 py-2 bg-red-900/30 rounded-lg border border-red-700/50">
            <div className="flex items-center space-x-2">
              <div className="w-2 h-2 rounded-full bg-red-500" />
              <span className="text-sm text-red-300">
                {wakeWordError.name === 'BrowserNotSupportedError'
                  ? 'Wake word not supported in this browser. Use Chrome/Edge/Safari or the manual mic button.'
                  : <>Wake word not available. Run: <code className="bg-red-900/50 px-1 rounded">docker compose up -d --build</code></>
                }
              </span>
            </div>
          </div>
        )}

        {/* Wake Word Listening Indicator */}
        {wakeWordEnabled && !recording && (
          <div className="mt-3 flex items-center justify-between px-3 py-2 bg-green-900/30 rounded-lg border border-green-700/50">
            <div className="flex items-center space-x-2">
              <div className={`w-2 h-2 rounded-full ${wakeWordListening ? 'bg-green-500 animate-pulse' : 'bg-yellow-500'}`} />
              <span className="text-sm text-green-300">
                {wakeWordListening
                  ? `Listening for "${availableKeywords.find(k => k.id === wakeWordSettings.keyword)?.label || 'Hey Jarvis'}"...`
                  : wakeWordStatus === 'activated'
                    ? 'Wake word detected! Starting recording...'
                    : 'Wake word paused'
                }
              </span>
            </div>
            {lastDetection && (
              <span className="text-xs text-gray-400">
                Last: {lastDetection.keyword} ({(lastDetection.score * 100).toFixed(0)}%)
              </span>
            )}
          </div>
        )}

        {/* Wake Word Settings Dropdown */}
        {showWakeWordSettings && (
          <div className="mt-3 p-4 bg-gray-800 rounded-lg border border-gray-700">
            <h3 className="text-sm font-medium text-white mb-3">Wake Word Settings</h3>

            <div className="space-y-3">
              {/* Keyword Selection */}
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Wake Word</label>
                <select
                  value={wakeWordSettings.keyword}
                  onChange={(e) => setWakeWordKeyword(e.target.value)}
                  className="w-full bg-gray-700 text-white text-sm rounded-lg px-3 py-2 border border-gray-600 focus:border-primary-500 focus:outline-none"
                >
                  {availableKeywords.map(kw => (
                    <option key={kw.id} value={kw.id}>{kw.label}</option>
                  ))}
                </select>
              </div>

              {/* Threshold Slider */}
              <div>
                <label className="text-xs text-gray-400 mb-1 block">
                  Sensitivity: {(wakeWordSettings.threshold * 100).toFixed(0)}%
                </label>
                <input
                  type="range"
                  min="0.3"
                  max="0.8"
                  step="0.05"
                  value={wakeWordSettings.threshold}
                  onChange={(e) => setWakeWordThreshold(parseFloat(e.target.value))}
                  className="w-full"
                />
                <div className="flex justify-between text-xs text-gray-500">
                  <span>More sensitive</span>
                  <span>Less false positives</span>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto card space-y-4 mb-4">
        {messages.length === 0 && (
          <div className="text-center py-12">
            <p className="text-gray-400 mb-4">Starte ein Gespräch mit Renfield</p>
            <p className="text-sm text-gray-500">
              Du kannst Text eingeben oder das Mikrofon nutzen
            </p>
          </div>
        )}

        {messages.map((message, index) => (
          <div
            key={index}
            className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[70%] px-4 py-2 rounded-lg ${
                message.role === 'user'
                  ? 'bg-primary-600 text-white'
                  : 'bg-gray-700 text-gray-100'
              }`}
            >
              <p className="whitespace-pre-wrap">{message.content}</p>
              
              {message.role === 'assistant' && !message.streaming && (
                <button
                  onClick={() => speakText(message.content)}
                  className="mt-2 text-xs text-gray-400 hover:text-white flex items-center space-x-1"
                >
                  <Volume2 className="w-3 h-3" />
                  <span>Vorlesen</span>
                </button>
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex justify-start">
            <div className="bg-gray-700 px-4 py-2 rounded-lg">
              <Loader className="w-5 h-5 animate-spin text-gray-400" />
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="card">
        {/* Audio Waveform Visualizer während der Aufnahme */}
        {recording && (
          <div className="mb-4 p-4 bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl border border-gray-700/50 backdrop-blur-sm">
            {/* Header mit Status und Countdown */}
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center space-x-2">
                <div className="w-2.5 h-2.5 bg-red-500 rounded-full animate-pulse"></div>
                <span className="text-sm font-medium text-white">
                  {audioLevel > 10 ? 'Sprechen erkannt' : silenceTimeRemaining > 0 ? 'Stille erkannt...' : 'Höre zu...'}
                </span>
              </div>

              {/* Countdown Timer */}
              {silenceTimeRemaining > 0 && (
                <div className="flex items-center space-x-2 px-3 py-1 bg-yellow-500/20 rounded-full border border-yellow-500/30">
                  <div className="w-1.5 h-1.5 bg-yellow-400 rounded-full animate-pulse"></div>
                  <span className="text-xs font-mono text-yellow-300">
                    Auto-Stop in {(silenceTimeRemaining / 1000).toFixed(1)}s
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
                                   'bg-gray-600';

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
              <span className="text-gray-400">
                Level: {audioLevel} / 10
              </span>
              <span className="text-gray-500">
                Klicke 🔴 zum manuellen Stopp
              </span>
            </div>
          </div>
        )}
        
        <div className="flex items-center space-x-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyPress={(e) => e.key === 'Enter' && sendMessage(input, false)}
            placeholder="Nachricht eingeben..."
            className="input flex-1"
            disabled={loading || recording}
          />
          
          <button
            onClick={recording ? stopRecording : startRecording}
            className={`p-3 rounded-lg transition-colors ${
              recording
                ? 'bg-red-600 hover:bg-red-700 animate-pulse'
                : 'bg-gray-700 hover:bg-gray-600'
            }`}
            disabled={loading}
            title={recording ? 'Klicken um sofort zu stoppen (oder warte auf Stille)' : 'Klicken für Sprachaufnahme'}
          >
            {recording ? <MicOff className="w-5 h-5" /> : <Mic className="w-5 h-5" />}
          </button>

          <button
            onClick={() => sendMessage(input, false)}
            disabled={loading || !input.trim()}
            className="btn btn-primary"
          >
            <Send className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  );
}
