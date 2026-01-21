import React, { useState, useEffect } from 'react';
import {
  Home, Plus, Edit3, Trash2, Loader, CheckCircle, XCircle,
  AlertCircle, RefreshCw, Link as LinkIcon, Unlink, Radio,
  ArrowDownToLine, ArrowUpFromLine, ArrowLeftRight,
  Monitor, Tablet, Smartphone, Tv
} from 'lucide-react';
import apiClient from '../utils/axios';
import RoomOutputSettings from '../components/RoomOutputSettings';
import { useConfirmDialog } from '../components/ConfirmDialog';
import Modal from '../components/Modal';

// Device type icons and labels
const DEVICE_TYPE_CONFIG = {
  satellite: { icon: Radio, label: 'Satellite', color: 'text-green-400' },
  web_panel: { icon: Monitor, label: 'Panel', color: 'text-blue-400' },
  web_tablet: { icon: Tablet, label: 'Tablet', color: 'text-purple-400' },
  web_browser: { icon: Smartphone, label: 'Browser', color: 'text-gray-400' },
  web_kiosk: { icon: Tv, label: 'Kiosk', color: 'text-yellow-400' },
};

export default function RoomsPage() {
  // Confirm dialog hook
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  // State
  const [rooms, setRooms] = useState([]);
  const [haAreas, setHAAreas] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingAreas, setLoadingAreas] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showLinkModal, setShowLinkModal] = useState(false);
  const [showSyncPanel, setShowSyncPanel] = useState(false);
  const [selectedRoom, setSelectedRoom] = useState(null);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Form state
  const [newRoomName, setNewRoomName] = useState('');
  const [newRoomIcon, setNewRoomIcon] = useState('');
  const [editRoomName, setEditRoomName] = useState('');
  const [editRoomIcon, setEditRoomIcon] = useState('');
  const [updating, setUpdating] = useState(false);
  const [selectedHAArea, setSelectedHAArea] = useState('');
  const [conflictResolution, setConflictResolution] = useState('link');

  // Load data on mount
  useEffect(() => {
    loadRooms();
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

  const loadRooms = async () => {
    try {
      setLoading(true);
      const response = await apiClient.get('/api/rooms');
      setRooms(response.data);
    } catch (err) {
      console.error('Failed to load rooms:', err);
      setError('Raeume konnten nicht geladen werden');
    } finally {
      setLoading(false);
    }
  };

  const loadHAAreas = async () => {
    try {
      setLoadingAreas(true);
      const response = await apiClient.get('/api/rooms/ha/areas');
      setHAAreas(response.data);
    } catch (err) {
      console.error('Failed to load HA areas:', err);
      setError('Home Assistant Areas konnten nicht geladen werden');
    } finally {
      setLoadingAreas(false);
    }
  };

  const createRoom = async () => {
    if (!newRoomName.trim()) {
      setError('Name ist erforderlich');
      return;
    }

    try {
      await apiClient.post('/api/rooms', {
        name: newRoomName,
        icon: newRoomIcon || null
      });

      setSuccess(`Raum "${newRoomName}" erstellt`);
      setShowCreateModal(false);
      setNewRoomName('');
      setNewRoomIcon('');
      loadRooms();
    } catch (err) {
      console.error('Failed to create room:', err);
      setError(err.response?.data?.detail || 'Raum konnte nicht erstellt werden');
    }
  };

  const updateRoom = async () => {
    if (!selectedRoom || !editRoomName.trim()) {
      setError('Name ist erforderlich');
      return;
    }

    try {
      setUpdating(true);
      await apiClient.patch(`/api/rooms/${selectedRoom.id}`, {
        name: editRoomName,
        icon: editRoomIcon || null
      });

      setSuccess(`Raum "${editRoomName}" aktualisiert`);
      setShowEditModal(false);
      loadRooms();
    } catch (err) {
      console.error('Failed to update room:', err);
      setError(err.response?.data?.detail || 'Raum konnte nicht aktualisiert werden');
    } finally {
      setUpdating(false);
    }
  };

  const deleteRoom = async (room) => {
    const confirmed = await confirm({
      title: 'Raum löschen?',
      message: `Möchtest du "${room.name}" wirklich löschen? Diese Aktion kann nicht rückgängig gemacht werden.`,
      confirmLabel: 'Löschen',
      cancelLabel: 'Abbrechen',
      variant: 'danger',
    });

    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/rooms/${room.id}`);
      setSuccess(`Raum "${room.name}" gelöscht`);
      loadRooms();
    } catch (err) {
      console.error('Failed to delete room:', err);
      setError('Raum konnte nicht gelöscht werden');
    }
  };

  const linkToHAArea = async () => {
    if (!selectedRoom || !selectedHAArea) {
      setError('Bitte einen Area auswaehlen');
      return;
    }

    try {
      setUpdating(true);
      await apiClient.post(`/api/rooms/${selectedRoom.id}/link/${selectedHAArea}`);
      setSuccess(`Raum mit HA Area verknuepft`);
      setShowLinkModal(false);
      setSelectedHAArea('');
      loadRooms();
    } catch (err) {
      console.error('Failed to link room:', err);
      setError(err.response?.data?.detail || 'Verknuepfung fehlgeschlagen');
    } finally {
      setUpdating(false);
    }
  };

  const unlinkFromHA = async (room) => {
    const confirmed = await confirm({
      title: 'Verknüpfung aufheben?',
      message: `Möchtest du die Home Assistant Verknüpfung von "${room.name}" wirklich aufheben?`,
      confirmLabel: 'Trennen',
      cancelLabel: 'Abbrechen',
      variant: 'warning',
    });

    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/rooms/${room.id}/link`);
      setSuccess(`Verknüpfung aufgehoben`);
      loadRooms();
    } catch (err) {
      console.error('Failed to unlink room:', err);
      setError('Verknüpfung konnte nicht aufgehoben werden');
    }
  };

  const importFromHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post('/api/rooms/ha/import', {
        conflict_resolution: conflictResolution
      });

      const { imported, linked, skipped } = response.data;
      setSuccess(`Import: ${imported} neu, ${linked} verknuepft, ${skipped} uebersprungen`);
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to import from HA:', err);
      setError(err.response?.data?.detail || 'Import fehlgeschlagen');
    } finally {
      setSyncing(false);
    }
  };

  const exportToHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post('/api/rooms/ha/export');

      const { exported, linked } = response.data;
      setSuccess(`Export: ${exported} neu erstellt, ${linked} verknuepft`);
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to export to HA:', err);
      setError(err.response?.data?.detail || 'Export fehlgeschlagen');
    } finally {
      setSyncing(false);
    }
  };

  const syncWithHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post(`/api/rooms/ha/sync?conflict_resolution=${conflictResolution}`);

      const { import_results, export_results } = response.data;
      setSuccess(
        `Sync: ${import_results.imported + import_results.linked} importiert, ` +
        `${export_results.exported + export_results.linked} exportiert`
      );
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to sync with HA:', err);
      setError(err.response?.data?.detail || 'Sync fehlgeschlagen');
    } finally {
      setSyncing(false);
    }
  };

  const openEditModal = (room) => {
    setSelectedRoom(room);
    setEditRoomName(room.name);
    setEditRoomIcon(room.icon || '');
    setShowEditModal(true);
  };

  const openLinkModal = (room) => {
    setSelectedRoom(room);
    setSelectedHAArea('');
    loadHAAreas();
    setShowLinkModal(true);
  };

  const openSyncPanel = () => {
    loadHAAreas();
    setShowSyncPanel(true);
  };

  const getSourceBadge = (source) => {
    switch (source) {
      case 'homeassistant':
        return <span className="px-2 py-1 bg-blue-600/20 text-blue-400 text-xs rounded">HA</span>;
      case 'satellite':
        return <span className="px-2 py-1 bg-green-600/20 text-green-400 text-xs rounded">Satellite</span>;
      default:
        return <span className="px-2 py-1 bg-gray-600/20 text-gray-400 text-xs rounded">Renfield</span>;
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-white mb-2">Raumverwaltung</h1>
            <p className="text-gray-400">Verwalte Raeume und synchronisiere mit Home Assistant</p>
          </div>
          <div className="flex items-center space-x-2">
            <button
              onClick={loadRooms}
              className="p-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300"
              aria-label="Räume aktualisieren"
            >
              <RefreshCw className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>

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
        >
          <Plus className="w-4 h-4" />
          <span>Neuer Raum</span>
        </button>

        <button
          onClick={openSyncPanel}
          className="btn bg-blue-600 hover:bg-blue-700 text-white flex items-center space-x-2"
        >
          <ArrowLeftRight className="w-4 h-4" />
          <span>HA Sync</span>
        </button>
      </div>

      {/* Rooms List */}
      <div>
        <h2 className="text-xl font-semibold text-white mb-4">
          Raeume ({rooms.length})
        </h2>

        {loading ? (
          <div className="card text-center py-12" role="status" aria-label="Räume werden geladen">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-400 mb-2" aria-hidden="true" />
            <p className="text-gray-400">Lade Räume...</p>
          </div>
        ) : rooms.length === 0 ? (
          <div className="card text-center py-12">
            <Home className="w-12 h-12 mx-auto text-gray-600 mb-4" />
            <p className="text-gray-400 mb-4">Noch keine Raeume vorhanden</p>
            <button
              onClick={() => setShowCreateModal(true)}
              className="btn btn-primary"
            >
              Ersten Raum anlegen
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {rooms.map((room) => (
              <div key={room.id} className="card">
                <div className="flex items-start justify-between mb-4">
                  <div className="flex items-center space-x-3">
                    <div className="p-3 rounded-lg bg-primary-600">
                      <Home className="w-6 h-6 text-white" />
                    </div>
                    <div>
                      <p className="text-white font-medium">{room.name}</p>
                      <p className="text-sm text-gray-400">@{room.alias}</p>
                    </div>
                  </div>
                  {getSourceBadge(room.source)}
                </div>

                {/* HA Link Status */}
                <div className="flex items-center justify-between text-sm mb-2">
                  <span className="text-gray-400">Home Assistant:</span>
                  {room.ha_area_id ? (
                    <span className="text-green-400 flex items-center space-x-1">
                      <LinkIcon className="w-3 h-3" />
                      <span>Verknuepft</span>
                    </span>
                  ) : (
                    <span className="text-gray-500">Nicht verknuepft</span>
                  )}
                </div>

                {/* Devices Summary */}
                <div className="flex items-center justify-between text-sm mb-2">
                  <span className="text-gray-400">Geraete:</span>
                  <span className="text-gray-300">
                    {room.device_count || 0}
                    {room.online_count > 0 && (
                      <span className="text-green-400 ml-1">
                        ({room.online_count} online)
                      </span>
                    )}
                  </span>
                </div>

                {/* Device List */}
                {room.devices?.length > 0 && (
                  <div className="mb-4 p-2 bg-gray-800 rounded-lg space-y-1">
                    {room.devices.map((device) => {
                      const config = DEVICE_TYPE_CONFIG[device.device_type] || DEVICE_TYPE_CONFIG.web_browser;
                      const DeviceIcon = config.icon;
                      return (
                        <div
                          key={device.device_id}
                          className="flex items-center justify-between text-xs py-1"
                        >
                          <div className="flex items-center space-x-2 min-w-0 flex-1">
                            <DeviceIcon className={`w-3 h-3 flex-shrink-0 ${config.color}`} />
                            <span className="text-gray-400 truncate" title={device.device_id}>
                              {device.device_name || device.device_id}
                            </span>
                          </div>
                          <div className="flex items-center space-x-2 flex-shrink-0 ml-2">
                            <span className="text-gray-500 text-[10px]">{config.label}</span>
                            <span className={device.is_online ? 'text-green-400' : 'text-gray-500'}>
                              {device.is_online ? 'online' : 'offline'}
                            </span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Output Device Settings */}
                <RoomOutputSettings roomId={room.id} roomName={room.name} />

                {/* Actions */}
                <div className="flex space-x-2">
                  {room.ha_area_id ? (
                    <button
                      onClick={() => unlinkFromHA(room)}
                      className="flex-1 btn bg-yellow-600/20 hover:bg-yellow-600/40 text-yellow-400 text-sm flex items-center justify-center space-x-1"
                    >
                      <Unlink className="w-4 h-4" />
                      <span>Trennen</span>
                    </button>
                  ) : (
                    <button
                      onClick={() => openLinkModal(room)}
                      className="flex-1 btn bg-blue-600/20 hover:bg-blue-600/40 text-blue-400 text-sm flex items-center justify-center space-x-1"
                    >
                      <LinkIcon className="w-4 h-4" />
                      <span>Verknuepfen</span>
                    </button>
                  )}
                  <button
                    onClick={() => openEditModal(room)}
                    className="p-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300"
                    aria-label={`${room.name} bearbeiten`}
                  >
                    <Edit3 className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    onClick={() => deleteRoom(room)}
                    className="p-2 rounded-lg bg-red-600/20 hover:bg-red-600/40 text-red-400"
                    aria-label={`${room.name} löschen`}
                  >
                    <Trash2 className="w-4 h-4" aria-hidden="true" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Create Room Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-white mb-4">Neuen Raum anlegen</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-400 mb-1">Name</label>
                <input
                  type="text"
                  value={newRoomName}
                  onChange={(e) => setNewRoomName(e.target.value)}
                  placeholder="Wohnzimmer"
                  className="input w-full"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-1">Icon (optional)</label>
                <input
                  type="text"
                  value={newRoomIcon}
                  onChange={(e) => setNewRoomIcon(e.target.value)}
                  placeholder="mdi:sofa"
                  className="input w-full"
                />
                <p className="text-xs text-gray-500 mt-1">Material Design Icon (z.B. mdi:sofa)</p>
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
                onClick={createRoom}
                className="flex-1 btn btn-primary"
              >
                Erstellen
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Edit Room Modal */}
      {showEditModal && selectedRoom && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-white mb-4">Raum bearbeiten</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-400 mb-1">Name</label>
                <input
                  type="text"
                  value={editRoomName}
                  onChange={(e) => setEditRoomName(e.target.value)}
                  placeholder="Wohnzimmer"
                  className="input w-full"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-1">Icon (optional)</label>
                <input
                  type="text"
                  value={editRoomIcon}
                  onChange={(e) => setEditRoomIcon(e.target.value)}
                  placeholder="mdi:sofa"
                  className="input w-full"
                />
              </div>

              <div className="text-sm text-gray-500">
                Alias: @{selectedRoom.alias}
              </div>
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowEditModal(false)}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                Abbrechen
              </button>
              <button
                onClick={updateRoom}
                disabled={updating}
                className="flex-1 btn btn-primary disabled:opacity-50"
              >
                {updating ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  'Speichern'
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Link to HA Area Modal */}
      {showLinkModal && selectedRoom && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-white mb-2">Mit HA Area verknuepfen</h2>
            <p className="text-gray-400 mb-4">Raum: {selectedRoom.name}</p>

            <div className="space-y-4">
              {loadingAreas ? (
                <div className="text-center py-4">
                  <Loader className="w-6 h-6 animate-spin mx-auto text-gray-400" />
                </div>
              ) : haAreas.length === 0 ? (
                <p className="text-gray-400 text-center py-4">
                  Keine Home Assistant Areas gefunden
                </p>
              ) : (
                <div>
                  <label className="block text-sm text-gray-400 mb-2">HA Area auswaehlen:</label>
                  <select
                    value={selectedHAArea}
                    onChange={(e) => setSelectedHAArea(e.target.value)}
                    className="input w-full"
                  >
                    <option value="">-- Area auswaehlen --</option>
                    {haAreas
                      .filter(a => !a.is_linked)
                      .map(area => (
                        <option key={area.area_id} value={area.area_id}>
                          {area.name}
                        </option>
                      ))
                    }
                  </select>
                  {haAreas.filter(a => !a.is_linked).length === 0 && (
                    <p className="text-yellow-400 text-sm mt-2">
                      Alle HA Areas sind bereits verknuepft
                    </p>
                  )}
                </div>
              )}
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowLinkModal(false)}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                Abbrechen
              </button>
              <button
                onClick={linkToHAArea}
                disabled={!selectedHAArea || updating}
                className="flex-1 btn btn-primary disabled:opacity-50"
              >
                {updating ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  'Verknuepfen'
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* HA Sync Panel Modal */}
      {showSyncPanel && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-lg w-full max-h-[90vh] overflow-y-auto">
            <h2 className="text-xl font-bold text-white mb-4">Home Assistant Synchronisation</h2>

            {/* Conflict Resolution */}
            <div className="mb-6">
              <label className="block text-sm text-gray-400 mb-2">Konfliktloesung:</label>
              <select
                value={conflictResolution}
                onChange={(e) => setConflictResolution(e.target.value)}
                className="input w-full"
              >
                <option value="skip">Ueberspringen (bei Namenskollision)</option>
                <option value="link">Verknuepfen (vorhandenen Raum mit HA verbinden)</option>
                <option value="overwrite">Ueberschreiben (HA-Verknuepfung ersetzen)</option>
              </select>
            </div>

            {/* Sync Actions */}
            <div className="grid grid-cols-3 gap-3 mb-6">
              <button
                onClick={importFromHA}
                disabled={syncing}
                className="btn bg-green-600 hover:bg-green-700 text-white flex flex-col items-center py-4"
              >
                <ArrowDownToLine className="w-6 h-6 mb-2" />
                <span className="text-sm">Import</span>
              </button>
              <button
                onClick={exportToHA}
                disabled={syncing}
                className="btn bg-blue-600 hover:bg-blue-700 text-white flex flex-col items-center py-4"
              >
                <ArrowUpFromLine className="w-6 h-6 mb-2" />
                <span className="text-sm">Export</span>
              </button>
              <button
                onClick={syncWithHA}
                disabled={syncing}
                className="btn bg-purple-600 hover:bg-purple-700 text-white flex flex-col items-center py-4"
              >
                {syncing ? (
                  <Loader className="w-6 h-6 mb-2 animate-spin" />
                ) : (
                  <ArrowLeftRight className="w-6 h-6 mb-2" />
                )}
                <span className="text-sm">Sync</span>
              </button>
            </div>

            {/* HA Areas List */}
            <div>
              <h3 className="text-lg font-semibold text-white mb-3">
                Home Assistant Areas ({haAreas.length})
              </h3>
              {loadingAreas ? (
                <div className="text-center py-4">
                  <Loader className="w-6 h-6 animate-spin mx-auto text-gray-400" />
                </div>
              ) : haAreas.length === 0 ? (
                <p className="text-gray-400 text-center py-4">
                  Keine HA Areas gefunden. Ist Home Assistant verbunden?
                </p>
              ) : (
                <div className="space-y-2 max-h-60 overflow-y-auto">
                  {haAreas.map(area => (
                    <div
                      key={area.area_id}
                      className="flex items-center justify-between p-3 bg-gray-800 rounded-lg"
                    >
                      <div>
                        <p className="text-white">{area.name}</p>
                        <p className="text-xs text-gray-500">{area.area_id}</p>
                      </div>
                      {area.is_linked ? (
                        <span className="text-green-400 text-sm flex items-center space-x-1">
                          <LinkIcon className="w-3 h-3" />
                          <span>{area.linked_room_name}</span>
                        </span>
                      ) : (
                        <span className="text-gray-500 text-sm">Nicht verknuepft</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowSyncPanel(false)}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                Schließen
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
