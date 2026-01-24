import React, { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Users, UserPlus, Mic, MicOff, Trash2, Loader, CheckCircle,
  XCircle, AlertCircle, Volume2, Shield, ShieldCheck, RefreshCw,
  Edit3, GitMerge
} from 'lucide-react';
import apiClient from '../utils/axios';
import { useConfirmDialog } from '../components/ConfirmDialog';
import Modal from '../components/Modal';

export default function SpeakersPage() {
  const { t } = useTranslation();
  // Confirm dialog hook
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  // State
  const [speakers, setSpeakers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [serviceStatus, setServiceStatus] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEnrollModal, setShowEnrollModal] = useState(false);
  const [showIdentifyModal, setShowIdentifyModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showMergeModal, setShowMergeModal] = useState(false);
  const [selectedSpeaker, setSelectedSpeaker] = useState(null);
  const [mergeTargetId, setMergeTargetId] = useState(null);
  const [merging, setMerging] = useState(false);
  const [recording, setRecording] = useState(false);
  const [audioBlob, setAudioBlob] = useState(null);
  const [enrolling, setEnrolling] = useState(false);
  const [identifying, setIdentifying] = useState(false);
  const [identifyResult, setIdentifyResult] = useState(null);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [audioLevel, setAudioLevel] = useState(0);

  // Form state
  const [newSpeakerName, setNewSpeakerName] = useState('');
  const [newSpeakerAlias, setNewSpeakerAlias] = useState('');
  const [newSpeakerIsAdmin, setNewSpeakerIsAdmin] = useState(false);

  // Edit form state
  const [editSpeakerName, setEditSpeakerName] = useState('');
  const [editSpeakerAlias, setEditSpeakerAlias] = useState('');
  const [editSpeakerIsAdmin, setEditSpeakerIsAdmin] = useState(false);
  const [updating, setUpdating] = useState(false);

  // Refs
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const streamRef = useRef(null);
  const audioContextRef = useRef(null);
  const analyserRef = useRef(null);
  const animationFrameRef = useRef(null);

  // Load data on mount
  useEffect(() => {
    loadServiceStatus();
    loadSpeakers();
  }, []);

  // Clear messages after 5 seconds
  useEffect(() => {
    if (error || success) {
      const timer = setTimeout(() => {
        setError(null);
        setSuccess(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [error, success]);

  const loadServiceStatus = async () => {
    try {
      const response = await apiClient.get('/api/speakers/status');
      setServiceStatus(response.data);
    } catch (err) {
      console.error('Failed to load service status:', err);
      setServiceStatus({ available: false, message: t('speakers.serviceNotAvailable') });
    }
  };

  const loadSpeakers = async () => {
    try {
      setLoading(true);
      const response = await apiClient.get('/api/speakers');
      setSpeakers(response.data);
    } catch (err) {
      console.error('Failed to load speakers:', err);
      setError(t('speakers.couldNotLoad'));
    } finally {
      setLoading(false);
    }
  };

  const createSpeaker = async () => {
    if (!newSpeakerName.trim() || !newSpeakerAlias.trim()) {
      setError(t('speakers.nameAndAliasRequired'));
      return;
    }

    try {
      await apiClient.post('/api/speakers', {
        name: newSpeakerName,
        alias: newSpeakerAlias,
        is_admin: newSpeakerIsAdmin
      });

      setSuccess(t('speakers.speakerCreated', { name: newSpeakerName }));
      setShowCreateModal(false);
      setNewSpeakerName('');
      setNewSpeakerAlias('');
      setNewSpeakerIsAdmin(false);
      loadSpeakers();
    } catch (err) {
      console.error('Failed to create speaker:', err);
      setError(err.response?.data?.detail || t('common.error'));
    }
  };

  const deleteSpeaker = async (speaker) => {
    const confirmed = await confirm({
      title: t('speakers.deleteSpeaker'),
      message: t('speakers.deleteSpeakerConfirm', { name: speaker.name }),
      confirmLabel: t('common.delete'),
      cancelLabel: t('common.cancel'),
      variant: 'danger',
    });

    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/speakers/${speaker.id}`);
      setSuccess(t('speakers.speakerDeleted', { name: speaker.name }));
      loadSpeakers();
    } catch (err) {
      console.error('Failed to delete speaker:', err);
      setError(t('common.error'));
    }
  };

  const updateSpeaker = async () => {
    if (!selectedSpeaker || !editSpeakerName.trim() || !editSpeakerAlias.trim()) {
      setError(t('speakers.nameAndAliasRequired'));
      return;
    }

    try {
      setUpdating(true);
      await apiClient.patch(`/api/speakers/${selectedSpeaker.id}`, {
        name: editSpeakerName,
        alias: editSpeakerAlias,
        is_admin: editSpeakerIsAdmin
      });

      setSuccess(t('speakers.speakerUpdated', { name: editSpeakerName }));
      setShowEditModal(false);
      loadSpeakers();
    } catch (err) {
      console.error('Failed to update speaker:', err);
      setError(err.response?.data?.detail || t('common.error'));
    } finally {
      setUpdating(false);
    }
  };

  const mergeSpeakers = async () => {
    if (!selectedSpeaker || !mergeTargetId) {
      setError(t('speakers.selectSourceAndTarget'));
      return;
    }

    if (selectedSpeaker.id === parseInt(mergeTargetId)) {
      setError(t('speakers.sameSourceAndTarget'));
      return;
    }

    const targetSpeaker = speakers.find(s => s.id === parseInt(mergeTargetId));
    const confirmed = await confirm({
      title: t('speakers.mergeSpeakers'),
      message: t('speakers.mergeConfirm', { source: selectedSpeaker.name, target: targetSpeaker?.name }),
      confirmLabel: t('speakers.merge'),
      cancelLabel: t('common.cancel'),
      variant: 'warning',
    });

    if (!confirmed) return;

    try {
      setMerging(true);
      const response = await apiClient.post('/api/speakers/merge', {
        source_speaker_id: selectedSpeaker.id,
        target_speaker_id: parseInt(mergeTargetId)
      });

      setSuccess(response.data.message);
      setShowMergeModal(false);
      setSelectedSpeaker(null);
      setMergeTargetId(null);
      loadSpeakers();
    } catch (err) {
      console.error('Failed to merge speakers:', err);
      setError(err.response?.data?.detail || t('speakers.mergeFailed'));
    } finally {
      setMerging(false);
    }
  };

  const openEditModal = (speaker) => {
    setSelectedSpeaker(speaker);
    setEditSpeakerName(speaker.name);
    setEditSpeakerAlias(speaker.alias);
    setEditSpeakerIsAdmin(speaker.is_admin);
    setShowEditModal(true);
  };

  const openMergeModal = (speaker) => {
    setSelectedSpeaker(speaker);
    setMergeTargetId(null);
    setShowMergeModal(true);
  };

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      // Set up audio context for level monitoring
      try {
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        audioContextRef.current = audioContext;

        if (audioContext.state === 'suspended') {
          await audioContext.resume();
        }

        const source = audioContext.createMediaStreamSource(stream);
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.3;
        source.connect(analyser);
        analyserRef.current = analyser;

        // Start level monitoring
        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        const checkAudioLevel = () => {
          if (!analyserRef.current) return;

          analyserRef.current.getByteFrequencyData(dataArray);

          // Calculate RMS
          let sum = 0;
          for (let i = 0; i < dataArray.length; i++) {
            sum += dataArray[i] * dataArray[i];
          }
          const rms = Math.sqrt(sum / dataArray.length);
          setAudioLevel(Math.round(rms));

          animationFrameRef.current = requestAnimationFrame(checkAudioLevel);
        };

        checkAudioLevel();
      } catch (audioErr) {
        console.warn('Audio context setup failed:', audioErr);
      }

      mediaRecorder.ondataavailable = (event) => {
        audioChunksRef.current.push(event.data);
      };

      mediaRecorder.onstop = async () => {
        const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
        setAudioBlob(blob);

        // Stop level monitoring
        if (animationFrameRef.current) {
          cancelAnimationFrame(animationFrameRef.current);
        }
        if (audioContextRef.current) {
          try {
            await audioContextRef.current.close();
          } catch (e) {
            // Ignore
          }
        }
        setAudioLevel(0);

        // Stop stream
        if (streamRef.current) {
          streamRef.current.getTracks().forEach(track => track.stop());
        }
      };

      mediaRecorder.start();
      setRecording(true);
    } catch (err) {
      console.error('Failed to start recording:', err);
      setError(t('speakers.micAccessFailed'));
    }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && recording) {
      mediaRecorderRef.current.stop();
      setRecording(false);
    }
  };

  const enrollVoiceSample = async () => {
    if (!audioBlob || !selectedSpeaker) {
      setError(t('speakers.recordFirstSample'));
      return;
    }

    try {
      setEnrolling(true);

      const formData = new FormData();
      formData.append('audio', audioBlob, 'voice_sample.webm');

      const response = await apiClient.post(
        `/api/speakers/${selectedSpeaker.id}/enroll`,
        formData,
        { headers: { 'Content-Type': 'multipart/form-data' } }
      );

      setSuccess(response.data.message);
      setAudioBlob(null);
      setShowEnrollModal(false);
      loadSpeakers();
    } catch (err) {
      console.error('Failed to enroll voice sample:', err);
      setError(err.response?.data?.detail || t('speakers.voiceSampleFailed'));
    } finally {
      setEnrolling(false);
    }
  };

  const identifySpeaker = async () => {
    if (!audioBlob) {
      setError(t('speakers.recordFirstSample'));
      return;
    }

    try {
      setIdentifying(true);
      setIdentifyResult(null);

      const formData = new FormData();
      formData.append('audio', audioBlob, 'identify.webm');

      const response = await apiClient.post(
        '/api/speakers/identify',
        formData,
        { headers: { 'Content-Type': 'multipart/form-data' } }
      );

      setIdentifyResult(response.data);
    } catch (err) {
      console.error('Failed to identify speaker:', err);
      setError(err.response?.data?.detail || t('speakers.identificationFailed'));
    } finally {
      setIdentifying(false);
    }
  };

  const openEnrollModal = (speaker) => {
    setSelectedSpeaker(speaker);
    setAudioBlob(null);
    setShowEnrollModal(true);
  };

  const openIdentifyModal = () => {
    setAudioBlob(null);
    setIdentifyResult(null);
    setShowIdentifyModal(true);
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('speakers.title')}</h1>
            <p className="text-gray-500 dark:text-gray-400">{t('speakers.subtitle')}</p>
          </div>
          <div className="flex items-center space-x-2">
            <button
              onClick={loadSpeakers}
              className="p-2 rounded-lg bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300"
              aria-label={t('speakers.refreshSpeakers')}
            >
              <RefreshCw className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>

      {/* Service Status */}
      {serviceStatus && (
        <div className={`card ${serviceStatus.available ? 'bg-green-100 dark:bg-green-900/20 border-green-300 dark:border-green-700' : 'bg-red-100 dark:bg-red-900/20 border-red-300 dark:border-red-700'}`}>
          <div className="flex items-center space-x-3">
            {serviceStatus.available ? (
              <CheckCircle className="w-5 h-5 text-green-500" />
            ) : (
              <XCircle className="w-5 h-5 text-red-500" />
            )}
            <div>
              <p className={`font-medium ${serviceStatus.available ? 'text-green-700 dark:text-green-400' : 'text-red-700 dark:text-red-400'}`}>
                {serviceStatus.available ? t('speakers.serviceActive') : t('speakers.serviceNotAvailable')}
              </p>
              <p className="text-sm text-gray-500 dark:text-gray-400">{serviceStatus.message}</p>
            </div>
          </div>
        </div>
      )}

      {/* Alerts */}
      {error && (
        <div className="card bg-red-100 dark:bg-red-900/20 border-red-300 dark:border-red-700">
          <div className="flex items-center space-x-3">
            <AlertCircle className="w-5 h-5 text-red-500" />
            <p className="text-red-700 dark:text-red-400">{error}</p>
          </div>
        </div>
      )}

      {success && (
        <div className="card bg-green-100 dark:bg-green-900/20 border-green-300 dark:border-green-700">
          <div className="flex items-center space-x-3">
            <CheckCircle className="w-5 h-5 text-green-500" />
            <p className="text-green-700 dark:text-green-400">{success}</p>
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-wrap gap-3">
        <button
          onClick={() => setShowCreateModal(true)}
          className="btn btn-primary flex items-center space-x-2"
          disabled={!serviceStatus?.available}
        >
          <UserPlus className="w-4 h-4" />
          <span>{t('speakers.newSpeaker')}</span>
        </button>

        <button
          onClick={openIdentifyModal}
          className="btn bg-purple-600 hover:bg-purple-700 text-white flex items-center space-x-2"
          disabled={!serviceStatus?.available || speakers.length === 0}
        >
          <Volume2 className="w-4 h-4" />
          <span>{t('speakers.identifySpeaker')}</span>
        </button>
      </div>

      {/* Speakers List */}
      <div>
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">
          {t('speakers.registeredSpeakers', { count: speakers.length })}
        </h2>

        {loading ? (
          <div className="card text-center py-12" role="status" aria-label={t('speakers.loadingSpeakers')}>
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" aria-hidden="true" />
            <p className="text-gray-500 dark:text-gray-400">{t('speakers.loadingSpeakers')}</p>
          </div>
        ) : speakers.length === 0 ? (
          <div className="card text-center py-12">
            <Users className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-4" />
            <p className="text-gray-500 dark:text-gray-400 mb-4">{t('speakers.noSpeakers')}</p>
            <button
              onClick={() => setShowCreateModal(true)}
              className="btn btn-primary"
              disabled={!serviceStatus?.available}
            >
              {t('speakers.createFirstSpeaker')}
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {speakers.map((speaker) => (
              <div key={speaker.id} className="card">
                <div className="flex items-start justify-between mb-4">
                  <div className="flex items-center space-x-3">
                    <div className={`p-3 rounded-lg ${speaker.is_admin ? 'bg-yellow-600' : 'bg-primary-600'}`}>
                      {speaker.is_admin ? (
                        <ShieldCheck className="w-6 h-6 text-white" />
                      ) : (
                        <Users className="w-6 h-6 text-white" />
                      )}
                    </div>
                    <div>
                      <p className="text-gray-900 dark:text-white font-medium">{speaker.name}</p>
                      <p className="text-sm text-gray-500 dark:text-gray-400">@{speaker.alias}</p>
                    </div>
                  </div>
                  {speaker.is_admin && (
                    <span className="px-2 py-1 bg-yellow-100 text-yellow-700 dark:bg-yellow-600/20 dark:text-yellow-400 text-xs rounded">
                      Admin
                    </span>
                  )}
                </div>

                <div className="flex items-center justify-between text-sm mb-4">
                  <span className="text-gray-500 dark:text-gray-400">{t('speakers.voiceSamples')}:</span>
                  <span className={`font-medium ${
                    speaker.embedding_count >= 3 ? 'text-green-600 dark:text-green-400' :
                    speaker.embedding_count > 0 ? 'text-yellow-600 dark:text-yellow-400' : 'text-red-600 dark:text-red-400'
                  }`}>
                    {speaker.embedding_count} {speaker.embedding_count >= 3 ? t('speakers.voiceSamplesGood') : speaker.embedding_count > 0 ? t('speakers.voiceSamplesMore') : t('speakers.voiceSamplesNone')}
                  </span>
                </div>

                <div className="flex space-x-2">
                  <button
                    onClick={() => openEnrollModal(speaker)}
                    className="flex-1 btn bg-green-600 hover:bg-green-700 text-white text-sm flex items-center justify-center space-x-1"
                    aria-label={t('speakers.recordFor', { name: speaker.name })}
                  >
                    <Mic className="w-4 h-4" aria-hidden="true" />
                    <span>{t('speakers.record')}</span>
                  </button>
                  <button
                    onClick={() => openEditModal(speaker)}
                    className="p-2 rounded-lg bg-blue-100 hover:bg-blue-200 text-blue-600 dark:bg-blue-600/20 dark:hover:bg-blue-600/40 dark:text-blue-400"
                    aria-label={`${speaker.name} ${t('common.edit').toLowerCase()}`}
                  >
                    <Edit3 className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    onClick={() => openMergeModal(speaker)}
                    className="p-2 rounded-lg bg-purple-100 hover:bg-purple-200 text-purple-600 dark:bg-purple-600/20 dark:hover:bg-purple-600/40 dark:text-purple-400"
                    aria-label={`${speaker.name} ${t('speakers.merge').toLowerCase()}`}
                    disabled={speakers.length < 2}
                  >
                    <GitMerge className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    onClick={() => deleteSpeaker(speaker)}
                    className="p-2 rounded-lg bg-red-100 hover:bg-red-200 text-red-600 dark:bg-red-600/20 dark:hover:bg-red-600/40 dark:text-red-400"
                    aria-label={`${speaker.name} ${t('common.delete').toLowerCase()}`}
                  >
                    <Trash2 className="w-4 h-4" aria-hidden="true" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Create Speaker Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">{t('speakers.createSpeaker')}</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('common.name')}</label>
                <input
                  type="text"
                  value={newSpeakerName}
                  onChange={(e) => setNewSpeakerName(e.target.value)}
                  placeholder="Max Mustermann"
                  className="input w-full"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('speakers.aliasLabel')}</label>
                <input
                  type="text"
                  value={newSpeakerAlias}
                  onChange={(e) => setNewSpeakerAlias(e.target.value.toLowerCase().replace(/\s/g, '_'))}
                  placeholder="max"
                  className="input w-full"
                />
              </div>

              <div className="flex items-center space-x-3">
                <input
                  type="checkbox"
                  id="isAdmin"
                  checked={newSpeakerIsAdmin}
                  onChange={(e) => setNewSpeakerIsAdmin(e.target.checked)}
                  className="w-4 h-4 rounded"
                />
                <label htmlFor="isAdmin" className="text-sm text-gray-600 dark:text-gray-300 flex items-center space-x-2">
                  <Shield className="w-4 h-4" />
                  <span>{t('speakers.adminPermission')}</span>
                </label>
              </div>
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowCreateModal(false)}
                className="flex-1 btn btn-secondary"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={createSpeaker}
                className="flex-1 btn btn-primary"
              >
                {t('common.create')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Enroll Modal */}
      {showEnrollModal && selectedSpeaker && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-2">{t('speakers.recordVoiceSample')}</h2>
            <p className="text-gray-500 dark:text-gray-400 mb-4">{t('speakers.recordFor', { name: selectedSpeaker.name })}</p>

            <div className="bg-gray-100 dark:bg-gray-800 rounded-lg p-6 mb-4">
              <div className="text-center">
                {recording ? (
                  <div>
                    {/* Recording Header */}
                    <div className="flex items-center justify-center space-x-2 mb-4">
                      <div className="w-2.5 h-2.5 bg-red-500 rounded-full animate-pulse"></div>
                      <span className="text-sm font-medium text-red-600 dark:text-red-400">
                        {audioLevel > 10 ? t('voice.speechDetected') : t('voice.listening')}
                      </span>
                    </div>

                    {/* Waveform Visualization */}
                    <div className="flex items-center justify-center space-x-1.5 h-16 mb-4">
                      {[0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => {
                        const variation = Math.sin((Date.now() / 100) + i) * 0.3 + 0.7;
                        const baseHeight = Math.max(10, audioLevel) * variation;
                        const height = Math.min(100, baseHeight);
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

                    <p className="text-sm text-gray-500 dark:text-gray-400">{t('speakers.speak3to10seconds')}</p>
                    <p className="text-xs text-gray-500 mt-1">{t('voice.level')}: {audioLevel}</p>
                  </div>
                ) : audioBlob ? (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-green-600 rounded-full flex items-center justify-center">
                      <CheckCircle className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-green-600 dark:text-green-400 font-medium">{t('speakers.recordingReady')}</p>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
                      {(audioBlob.size / 1024).toFixed(1)} KB
                    </p>
                  </div>
                ) : (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-gray-300 dark:bg-gray-700 rounded-full flex items-center justify-center">
                      <Mic className="w-8 h-8 text-gray-500 dark:text-gray-400" />
                    </div>
                    <p className="text-gray-500 dark:text-gray-400">{t('speakers.readyToRecord')}</p>
                    <p className="text-sm text-gray-500 mt-2">{t('speakers.speakAnySentence')}</p>
                  </div>
                )}
              </div>
            </div>

            <div className="flex space-x-3 mb-4">
              {recording ? (
                <button
                  onClick={stopRecording}
                  className="flex-1 btn bg-red-600 hover:bg-red-700 text-white flex items-center justify-center space-x-2"
                >
                  <MicOff className="w-4 h-4" />
                  <span>{t('speakers.stop')}</span>
                </button>
              ) : (
                <button
                  onClick={startRecording}
                  className="flex-1 btn bg-green-600 hover:bg-green-700 text-white flex items-center justify-center space-x-2"
                >
                  <Mic className="w-4 h-4" />
                  <span>{t('speakers.record')}</span>
                </button>
              )}
            </div>

            <div className="flex space-x-3">
              <button
                onClick={() => {
                  setShowEnrollModal(false);
                  setAudioBlob(null);
                }}
                className="flex-1 btn btn-secondary"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={enrollVoiceSample}
                disabled={!audioBlob || enrolling}
                className="flex-1 btn btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {enrolling ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  t('common.save')
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Identify Modal */}
      {showIdentifyModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-2">{t('speakers.identifySpeaker')}</h2>
            <p className="text-gray-500 dark:text-gray-400 mb-4">{t('speakers.identifySubtitle')}</p>

            <div className="bg-gray-100 dark:bg-gray-800 rounded-lg p-6 mb-4">
              <div className="text-center">
                {recording ? (
                  <div>
                    {/* Recording Header */}
                    <div className="flex items-center justify-center space-x-2 mb-4">
                      <div className="w-2.5 h-2.5 bg-red-500 rounded-full animate-pulse"></div>
                      <span className="text-sm font-medium text-red-600 dark:text-red-400">
                        {audioLevel > 10 ? t('voice.speechDetected') : t('voice.listening')}
                      </span>
                    </div>

                    {/* Waveform Visualization */}
                    <div className="flex items-center justify-center space-x-1.5 h-16 mb-4">
                      {[0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => {
                        const variation = Math.sin((Date.now() / 100) + i) * 0.3 + 0.7;
                        const baseHeight = Math.max(10, audioLevel) * variation;
                        const height = Math.min(100, baseHeight);
                        const colorClass = audioLevel > 50 ? 'bg-green-500' :
                                           audioLevel > 10 ? 'bg-purple-500' :
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

                    <p className="text-xs text-gray-500">{t('voice.level')}: {audioLevel}</p>
                  </div>
                ) : audioBlob && !identifyResult ? (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-green-600 rounded-full flex items-center justify-center">
                      <CheckCircle className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-green-600 dark:text-green-400 font-medium">{t('speakers.recordingReady')}</p>
                  </div>
                ) : identifyResult ? (
                  <div>
                    {identifyResult.is_identified ? (
                      <>
                        <div className="w-16 h-16 mx-auto mb-4 bg-green-600 rounded-full flex items-center justify-center">
                          <CheckCircle className="w-8 h-8 text-white" />
                        </div>
                        <p className="text-green-600 dark:text-green-400 font-medium text-lg">{identifyResult.speaker_name}</p>
                        <p className="text-gray-500 dark:text-gray-400">@{identifyResult.speaker_alias}</p>
                        <p className="text-sm text-gray-500 mt-2">
                          {t('speakers.confidence')}: {(identifyResult.confidence * 100).toFixed(1)}%
                        </p>
                      </>
                    ) : (
                      <>
                        <div className="w-16 h-16 mx-auto mb-4 bg-yellow-600 rounded-full flex items-center justify-center">
                          <AlertCircle className="w-8 h-8 text-white" />
                        </div>
                        <p className="text-yellow-600 dark:text-yellow-400 font-medium">{t('speakers.speakerNotIdentified')}</p>
                        <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
                          {t('speakers.noRegisteredSpeakerFound')}
                        </p>
                      </>
                    )}
                  </div>
                ) : (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-purple-600 rounded-full flex items-center justify-center">
                      <Volume2 className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-gray-500 dark:text-gray-400">{t('speakers.readyToIdentify')}</p>
                  </div>
                )}
              </div>
            </div>

            <div className="flex space-x-3 mb-4">
              {recording ? (
                <button
                  onClick={stopRecording}
                  className="flex-1 btn bg-red-600 hover:bg-red-700 text-white flex items-center justify-center space-x-2"
                >
                  <MicOff className="w-4 h-4" />
                  <span>{t('speakers.stop')}</span>
                </button>
              ) : (
                <button
                  onClick={() => {
                    setIdentifyResult(null);
                    startRecording();
                  }}
                  className="flex-1 btn bg-green-600 hover:bg-green-700 text-white flex items-center justify-center space-x-2"
                >
                  <Mic className="w-4 h-4" />
                  <span>{identifyResult ? t('speakers.recordAgain') : t('speakers.record')}</span>
                </button>
              )}
            </div>

            <div className="flex space-x-3">
              <button
                onClick={() => {
                  setShowIdentifyModal(false);
                  setAudioBlob(null);
                  setIdentifyResult(null);
                }}
                className="flex-1 btn btn-secondary"
              >
                {t('common.close')}
              </button>
              <button
                onClick={identifySpeaker}
                disabled={!audioBlob || identifying || identifyResult}
                className="flex-1 btn btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {identifying ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  t('speakers.identify')
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Edit Speaker Modal */}
      {showEditModal && selectedSpeaker && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">{t('speakers.editSpeaker')}</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('common.name')}</label>
                <input
                  type="text"
                  value={editSpeakerName}
                  onChange={(e) => setEditSpeakerName(e.target.value)}
                  placeholder="Max Mustermann"
                  className="input w-full"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('speakers.aliasLabel')}</label>
                <input
                  type="text"
                  value={editSpeakerAlias}
                  onChange={(e) => setEditSpeakerAlias(e.target.value.toLowerCase().replace(/\s/g, '_'))}
                  placeholder="max"
                  className="input w-full"
                />
              </div>

              <div className="flex items-center space-x-3">
                <input
                  type="checkbox"
                  id="editIsAdmin"
                  checked={editSpeakerIsAdmin}
                  onChange={(e) => setEditSpeakerIsAdmin(e.target.checked)}
                  className="w-4 h-4 rounded"
                />
                <label htmlFor="editIsAdmin" className="text-sm text-gray-600 dark:text-gray-300 flex items-center space-x-2">
                  <Shield className="w-4 h-4" />
                  <span>{t('speakers.adminPermission')}</span>
                </label>
              </div>

              <div className="text-sm text-gray-500">
                {t('speakers.voiceSamples')}: {selectedSpeaker.embedding_count}
              </div>
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowEditModal(false)}
                className="flex-1 btn btn-secondary"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={updateSpeaker}
                disabled={updating}
                className="flex-1 btn btn-primary disabled:opacity-50"
              >
                {updating ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  t('common.save')
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Merge Speakers Modal */}
      {showMergeModal && selectedSpeaker && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-2">{t('speakers.mergeSpeakers')}</h2>
            <p className="text-gray-500 dark:text-gray-400 mb-4">
              {t('speakers.mergeSubtitle', { name: selectedSpeaker.name })}
            </p>

            <div className="bg-gray-100 dark:bg-gray-800 rounded-lg p-4 mb-4">
              <div className="flex items-center space-x-3 mb-4">
                <div className="p-2 bg-purple-600 rounded-lg">
                  <GitMerge className="w-5 h-5 text-white" />
                </div>
                <div>
                  <p className="text-gray-900 dark:text-white font-medium">{selectedSpeaker.name}</p>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    {selectedSpeaker.embedding_count} {t('speakers.voiceSamples')}
                  </p>
                </div>
              </div>

              <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-2">{t('speakers.mergeTarget')}:</label>
                <select
                  value={mergeTargetId || ''}
                  onChange={(e) => setMergeTargetId(e.target.value)}
                  className="input w-full"
                >
                  <option value="">{t('speakers.mergeTargetPlaceholder')}</option>
                  {speakers
                    .filter(s => s.id !== selectedSpeaker.id)
                    .map(s => (
                      <option key={s.id} value={s.id}>
                        {s.name} (@{s.alias}) - {s.embedding_count} Samples
                      </option>
                    ))
                  }
                </select>
              </div>
            </div>

            <div className="bg-yellow-100 dark:bg-yellow-900/20 border border-yellow-300 dark:border-yellow-700 rounded-lg p-3 mb-4">
              <div className="flex items-start space-x-2">
                <AlertCircle className="w-5 h-5 text-yellow-600 dark:text-yellow-500 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-yellow-700 dark:text-yellow-400">
                  {t('speakers.mergeWarning', { name: selectedSpeaker.name })}
                </p>
              </div>
            </div>

            <div className="flex space-x-3">
              <button
                onClick={() => {
                  setShowMergeModal(false);
                  setSelectedSpeaker(null);
                  setMergeTargetId(null);
                }}
                className="flex-1 btn btn-secondary"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={mergeSpeakers}
                disabled={!mergeTargetId || merging}
                className="flex-1 btn bg-purple-600 hover:bg-purple-700 text-white disabled:opacity-50"
              >
                {merging ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" aria-label={t('common.loading')} />
                ) : (
                  t('speakers.merge')
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Confirm Dialog */}
      {ConfirmDialogComponent}
    </div>
  );
}
