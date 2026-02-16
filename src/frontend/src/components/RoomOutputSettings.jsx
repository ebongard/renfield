import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Volume2, Plus, Trash2, Loader, ChevronDown, ChevronUp,
  GripVertical, Power, PowerOff, Speaker, Radio, Monitor
} from 'lucide-react';
import apiClient from '../utils/axios';
import { useConfirmDialog } from './ConfirmDialog';
import { useAuth } from '../context/AuthContext';

// Output device type icons
const OUTPUT_TYPE_ICONS = {
  renfield: Radio,
  homeassistant: Speaker,
};

/**
 * RoomOutputSettings - Manage audio output devices for a room
 *
 * Features:
 * - List configured output devices with priority order
 * - Add new output devices (Renfield devices or HA media players)
 * - Edit device settings (priority, interruption, volume)
 * - Delete output devices
 * - Drag-and-drop reordering (simplified: up/down buttons)
 */
export default function RoomOutputSettings({ roomId, roomName }) {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();
  const { isFeatureEnabled } = useAuth();
  const showHA = isFeatureEnabled('smart_home');
  // State
  const [expanded, setExpanded] = useState(false);
  const [outputDevices, setOutputDevices] = useState([]);
  const [availableOutputs, setAvailableOutputs] = useState({ renfield_devices: [], ha_media_players: [] });
  const [loading, setLoading] = useState(false);
  const [loadingAvailable, setLoadingAvailable] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);
  const [error, setError] = useState(null);

  // Add form state
  const [selectedType, setSelectedType] = useState(showHA ? 'homeassistant' : 'renfield'); // 'renfield' or 'homeassistant'
  const [selectedDevice, setSelectedDevice] = useState('');
  const [allowInterruption, setAllowInterruption] = useState(false);
  const [ttsVolume, setTtsVolume] = useState(50);
  const [adding, setAdding] = useState(false);

  // Load output devices when expanded
  useEffect(() => {
    if (expanded) {
      loadOutputDevices();
    }
  }, [expanded, roomId]);

  const loadOutputDevices = async () => {
    try {
      setLoading(true);
      const response = await apiClient.get(`/api/rooms/${roomId}/output-devices`);
      setOutputDevices(response.data);
    } catch (err) {
      console.error('Failed to load output devices:', err);
      setError('Ausgabegeraete konnten nicht geladen werden');
    } finally {
      setLoading(false);
    }
  };

  const loadAvailableOutputs = async () => {
    try {
      setLoadingAvailable(true);
      const response = await apiClient.get(`/api/rooms/${roomId}/available-outputs`);
      setAvailableOutputs(response.data);
    } catch (err) {
      console.error('Failed to load available outputs:', err);
    } finally {
      setLoadingAvailable(false);
    }
  };

  const openAddModal = () => {
    loadAvailableOutputs();
    setSelectedType(showHA ? 'homeassistant' : 'renfield');
    setSelectedDevice('');
    setAllowInterruption(false);
    setTtsVolume(50);
    setShowAddModal(true);
  };

  const addOutputDevice = async () => {
    if (!selectedDevice) {
      setError('Bitte ein Geraet auswaehlen');
      return;
    }

    try {
      setAdding(true);

      const payload = {
        output_type: 'audio',
        allow_interruption: allowInterruption,
        tts_volume: ttsVolume / 100,
        priority: outputDevices.length + 1,
      };

      if (selectedType === 'renfield') {
        payload.renfield_device_id = selectedDevice;
      } else {
        payload.ha_entity_id = selectedDevice;
      }

      await apiClient.post(`/api/rooms/${roomId}/output-devices`, payload);
      setShowAddModal(false);
      loadOutputDevices();
    } catch (err) {
      console.error('Failed to add output device:', err);
      setError(err.response?.data?.detail || 'Geraet konnte nicht hinzugefuegt werden');
    } finally {
      setAdding(false);
    }
  };

  const updateOutputDevice = async (deviceId, updates) => {
    try {
      await apiClient.patch(`/api/rooms/output-devices/${deviceId}`, updates);
      loadOutputDevices();
    } catch (err) {
      console.error('Failed to update output device:', err);
      setError('Geraet konnte nicht aktualisiert werden');
    }
  };

  const deleteOutputDevice = async (deviceId) => {
    const confirmed = await confirm({
      title: t('rooms.removeOutputDevice'),
      message: t('rooms.removeOutputDeviceConfirm'),
      variant: 'danger',
    });
    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/rooms/output-devices/${deviceId}`);
      loadOutputDevices();
    } catch (err) {
      console.error('Failed to delete output device:', err);
      setError('Geraet konnte nicht entfernt werden');
    }
  };

  const moveDevice = async (index, direction) => {
    const newDevices = [...outputDevices];
    const targetIndex = direction === 'up' ? index - 1 : index + 1;

    if (targetIndex < 0 || targetIndex >= newDevices.length) return;

    // Swap devices
    [newDevices[index], newDevices[targetIndex]] = [newDevices[targetIndex], newDevices[index]];

    // Get new order of IDs
    const deviceIds = newDevices.map(d => d.id);

    try {
      await apiClient.post(`/api/rooms/${roomId}/output-devices/reorder?output_type=audio`, {
        device_ids: deviceIds
      });
      loadOutputDevices();
    } catch (err) {
      console.error('Failed to reorder devices:', err);
      setError('Reihenfolge konnte nicht geaendert werden');
    }
  };

  const getDeviceIcon = (device) => {
    if (device.renfield_device_id) {
      return <Radio className="w-4 h-4 text-green-400" />;
    }
    return <Speaker className="w-4 h-4 text-blue-400" />;
  };

  // Filter out already configured devices from available list
  const getAvailableDevices = () => {
    const configuredRenfieldIds = new Set(
      outputDevices.filter(d => d.renfield_device_id).map(d => d.renfield_device_id)
    );
    const configuredHAIds = new Set(
      outputDevices.filter(d => d.ha_entity_id).map(d => d.ha_entity_id)
    );

    if (selectedType === 'renfield') {
      return availableOutputs.renfield_devices.filter(
        d => !configuredRenfieldIds.has(d.device_id)
      );
    } else if (showHA) {
      return availableOutputs.ha_media_players.filter(
        d => !configuredHAIds.has(d.entity_id)
      );
    }
    return [];
  };

  return (
    <div className="mt-4 border-t border-gray-700 pt-4">
      {/* Header - Click to expand */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between text-left hover:bg-gray-800 rounded-lg p-2 -m-2"
      >
        <div className="flex items-center space-x-2">
          <Volume2 className="w-4 h-4 text-gray-400" />
          <span className="text-sm text-gray-300">Audio-Ausgabe</span>
          {outputDevices.length > 0 && (
            <span className="text-xs text-gray-500">({outputDevices.length})</span>
          )}
        </div>
        {expanded ? (
          <ChevronUp className="w-4 h-4 text-gray-400" />
        ) : (
          <ChevronDown className="w-4 h-4 text-gray-400" />
        )}
      </button>

      {/* Expanded Content */}
      {expanded && (
        <div className="mt-4 space-y-3">
          {/* Error Message */}
          {error && (
            <div className="text-red-400 text-xs bg-red-900/20 p-2 rounded-sm">
              {error}
              <button onClick={() => setError(null)} className="ml-2 underline">
                OK
              </button>
            </div>
          )}

          {/* Loading */}
          {loading ? (
            <div className="text-center py-4">
              <Loader className="w-5 h-5 animate-spin mx-auto text-gray-400" />
            </div>
          ) : outputDevices.length === 0 ? (
            <p className="text-gray-500 text-xs text-center py-2">
              Keine Ausgabegeraete konfiguriert.
              <br />
              TTS wird auf dem Eingabegeraet abgespielt.
            </p>
          ) : (
            /* Output Devices List */
            <div className="space-y-2">
              {outputDevices.map((device, index) => (
                <div
                  key={device.id}
                  className={`flex items-center space-x-2 p-2 rounded-lg ${
                    device.is_enabled ? 'bg-gray-800' : 'bg-gray-800/50 opacity-50'
                  }`}
                >
                  {/* Priority Badge */}
                  <span className="w-5 h-5 bg-gray-700 rounded-sm text-xs flex items-center justify-center text-gray-400">
                    {index + 1}
                  </span>

                  {/* Device Icon */}
                  {getDeviceIcon(device)}

                  {/* Device Name */}
                  <span className="flex-1 text-sm text-gray-300 truncate">
                    {device.device_name || device.ha_entity_id || device.renfield_device_id}
                  </span>

                  {/* Volume Badge */}
                  {device.tts_volume !== null && (
                    <span className="text-xs text-gray-500">
                      {Math.round(device.tts_volume * 100)}%
                    </span>
                  )}

                  {/* Interruption Badge */}
                  {device.allow_interruption && (
                    <span className="text-xs text-yellow-400" title="Unterbricht laufende Wiedergabe">
                      INT
                    </span>
                  )}

                  {/* Enable/Disable */}
                  <button
                    onClick={() => updateOutputDevice(device.id, { is_enabled: !device.is_enabled })}
                    className={`p-1 rounded-sm ${device.is_enabled ? 'text-green-400' : 'text-gray-500'}`}
                    title={device.is_enabled ? 'Deaktivieren' : 'Aktivieren'}
                  >
                    {device.is_enabled ? <Power className="w-3 h-3" /> : <PowerOff className="w-3 h-3" />}
                  </button>

                  {/* Move Up/Down */}
                  <div className="flex flex-col">
                    <button
                      onClick={() => moveDevice(index, 'up')}
                      disabled={index === 0}
                      className="text-gray-500 hover:text-gray-300 disabled:opacity-30"
                    >
                      <ChevronUp className="w-3 h-3" />
                    </button>
                    <button
                      onClick={() => moveDevice(index, 'down')}
                      disabled={index === outputDevices.length - 1}
                      className="text-gray-500 hover:text-gray-300 disabled:opacity-30"
                    >
                      <ChevronDown className="w-3 h-3" />
                    </button>
                  </div>

                  {/* Delete */}
                  <button
                    onClick={() => deleteOutputDevice(device.id)}
                    className="p-1 text-red-400 hover:text-red-300"
                    title="Entfernen"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Add Button */}
          <button
            onClick={openAddModal}
            className="w-full flex items-center justify-center space-x-2 py-2 text-sm text-gray-400 hover:text-gray-300 border border-dashed border-gray-700 rounded-lg hover:border-gray-600"
          >
            <Plus className="w-4 h-4" />
            <span>Ausgabegeraet hinzufuegen</span>
          </button>
        </div>
      )}

      {ConfirmDialogComponent}

      {/* Add Device Modal */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-white mb-4">Ausgabegeraet hinzufuegen</h2>
            <p className="text-gray-400 text-sm mb-4">Raum: {roomName}</p>

            <div className="space-y-4">
              {/* Device Type Selector */}
              <div>
                <label className="block text-sm text-gray-400 mb-2">Geraetetyp:</label>
                <div className="flex space-x-2">
                  {showHA && (
                    <button
                      onClick={() => {
                        setSelectedType('homeassistant');
                        setSelectedDevice('');
                      }}
                      className={`flex-1 p-3 rounded-lg border ${
                        selectedType === 'homeassistant'
                          ? 'border-blue-500 bg-blue-500/20'
                          : 'border-gray-700 bg-gray-800'
                      }`}
                    >
                      <Speaker className="w-5 h-5 mx-auto mb-1 text-blue-400" />
                      <span className="text-sm text-gray-300">HA Media Player</span>
                    </button>
                  )}
                  <button
                    onClick={() => {
                      setSelectedType('renfield');
                      setSelectedDevice('');
                    }}
                    className={`flex-1 p-3 rounded-lg border ${
                      selectedType === 'renfield'
                        ? 'border-green-500 bg-green-500/20'
                        : 'border-gray-700 bg-gray-800'
                    }`}
                  >
                    <Radio className="w-5 h-5 mx-auto mb-1 text-green-400" />
                    <span className="text-sm text-gray-300">Renfield Geraet</span>
                  </button>
                </div>
              </div>

              {/* Device Selector */}
              <div>
                <label className="block text-sm text-gray-400 mb-2">Geraet:</label>
                {loadingAvailable ? (
                  <div className="text-center py-4">
                    <Loader className="w-5 h-5 animate-spin mx-auto text-gray-400" />
                  </div>
                ) : (
                  <select
                    value={selectedDevice}
                    onChange={(e) => setSelectedDevice(e.target.value)}
                    className="input w-full"
                  >
                    <option value="">-- Geraet auswaehlen --</option>
                    {getAvailableDevices().map((device) => (
                      <option
                        key={selectedType === 'renfield' ? device.device_id : device.entity_id}
                        value={selectedType === 'renfield' ? device.device_id : device.entity_id}
                      >
                        {selectedType === 'renfield'
                          ? device.device_name || device.device_id
                          : device.friendly_name || device.entity_id}
                      </option>
                    ))}
                  </select>
                )}
                {getAvailableDevices().length === 0 && !loadingAvailable && (
                  <p className="text-yellow-400 text-xs mt-2">
                    Keine verfuegbaren Geraete gefunden
                  </p>
                )}
              </div>

              {/* TTS Volume */}
              <div>
                <label className="block text-sm text-gray-400 mb-2">
                  TTS Lautstaerke: {ttsVolume}%
                </label>
                <input
                  type="range"
                  min="0"
                  max="100"
                  value={ttsVolume}
                  onChange={(e) => setTtsVolume(parseInt(e.target.value))}
                  className="w-full"
                />
                <p className="text-xs text-gray-500 mt-1">
                  Lautstaerke fuer TTS-Ausgabe (0 = keine Aenderung)
                </p>
              </div>

              {/* Allow Interruption */}
              <div className="flex items-center space-x-3">
                <input
                  type="checkbox"
                  id="allowInterruption"
                  checked={allowInterruption}
                  onChange={(e) => setAllowInterruption(e.target.checked)}
                  className="w-4 h-4"
                />
                <label htmlFor="allowInterruption" className="text-sm text-gray-300">
                  Unterbrechung erlauben
                </label>
              </div>
              <p className="text-xs text-gray-500 -mt-2">
                Wenn aktiviert, wird laufende Wiedergabe unterbrochen
              </p>
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowAddModal(false)}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                Abbrechen
              </button>
              <button
                onClick={addOutputDevice}
                disabled={!selectedDevice || adding}
                className="flex-1 btn btn-primary disabled:opacity-50"
              >
                {adding ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  'Hinzufuegen'
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
