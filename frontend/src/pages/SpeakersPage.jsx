import React, { useState, useEffect, useRef } from 'react';
import {
  Users, UserPlus, Mic, MicOff, Trash2, Loader, CheckCircle,
  XCircle, AlertCircle, Volume2, Shield, ShieldCheck, RefreshCw
} from 'lucide-react';
import apiClient from '../utils/axios';

export default function SpeakersPage() {
  // State
  const [speakers, setSpeakers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [serviceStatus, setServiceStatus] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEnrollModal, setShowEnrollModal] = useState(false);
  const [showIdentifyModal, setShowIdentifyModal] = useState(false);
  const [selectedSpeaker, setSelectedSpeaker] = useState(null);
  const [recording, setRecording] = useState(false);
  const [audioBlob, setAudioBlob] = useState(null);
  const [enrolling, setEnrolling] = useState(false);
  const [identifying, setIdentifying] = useState(false);
  const [identifyResult, setIdentifyResult] = useState(null);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Form state
  const [newSpeakerName, setNewSpeakerName] = useState('');
  const [newSpeakerAlias, setNewSpeakerAlias] = useState('');
  const [newSpeakerIsAdmin, setNewSpeakerIsAdmin] = useState(false);

  // Refs
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const streamRef = useRef(null);

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
      setServiceStatus({ available: false, message: 'Service nicht erreichbar' });
    }
  };

  const loadSpeakers = async () => {
    try {
      setLoading(true);
      const response = await apiClient.get('/api/speakers');
      setSpeakers(response.data);
    } catch (err) {
      console.error('Failed to load speakers:', err);
      setError('Sprecher konnten nicht geladen werden');
    } finally {
      setLoading(false);
    }
  };

  const createSpeaker = async () => {
    if (!newSpeakerName.trim() || !newSpeakerAlias.trim()) {
      setError('Name und Alias sind erforderlich');
      return;
    }

    try {
      await apiClient.post('/api/speakers', {
        name: newSpeakerName,
        alias: newSpeakerAlias,
        is_admin: newSpeakerIsAdmin
      });

      setSuccess(`Sprecher "${newSpeakerName}" erstellt`);
      setShowCreateModal(false);
      setNewSpeakerName('');
      setNewSpeakerAlias('');
      setNewSpeakerIsAdmin(false);
      loadSpeakers();
    } catch (err) {
      console.error('Failed to create speaker:', err);
      setError(err.response?.data?.detail || 'Sprecher konnte nicht erstellt werden');
    }
  };

  const deleteSpeaker = async (speaker) => {
    if (!confirm(`Sprecher "${speaker.name}" wirklich loeschen?`)) {
      return;
    }

    try {
      await apiClient.delete(`/api/speakers/${speaker.id}`);
      setSuccess(`Sprecher "${speaker.name}" geloescht`);
      loadSpeakers();
    } catch (err) {
      console.error('Failed to delete speaker:', err);
      setError('Sprecher konnte nicht geloescht werden');
    }
  };

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      mediaRecorder.ondataavailable = (event) => {
        audioChunksRef.current.push(event.data);
      };

      mediaRecorder.onstop = () => {
        const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
        setAudioBlob(blob);

        // Stop stream
        if (streamRef.current) {
          streamRef.current.getTracks().forEach(track => track.stop());
        }
      };

      mediaRecorder.start();
      setRecording(true);
    } catch (err) {
      console.error('Failed to start recording:', err);
      setError('Mikrofon-Zugriff nicht moeglich');
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
      setError('Bitte zuerst eine Aufnahme machen');
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
      setError(err.response?.data?.detail || 'Voice Sample konnte nicht gespeichert werden');
    } finally {
      setEnrolling(false);
    }
  };

  const identifySpeaker = async () => {
    if (!audioBlob) {
      setError('Bitte zuerst eine Aufnahme machen');
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
      setError(err.response?.data?.detail || 'Identifikation fehlgeschlagen');
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
            <h1 className="text-2xl font-bold text-white mb-2">Sprechererkennung</h1>
            <p className="text-gray-400">Verwalte Sprecher und Voice Samples</p>
          </div>
          <div className="flex items-center space-x-2">
            <button
              onClick={loadSpeakers}
              className="p-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300"
              title="Aktualisieren"
            >
              <RefreshCw className="w-5 h-5" />
            </button>
          </div>
        </div>
      </div>

      {/* Service Status */}
      {serviceStatus && (
        <div className={`card ${serviceStatus.available ? 'bg-green-900/20 border-green-700' : 'bg-red-900/20 border-red-700'}`}>
          <div className="flex items-center space-x-3">
            {serviceStatus.available ? (
              <CheckCircle className="w-5 h-5 text-green-500" />
            ) : (
              <XCircle className="w-5 h-5 text-red-500" />
            )}
            <div>
              <p className={`font-medium ${serviceStatus.available ? 'text-green-400' : 'text-red-400'}`}>
                {serviceStatus.available ? 'Speaker Recognition aktiv' : 'Speaker Recognition nicht verfuegbar'}
              </p>
              <p className="text-sm text-gray-400">{serviceStatus.message}</p>
            </div>
          </div>
        </div>
      )}

      {/* Alerts */}
      {error && (
        <div className="card bg-red-900/20 border-red-700">
          <div className="flex items-center space-x-3">
            <AlertCircle className="w-5 h-5 text-red-500" />
            <p className="text-red-400">{error}</p>
          </div>
        </div>
      )}

      {success && (
        <div className="card bg-green-900/20 border-green-700">
          <div className="flex items-center space-x-3">
            <CheckCircle className="w-5 h-5 text-green-500" />
            <p className="text-green-400">{success}</p>
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
          <span>Neuer Sprecher</span>
        </button>

        <button
          onClick={openIdentifyModal}
          className="btn bg-purple-600 hover:bg-purple-700 text-white flex items-center space-x-2"
          disabled={!serviceStatus?.available || speakers.length === 0}
        >
          <Volume2 className="w-4 h-4" />
          <span>Sprecher identifizieren</span>
        </button>
      </div>

      {/* Speakers List */}
      <div>
        <h2 className="text-xl font-semibold text-white mb-4">
          Registrierte Sprecher ({speakers.length})
        </h2>

        {loading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-400 mb-2" />
            <p className="text-gray-400">Lade Sprecher...</p>
          </div>
        ) : speakers.length === 0 ? (
          <div className="card text-center py-12">
            <Users className="w-12 h-12 mx-auto text-gray-600 mb-4" />
            <p className="text-gray-400 mb-4">Noch keine Sprecher registriert</p>
            <button
              onClick={() => setShowCreateModal(true)}
              className="btn btn-primary"
              disabled={!serviceStatus?.available}
            >
              Ersten Sprecher anlegen
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
                      <p className="text-white font-medium">{speaker.name}</p>
                      <p className="text-sm text-gray-400">@{speaker.alias}</p>
                    </div>
                  </div>
                  {speaker.is_admin && (
                    <span className="px-2 py-1 bg-yellow-600/20 text-yellow-400 text-xs rounded">
                      Admin
                    </span>
                  )}
                </div>

                <div className="flex items-center justify-between text-sm mb-4">
                  <span className="text-gray-400">Voice Samples:</span>
                  <span className={`font-medium ${
                    speaker.embedding_count >= 3 ? 'text-green-400' :
                    speaker.embedding_count > 0 ? 'text-yellow-400' : 'text-red-400'
                  }`}>
                    {speaker.embedding_count} {speaker.embedding_count >= 3 ? '(gut)' : speaker.embedding_count > 0 ? '(mehr empfohlen)' : '(keine)'}
                  </span>
                </div>

                <div className="flex space-x-2">
                  <button
                    onClick={() => openEnrollModal(speaker)}
                    className="flex-1 btn bg-green-600 hover:bg-green-700 text-white text-sm flex items-center justify-center space-x-1"
                  >
                    <Mic className="w-4 h-4" />
                    <span>Aufnehmen</span>
                  </button>
                  <button
                    onClick={() => deleteSpeaker(speaker)}
                    className="p-2 rounded-lg bg-red-600/20 hover:bg-red-600/40 text-red-400"
                    title="Loeschen"
                  >
                    <Trash2 className="w-4 h-4" />
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
            <h2 className="text-xl font-bold text-white mb-4">Neuen Sprecher anlegen</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-400 mb-1">Name</label>
                <input
                  type="text"
                  value={newSpeakerName}
                  onChange={(e) => setNewSpeakerName(e.target.value)}
                  placeholder="Max Mustermann"
                  className="input w-full"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-1">Alias (fuer Ansprache)</label>
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
                <label htmlFor="isAdmin" className="text-sm text-gray-300 flex items-center space-x-2">
                  <Shield className="w-4 h-4" />
                  <span>Administrator-Berechtigung</span>
                </label>
              </div>
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowCreateModal(false)}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                Abbrechen
              </button>
              <button
                onClick={createSpeaker}
                className="flex-1 btn btn-primary"
              >
                Erstellen
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Enroll Modal */}
      {showEnrollModal && selectedSpeaker && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-white mb-2">Voice Sample aufnehmen</h2>
            <p className="text-gray-400 mb-4">Fuer: {selectedSpeaker.name}</p>

            <div className="bg-gray-800 rounded-lg p-6 mb-4">
              <div className="text-center">
                {recording ? (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-red-600 rounded-full flex items-center justify-center animate-pulse">
                      <Mic className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-red-400 font-medium">Aufnahme laeuft...</p>
                    <p className="text-sm text-gray-400 mt-2">Sprich 3-10 Sekunden deutlich</p>
                  </div>
                ) : audioBlob ? (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-green-600 rounded-full flex items-center justify-center">
                      <CheckCircle className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-green-400 font-medium">Aufnahme bereit</p>
                    <p className="text-sm text-gray-400 mt-2">
                      {(audioBlob.size / 1024).toFixed(1)} KB
                    </p>
                  </div>
                ) : (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-gray-700 rounded-full flex items-center justify-center">
                      <Mic className="w-8 h-8 text-gray-400" />
                    </div>
                    <p className="text-gray-400">Bereit zur Aufnahme</p>
                    <p className="text-sm text-gray-500 mt-2">Sprich einen beliebigen Satz</p>
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
                  <span>Stoppen</span>
                </button>
              ) : (
                <button
                  onClick={startRecording}
                  className="flex-1 btn bg-green-600 hover:bg-green-700 text-white flex items-center justify-center space-x-2"
                >
                  <Mic className="w-4 h-4" />
                  <span>Aufnehmen</span>
                </button>
              )}
            </div>

            <div className="flex space-x-3">
              <button
                onClick={() => {
                  setShowEnrollModal(false);
                  setAudioBlob(null);
                }}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                Abbrechen
              </button>
              <button
                onClick={enrollVoiceSample}
                disabled={!audioBlob || enrolling}
                className="flex-1 btn btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {enrolling ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  'Speichern'
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
            <h2 className="text-xl font-bold text-white mb-2">Sprecher identifizieren</h2>
            <p className="text-gray-400 mb-4">Nimm eine Sprachprobe auf um den Sprecher zu erkennen</p>

            <div className="bg-gray-800 rounded-lg p-6 mb-4">
              <div className="text-center">
                {recording ? (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-red-600 rounded-full flex items-center justify-center animate-pulse">
                      <Mic className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-red-400 font-medium">Aufnahme laeuft...</p>
                  </div>
                ) : audioBlob && !identifyResult ? (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-green-600 rounded-full flex items-center justify-center">
                      <CheckCircle className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-green-400 font-medium">Aufnahme bereit</p>
                  </div>
                ) : identifyResult ? (
                  <div>
                    {identifyResult.is_identified ? (
                      <>
                        <div className="w-16 h-16 mx-auto mb-4 bg-green-600 rounded-full flex items-center justify-center">
                          <CheckCircle className="w-8 h-8 text-white" />
                        </div>
                        <p className="text-green-400 font-medium text-lg">{identifyResult.speaker_name}</p>
                        <p className="text-gray-400">@{identifyResult.speaker_alias}</p>
                        <p className="text-sm text-gray-500 mt-2">
                          Konfidenz: {(identifyResult.confidence * 100).toFixed(1)}%
                        </p>
                      </>
                    ) : (
                      <>
                        <div className="w-16 h-16 mx-auto mb-4 bg-yellow-600 rounded-full flex items-center justify-center">
                          <AlertCircle className="w-8 h-8 text-white" />
                        </div>
                        <p className="text-yellow-400 font-medium">Sprecher nicht erkannt</p>
                        <p className="text-sm text-gray-400 mt-2">
                          Kein registrierter Sprecher gefunden
                        </p>
                      </>
                    )}
                  </div>
                ) : (
                  <div>
                    <div className="w-16 h-16 mx-auto mb-4 bg-purple-600 rounded-full flex items-center justify-center">
                      <Volume2 className="w-8 h-8 text-white" />
                    </div>
                    <p className="text-gray-400">Bereit zur Identifikation</p>
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
                  <span>Stoppen</span>
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
                  <span>{identifyResult ? 'Erneut aufnehmen' : 'Aufnehmen'}</span>
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
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                Schliessen
              </button>
              <button
                onClick={identifySpeaker}
                disabled={!audioBlob || identifying || identifyResult}
                className="flex-1 btn btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {identifying ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  'Identifizieren'
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
