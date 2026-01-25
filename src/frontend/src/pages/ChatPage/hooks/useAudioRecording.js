import { useState, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import apiClient from '../../../utils/axios';
import { debug } from '../../../utils/debug';

// VAD Configuration Constants
const VAD_CONFIG = {
  SILENCE_THRESHOLD: 10,      // RMS threshold for silence detection
  SILENCE_DURATION: 1500,     // 1.5 seconds of silence to auto-stop
  MIN_RECORDING_TIME: 800,    // Minimum 0.8 seconds recording
  FFT_SIZE: 512,              // FFT size for audio analysis
  SMOOTHING: 0.3,             // Smoothing time constant
};

/**
 * Custom hook for audio recording with Voice Activity Detection (VAD).
 * Handles microphone access, recording, silence detection, and STT processing.
 *
 * @param {Object} options - Hook options
 * @param {Function} options.onTranscription - Callback with transcribed text
 * @param {Function} options.onError - Callback for errors
 * @param {Function} options.onRecordingStart - Callback when recording starts
 * @param {Function} options.onRecordingStop - Callback when recording stops
 * @returns {Object} Recording state and methods
 */
export function useAudioRecording({
  onTranscription,
  onError,
  onRecordingStart,
  onRecordingStop,
} = {}) {
  const { t } = useTranslation();

  // State
  const [recording, setRecording] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [silenceTimeRemaining, setSilenceTimeRemaining] = useState(0);
  const [processing, setProcessing] = useState(false);

  // Refs
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const audioContextRef = useRef(null);
  const analyserRef = useRef(null);
  const silenceTimerRef = useRef(null);
  const animationFrameRef = useRef(null);
  const isStoppingRef = useRef(false);
  const streamRef = useRef(null);

  /**
   * Process voice input - send to STT endpoint
   */
  const processVoiceInput = useCallback(async (audioBlob) => {
    debug.log('Processing voice input...');
    setProcessing(true);

    try {
      const formData = new FormData();
      formData.append('audio', audioBlob, 'recording.webm');

      debug.log('Sending audio to backend...');
      const sttResponse = await apiClient.post('/api/voice/stt', formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      });

      debug.log('STT Response:', sttResponse.data);
      const transcribedText = sttResponse.data.text;

      if (!transcribedText || transcribedText.trim() === '') {
        throw new Error(t('voice.noSpeechRecognized'));
      }

      debug.log('Transcribed text:', transcribedText);
      onTranscription?.(transcribedText);
    } catch (error) {
      console.error('Voice input error:', error);
      console.error('Error details:', error.response?.data);

      let errorMessage = t('voice.processingError');
      if (error.response?.data?.detail) {
        errorMessage += ' (' + error.response.data.detail + ')';
      }

      onError?.(errorMessage);
    } finally {
      setProcessing(false);
    }
  }, [t, onTranscription, onError]);

  /**
   * Stop recording
   */
  const stopRecording = useCallback(() => {
    // Prevent double calls (race condition protection)
    if (isStoppingRef.current) {
      console.warn('stopRecording already in progress, skipping duplicate call');
      return;
    }

    if (!mediaRecorderRef.current) {
      console.warn('stopRecording: mediaRecorderRef is null');
      return;
    }

    const mrState = mediaRecorderRef.current.state;
    debug.log('Stopping recording...');
    debug.log('MediaRecorder State:', mrState);

    // Only stop if MediaRecorder is in 'recording' state
    if (mrState !== 'recording') {
      console.warn('MediaRecorder not in recording state:', mrState);
      setRecording(false);
      isStoppingRef.current = false;
      return;
    }

    // Mark as "stopping" to prevent duplicate calls
    isStoppingRef.current = true;

    try {
      mediaRecorderRef.current.stop();
      setRecording(false);
      debug.log('stop() called, waiting for onstop handler...');
    } catch (error) {
      console.error('Error stopping recording:', error);
      setRecording(false);
      setProcessing(false);
      isStoppingRef.current = false;
    }
  }, []);

  /**
   * Start recording with Voice Activity Detection
   */
  const startRecording = useCallback(async () => {
    isStoppingRef.current = false;
    onRecordingStart?.();

    try {
      debug.log('Starting recording with Voice Activity Detection...');
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      debug.log('Microphone access granted');
      debug.log('Stream Tracks:', stream.getTracks().map(t => ({
        kind: t.kind,
        enabled: t.enabled,
        muted: t.muted,
        readyState: t.readyState
      })));

      // MediaRecorder setup
      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      // Audio Context for level monitoring
      try {
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        audioContextRef.current = audioContext;
        debug.log('AudioContext created, State:', audioContext.state);

        // Resume AudioContext if suspended
        if (audioContext.state === 'suspended') {
          await audioContext.resume();
          debug.log('AudioContext resumed, new State:', audioContext.state);
        }

        const source = audioContext.createMediaStreamSource(stream);
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = VAD_CONFIG.FFT_SIZE;
        analyser.smoothingTimeConstant = VAD_CONFIG.SMOOTHING;
        source.connect(analyser);
        analyserRef.current = analyser;

        debug.log('Analyser configured:', {
          fftSize: analyser.fftSize,
          frequencyBinCount: analyser.frequencyBinCount,
          smoothingTimeConstant: analyser.smoothingTimeConstant
        });
      } catch (audioError) {
        console.error('AudioContext error:', audioError);
        debug.log('Continuing without audio level monitoring');
      }

      // Voice Activity Detection
      const bufferLength = analyserRef.current ? analyserRef.current.frequencyBinCount : 0;
      const dataArray = bufferLength > 0 ? new Uint8Array(bufferLength) : null;

      let recordingStartTime = Date.now();
      let lastSoundTime = Date.now();
      let hasSoundDetected = false;
      let checkCount = 0;
      let isStillRecording = true;

      const checkAudioLevel = () => {
        if (!analyserRef.current || !dataArray) {
          // No audio monitoring available - show static level
          setAudioLevel(50);

          if (isStillRecording) {
            animationFrameRef.current = requestAnimationFrame(checkAudioLevel);
          }
          return;
        }

        analyserRef.current.getByteFrequencyData(dataArray);

        // Calculate audio level (RMS for better accuracy)
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) {
          sum += dataArray[i] * dataArray[i];
        }
        const rms = Math.sqrt(sum / dataArray.length);
        const average = rms;

        setAudioLevel(Math.round(average));

        checkCount++;
        // Log every 15 frames (~250ms) for debugging
        if (checkCount % 15 === 0) {
          const silenceDurationNow = Date.now() - lastSoundTime;
          debug.log('Audio Level:', Math.round(average),
            '| Threshold:', VAD_CONFIG.SILENCE_THRESHOLD,
            '| Sound detected:', hasSoundDetected,
            '| Silence:', Math.round(silenceDurationNow / 1000), 'sec');
        }

        const currentTime = Date.now();
        const recordingTime = currentTime - recordingStartTime;

        // Detect sound vs. silence
        if (average > VAD_CONFIG.SILENCE_THRESHOLD) {
          lastSoundTime = currentTime;
          hasSoundDetected = true;
          setSilenceTimeRemaining(0);

          // Clear silence timer
          if (silenceTimerRef.current) {
            clearTimeout(silenceTimerRef.current);
            silenceTimerRef.current = null;
          }

          if (checkCount % 30 === 0) {
            debug.log('Sound detected, Level:', Math.round(average));
          }
        } else {
          // Silence detected
          const silenceDuration = currentTime - lastSoundTime;

          // Calculate remaining time until auto-stop
          if (hasSoundDetected && recordingTime > VAD_CONFIG.MIN_RECORDING_TIME) {
            const remaining = Math.max(0, VAD_CONFIG.SILENCE_DURATION - silenceDuration);
            setSilenceTimeRemaining(remaining);
          } else {
            setSilenceTimeRemaining(0);
          }

          // Stop when all conditions are met
          if (recordingTime > VAD_CONFIG.MIN_RECORDING_TIME &&
            hasSoundDetected &&
            silenceDuration > VAD_CONFIG.SILENCE_DURATION) {

            debug.log('Silence detected for', Math.round(silenceDuration), 'ms - auto-stopping');
            debug.log('Recording Stats: Time:', Math.round(recordingTime), 'ms, Sound detected:', hasSoundDetected);
            isStillRecording = false;
            setSilenceTimeRemaining(0);
            stopRecording();
            return;
          }
        }

        // Continue monitoring
        if (isStillRecording) {
          animationFrameRef.current = requestAnimationFrame(checkAudioLevel);
        }
      };

      mediaRecorder.ondataavailable = (event) => {
        debug.log('Audio data received:', event.data.size, 'bytes');
        audioChunksRef.current.push(event.data);
      };

      mediaRecorder.onstop = async () => {
        debug.log('====== ONSTOP HANDLER STARTED ======');
        debug.log('Chunks received:', audioChunksRef.current.length);

        // Cleanup
        if (animationFrameRef.current) {
          cancelAnimationFrame(animationFrameRef.current);
          debug.log('AnimationFrame stopped');
        }
        if (silenceTimerRef.current) {
          clearTimeout(silenceTimerRef.current);
          debug.log('Silence Timer cleared');
        }
        if (audioContextRef.current) {
          try {
            await audioContextRef.current.close();
            debug.log('AudioContext closed');
          } catch (e) {
            console.warn('AudioContext close error:', e);
          }
        }

        setAudioLevel(0);

        const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
        debug.log('Audio Blob created:', audioBlob.size, 'bytes, Type:', audioBlob.type);

        // Stop stream
        if (streamRef.current) {
          streamRef.current.getTracks().forEach(track => {
            track.stop();
            debug.log('Stream Track stopped:', track.kind);
          });
        }

        onRecordingStop?.();

        // Only process if enough data
        if (audioBlob.size > 1000) {
          debug.log('Blob large enough, starting processing...');
          await processVoiceInput(audioBlob);
        } else {
          console.warn('Audio too short (', audioBlob.size, 'bytes), not processing');
          onError?.(t('voice.recordingTooShort'));
        }

        // Reset stopping flag
        isStoppingRef.current = false;
        debug.log('====== ONSTOP HANDLER COMPLETED ======');
      };

      mediaRecorder.start();
      setRecording(true);
      debug.log('Recording started... (auto-stop on silence)');

      // Start audio level monitoring
      checkAudioLevel();

    } catch (error) {
      console.error('Microphone error:', error);
      onError?.(t('voice.microphoneError') + ': ' + error.message);
      onRecordingStop?.();
    }
  }, [t, stopRecording, processVoiceInput, onRecordingStart, onRecordingStop, onError]);

  /**
   * Toggle recording state
   */
  const toggleRecording = useCallback(() => {
    if (recording) {
      stopRecording();
    } else {
      startRecording();
    }
  }, [recording, startRecording, stopRecording]);

  return {
    // State
    recording,
    audioLevel,
    silenceTimeRemaining,
    processing,

    // Methods
    startRecording,
    stopRecording,
    toggleRecording,
  };
}
