import { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Users, UserPlus, Mic, MicOff, Trash2, Loader, CheckCircle,
  AlertCircle, Volume2, Shield, ShieldCheck, RefreshCw,
  Edit3, GitMerge,
} from 'lucide-react';
import { extractApiError } from '../utils/axios';
import { useConfirmDialog } from '../components/ConfirmDialog';
import Modal from '../components/Modal';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import {
  useSpeakersQuery,
  useSpeakerStatusQuery,
  useCreateSpeaker,
  useUpdateSpeaker,
  useDeleteSpeaker,
  useMergeSpeakers,
  useEnrollSpeaker,
  useIdentifySpeaker,
  type Speaker,
  type IdentifyResult,
} from '../api/resources/speakers';

interface AudioContextCapableWindow {
  AudioContext?: typeof AudioContext;
  webkitAudioContext?: typeof AudioContext;
}

export default function SpeakersPage() {
  const { t } = useTranslation();
  // Confirm dialog hook
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const speakersQuery = useSpeakersQuery();
  const statusQuery = useSpeakerStatusQuery();
  const speakers: Speaker[] = speakersQuery.data ?? [];
  const loading = speakersQuery.isLoading;
  const serviceStatus = statusQuery.data ?? null;

  const createSpeakerMutation = useCreateSpeaker();
  const updateSpeakerMutation = useUpdateSpeaker();
  const deleteSpeakerMutation = useDeleteSpeaker();
  const mergeSpeakersMutation = useMergeSpeakers();
  const enrollMutation = useEnrollSpeaker();
  const identifyMutation = useIdentifySpeaker();

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEnrollModal, setShowEnrollModal] = useState(false);
  const [showIdentifyModal, setShowIdentifyModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showMergeModal, setShowMergeModal] = useState(false);
  const [selectedSpeaker, setSelectedSpeaker] = useState<Speaker | null>(null);
  const [mergeTargetId, setMergeTargetId] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [audioBlob, setAudioBlob] = useState<Blob | null>(null);
  const [identifyResult, setIdentifyResult] = useState<IdentifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [audioLevel, setAudioLevel] = useState(0);

  const merging = mergeSpeakersMutation.isPending;
  const enrolling = enrollMutation.isPending;
  const identifying = identifyMutation.isPending;
  const updating = updateSpeakerMutation.isPending;

  // Form state
  const [newSpeakerName, setNewSpeakerName] = useState('');
  const [newSpeakerAlias, setNewSpeakerAlias] = useState('');
  const [newSpeakerIsAdmin, setNewSpeakerIsAdmin] = useState(false);

  // Edit form state
  const [editSpeakerName, setEditSpeakerName] = useState('');
  const [editSpeakerAlias, setEditSpeakerAlias] = useState('');
  const [editSpeakerIsAdmin, setEditSpeakerIsAdmin] = useState(false);

  // Refs
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const animationFrameRef = useRef<number | null>(null);

  const displayError = error ?? speakersQuery.errorMessage;

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

  const createSpeaker = async () => {
    if (!newSpeakerName.trim() || !newSpeakerAlias.trim()) {
      setError(t('speakers.nameAndAliasRequired'));
      return;
    }
    try {
      await createSpeakerMutation.mutateAsync({
        name: newSpeakerName,
        alias: newSpeakerAlias,
        is_admin: newSpeakerIsAdmin,
      });
      setSuccess(t('speakers.speakerCreated', { name: newSpeakerName }));
      setShowCreateModal(false);
      setNewSpeakerName('');
      setNewSpeakerAlias('');
      setNewSpeakerIsAdmin(false);
    } catch (err) {
      setError(extractApiError(err, t('common.error')));
    }
  };

  const deleteSpeaker = async (speaker: Speaker) => {
    const confirmed = await confirm({
      title: t('speakers.deleteSpeaker'),
      message: t('speakers.deleteSpeakerConfirm', { name: speaker.name }),
      confirmLabel: t('common.delete'),
      cancelLabel: t('common.cancel'),
      variant: 'danger',
    });
    if (!confirmed) return;
    try {
      await deleteSpeakerMutation.mutateAsync(speaker.id);
      setSuccess(t('speakers.speakerDeleted', { name: speaker.name }));
    } catch {
      setError(t('common.error'));
    }
  };

  const updateSpeaker = async () => {
    if (!selectedSpeaker || !editSpeakerName.trim() || !editSpeakerAlias.trim()) {
      setError(t('speakers.nameAndAliasRequired'));
      return;
    }
    try {
      await updateSpeakerMutation.mutateAsync({
        id: selectedSpeaker.id,
        patch: {
          name: editSpeakerName,
          alias: editSpeakerAlias,
          is_admin: editSpeakerIsAdmin,
        },
      });
      setSuccess(t('speakers.speakerUpdated', { name: editSpeakerName }));
      setShowEditModal(false);
    } catch (err) {
      setError(extractApiError(err, t('common.error')));
    }
  };

  const mergeSpeakers = async () => {
    if (!selectedSpeaker || !mergeTargetId) {
      setError(t('speakers.selectSourceAndTarget'));
      return;
    }

    const mergeTargetIdInt = parseInt(mergeTargetId, 10);
    if (selectedSpeaker.id === mergeTargetIdInt) {
      setError(t('speakers.sameSourceAndTarget'));
      return;
    }

    const targetSpeaker = speakers.find((s) => s.id === mergeTargetIdInt);
    const confirmed = await confirm({
      title: t('speakers.mergeSpeakers'),
      message: t('speakers.mergeConfirm', { source: selectedSpeaker.name, target: targetSpeaker?.name }),
      confirmLabel: t('speakers.merge'),
      cancelLabel: t('common.cancel'),
      variant: 'warning',
    });

    if (!confirmed) return;
    try {
      const data = await mergeSpeakersMutation.mutateAsync({
        source_speaker_id: selectedSpeaker.id,
        target_speaker_id: mergeTargetIdInt,
      });
      setSuccess(data.message);
      setShowMergeModal(false);
      setSelectedSpeaker(null);
      setMergeTargetId(null);
    } catch (err) {
      setError(extractApiError(err, t('speakers.mergeFailed')));
    }
  };

  const openEditModal = (speaker: Speaker) => {
    setSelectedSpeaker(speaker);
    setEditSpeakerName(speaker.name);
    setEditSpeakerAlias(speaker.alias);
    setEditSpeakerIsAdmin(speaker.is_admin);
    setShowEditModal(true);
  };

  const openMergeModal = (speaker: Speaker) => {
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
        const win = window as unknown as AudioContextCapableWindow;
        const Ctor = win.AudioContext ?? win.webkitAudioContext;
        if (!Ctor) throw new Error('AudioContext is not supported');
        const audioContext = new Ctor();
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

      mediaRecorder.ondataavailable = (event: BlobEvent) => {
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
          streamRef.current.getTracks().forEach((track) => track.stop());
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
      const data = await enrollMutation.mutateAsync({ speakerId: selectedSpeaker.id, audio: audioBlob });
      setSuccess(data.message);
      setAudioBlob(null);
      setShowEnrollModal(false);
    } catch (err) {
      setError(extractApiError(err, t('speakers.voiceSampleFailed')));
    }
  };

  const identifySpeaker = async () => {
    if (!audioBlob) {
      setError(t('speakers.recordFirstSample'));
      return;
    }
    try {
      setIdentifyResult(null);
      const data = await identifyMutation.mutateAsync(audioBlob);
      setIdentifyResult(data);
    } catch (err) {
      setError(extractApiError(err, t('speakers.identificationFailed')));
    }
  };

  const openEnrollModal = (speaker: Speaker) => {
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
      <PageHeader icon={Users} title={t('speakers.title')} subtitle={t('speakers.subtitle')}>
        <button onClick={() => speakersQuery.refetch()} className="btn-icon btn-icon-ghost" aria-label={t('speakers.refreshSpeakers')}>
          <RefreshCw className="w-5 h-5" aria-hidden="true" />
        </button>
      </PageHeader>

      {/* Service Status */}
      {serviceStatus && (
        <Alert variant={serviceStatus.available ? 'success' : 'error'}>
          <div>
            <p className="font-medium">
              {serviceStatus.available ? t('speakers.serviceActive') : t('speakers.serviceNotAvailable')}
            </p>
            <p className="text-sm opacity-80">{serviceStatus.message}</p>
          </div>
        </Alert>
      )}

      {/* Alerts */}
      {displayError && <Alert variant="error">{displayError}</Alert>}

      {success && <Alert variant="success">{success}</Alert>}

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
          className="btn btn-secondary flex items-center space-x-2"
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
                    <Badge color="yellow">Admin</Badge>
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
                    className="flex-1 btn btn-success text-sm flex items-center justify-center space-x-1"
                    aria-label={t('speakers.recordFor', { name: speaker.name })}
                  >
                    <Mic className="w-4 h-4" aria-hidden="true" />
                    <span>{t('speakers.record')}</span>
                  </button>
                  <button
                    onClick={() => openEditModal(speaker)}
                    className="btn-icon btn-icon-ghost"
                    aria-label={`${speaker.name} ${t('common.edit').toLowerCase()}`}
                  >
                    <Edit3 className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    onClick={() => openMergeModal(speaker)}
                    className="btn-icon btn-icon-ghost"
                    aria-label={`${speaker.name} ${t('speakers.merge').toLowerCase()}`}
                    disabled={speakers.length < 2}
                  >
                    <GitMerge className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    onClick={() => deleteSpeaker(speaker)}
                    className="btn-icon btn-icon-danger"
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
      <Modal isOpen={showCreateModal} onClose={() => setShowCreateModal(false)} title={t('speakers.createSpeaker')}>
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
              className="w-4 h-4 rounded-sm"
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
      </Modal>

      {/* Enroll Modal */}
      <Modal isOpen={showEnrollModal && !!selectedSpeaker} onClose={() => { setShowEnrollModal(false); setAudioBlob(null); }} title={t('speakers.recordVoiceSample')}>
        <p className="text-gray-500 dark:text-gray-400 mb-4">{t('speakers.recordFor', { name: selectedSpeaker?.name })}</p>

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
              className="flex-1 btn btn-danger flex items-center justify-center space-x-2"
            >
              <MicOff className="w-4 h-4" />
              <span>{t('speakers.stop')}</span>
            </button>
          ) : (
            <button
              onClick={startRecording}
              className="flex-1 btn btn-success flex items-center justify-center space-x-2"
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
      </Modal>

      {/* Identify Modal */}
      <Modal isOpen={showIdentifyModal} onClose={() => { setShowIdentifyModal(false); setAudioBlob(null); setIdentifyResult(null); }} title={t('speakers.identifySpeaker')}>
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
              className="flex-1 btn btn-danger flex items-center justify-center space-x-2"
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
              className="flex-1 btn btn-success flex items-center justify-center space-x-2"
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
            disabled={!audioBlob || identifying || identifyResult !== null}
            className="flex-1 btn btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {identifying ? (
              <Loader className="w-4 h-4 animate-spin mx-auto" />
            ) : (
              t('speakers.identify')
            )}
          </button>
        </div>
      </Modal>

      {/* Edit Speaker Modal */}
      <Modal isOpen={showEditModal && !!selectedSpeaker} onClose={() => setShowEditModal(false)} title={t('speakers.editSpeaker')}>
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
              className="w-4 h-4 rounded-sm"
            />
            <label htmlFor="editIsAdmin" className="text-sm text-gray-600 dark:text-gray-300 flex items-center space-x-2">
              <Shield className="w-4 h-4" />
              <span>{t('speakers.adminPermission')}</span>
            </label>
          </div>

          <div className="text-sm text-gray-500">
            {t('speakers.voiceSamples')}: {selectedSpeaker?.embedding_count}
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
      </Modal>

      {/* Merge Speakers Modal */}
      <Modal isOpen={showMergeModal && !!selectedSpeaker} onClose={() => { setShowMergeModal(false); setSelectedSpeaker(null); setMergeTargetId(null); }} title={t('speakers.mergeSpeakers')}>
        <p className="text-gray-500 dark:text-gray-400 mb-4">
          {t('speakers.mergeSubtitle', { name: selectedSpeaker?.name })}
        </p>

        <div className="bg-gray-100 dark:bg-gray-800 rounded-lg p-4 mb-4">
          <div className="flex items-center space-x-3 mb-4">
            <div className="p-2 bg-purple-600 rounded-lg">
              <GitMerge className="w-5 h-5 text-white" />
            </div>
            <div>
              <p className="text-gray-900 dark:text-white font-medium">{selectedSpeaker?.name}</p>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {selectedSpeaker?.embedding_count} {t('speakers.voiceSamples')}
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
                .filter(s => s.id !== selectedSpeaker?.id)
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
            <AlertCircle className="w-5 h-5 text-yellow-600 dark:text-yellow-500 shrink-0 mt-0.5" />
            <p className="text-sm text-yellow-700 dark:text-yellow-400">
              {t('speakers.mergeWarning', { name: selectedSpeaker?.name })}
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
            className="flex-1 btn btn-secondary disabled:opacity-50"
          >
            {merging ? (
              <Loader className="w-4 h-4 animate-spin mx-auto" aria-label={t('common.loading')} />
            ) : (
              t('speakers.merge')
            )}
          </button>
        </div>
      </Modal>

      {/* Confirm Dialog */}
      {ConfirmDialogComponent}
    </div>
  );
}
