import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
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
  const { t } = useTranslation();
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
      setError(t('rooms.couldNotLoad'));
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
      setError(t('rooms.couldNotLoadAreas'));
    } finally {
      setLoadingAreas(false);
    }
  };

  const createRoom = async () => {
    if (!newRoomName.trim()) {
      setError(t('rooms.nameRequired'));
      return;
    }

    try {
      await apiClient.post('/api/rooms', {
        name: newRoomName,
        icon: newRoomIcon || null
      });

      setSuccess(t('rooms.roomCreated', { name: newRoomName }));
      setShowCreateModal(false);
      setNewRoomName('');
      setNewRoomIcon('');
      loadRooms();
    } catch (err) {
      console.error('Failed to create room:', err);
      setError(err.response?.data?.detail || t('common.error'));
    }
  };

  const updateRoom = async () => {
    if (!selectedRoom || !editRoomName.trim()) {
      setError(t('rooms.nameRequired'));
      return;
    }

    try {
      setUpdating(true);
      await apiClient.patch(`/api/rooms/${selectedRoom.id}`, {
        name: editRoomName,
        icon: editRoomIcon || null
      });

      setSuccess(t('rooms.roomUpdated', { name: editRoomName }));
      setShowEditModal(false);
      loadRooms();
    } catch (err) {
      console.error('Failed to update room:', err);
      setError(err.response?.data?.detail || t('common.error'));
    } finally {
      setUpdating(false);
    }
  };

  const deleteRoom = async (room) => {
    const confirmed = await confirm({
      title: t('rooms.deleteRoom'),
      message: t('rooms.deleteRoomConfirm', { name: room.name }),
      confirmLabel: t('common.delete'),
      cancelLabel: t('common.cancel'),
      variant: 'danger',
    });

    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/rooms/${room.id}`);
      setSuccess(t('rooms.roomDeleted', { name: room.name }));
      loadRooms();
    } catch (err) {
      console.error('Failed to delete room:', err);
      setError(t('common.error'));
    }
  };

  const linkToHAArea = async () => {
    if (!selectedRoom || !selectedHAArea) {
      setError(t('rooms.pleaseSelectArea'));
      return;
    }

    try {
      setUpdating(true);
      await apiClient.post(`/api/rooms/${selectedRoom.id}/link/${selectedHAArea}`);
      setSuccess(t('rooms.linkedWith'));
      setShowLinkModal(false);
      setSelectedHAArea('');
      loadRooms();
    } catch (err) {
      console.error('Failed to link room:', err);
      setError(err.response?.data?.detail || t('rooms.linkFailed'));
    } finally {
      setUpdating(false);
    }
  };

  const unlinkFromHA = async (room) => {
    const confirmed = await confirm({
      title: t('rooms.unlinkTitle'),
      message: t('rooms.unlinkConfirm', { name: room.name }),
      confirmLabel: t('rooms.unlinkFromHA'),
      cancelLabel: t('common.cancel'),
      variant: 'warning',
    });

    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/rooms/${room.id}/link`);
      setSuccess(t('rooms.linkUnlinked'));
      loadRooms();
    } catch (err) {
      console.error('Failed to unlink room:', err);
      setError(t('rooms.unlinkFailed'));
    }
  };

  const importFromHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post('/api/rooms/ha/import', {
        conflict_resolution: conflictResolution
      });

      const { imported, linked, skipped } = response.data;
      setSuccess(t('rooms.importResult', { imported, linked, skipped }));
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to import from HA:', err);
      setError(err.response?.data?.detail || t('rooms.importFailed'));
    } finally {
      setSyncing(false);
    }
  };

  const exportToHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post('/api/rooms/ha/export');

      const { exported, linked } = response.data;
      setSuccess(t('rooms.exportResult', { exported, linked }));
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to export to HA:', err);
      setError(err.response?.data?.detail || t('rooms.exportFailed'));
    } finally {
      setSyncing(false);
    }
  };

  const syncWithHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post(`/api/rooms/ha/sync?conflict_resolution=${conflictResolution}`);

      const { import_results, export_results } = response.data;
      const imported = import_results.imported + import_results.linked;
      const exported = export_results.exported + export_results.linked;
      setSuccess(t('rooms.syncResult', { imported, exported }));
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to sync with HA:', err);
      setError(err.response?.data?.detail || t('rooms.syncFailed'));
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
        return <span className="px-2 py-1 bg-blue-100 text-blue-600 dark:bg-blue-600/20 dark:text-blue-400 text-xs rounded">HA</span>;
      case 'satellite':
        return <span className="px-2 py-1 bg-green-100 text-green-600 dark:bg-green-600/20 dark:text-green-400 text-xs rounded">Satellite</span>;
      default:
        return <span className="px-2 py-1 bg-gray-200 text-gray-600 dark:bg-gray-600/20 dark:text-gray-400 text-xs rounded">Renfield</span>;
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('rooms.title')}</h1>
            <p className="text-gray-500 dark:text-gray-400">{t('rooms.subtitle')}</p>
          </div>
          <div className="flex items-center space-x-2">
            <button
              onClick={loadRooms}
              className="p-2 rounded-lg bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300"
              aria-label={t('rooms.refreshRooms')}
            >
              <RefreshCw className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>

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
        >
          <Plus className="w-4 h-4" />
          <span>{t('rooms.newRoom')}</span>
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
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">
          {t('rooms.roomsCount', { count: rooms.length })}
        </h2>

        {loading ? (
          <div className="card text-center py-12" role="status" aria-label={t('rooms.loadingRooms')}>
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" aria-hidden="true" />
            <p className="text-gray-500 dark:text-gray-400">{t('rooms.loadingRooms')}</p>
          </div>
        ) : rooms.length === 0 ? (
          <div className="card text-center py-12">
            <Home className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-4" />
            <p className="text-gray-500 dark:text-gray-400 mb-4">{t('rooms.noRooms')}</p>
            <button
              onClick={() => setShowCreateModal(true)}
              className="btn btn-primary"
            >
              {t('rooms.createFirstRoom')}
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
                      <p className="text-gray-900 dark:text-white font-medium">{room.name}</p>
                      <p className="text-sm text-gray-500 dark:text-gray-400">@{room.alias}</p>
                    </div>
                  </div>
                  {getSourceBadge(room.source)}
                </div>

                {/* HA Link Status */}
                <div className="flex items-center justify-between text-sm mb-2">
                  <span className="text-gray-500 dark:text-gray-400">Home Assistant:</span>
                  {room.ha_area_id ? (
                    <span className="text-green-600 dark:text-green-400 flex items-center space-x-1">
                      <LinkIcon className="w-3 h-3" />
                      <span>{t('rooms.haLinked')}</span>
                    </span>
                  ) : (
                    <span className="text-gray-500">{t('rooms.haNotLinked')}</span>
                  )}
                </div>

                {/* Devices Summary */}
                <div className="flex items-center justify-between text-sm mb-2">
                  <span className="text-gray-500 dark:text-gray-400">{t('rooms.devices')}:</span>
                  <span className="text-gray-600 dark:text-gray-300">
                    {room.device_count || 0}
                    {room.online_count > 0 && (
                      <span className="text-green-600 dark:text-green-400 ml-1">
                        ({room.online_count} {t('common.online')})
                      </span>
                    )}
                  </span>
                </div>

                {/* Device List */}
                {room.devices?.length > 0 && (
                  <div className="mb-4 p-2 bg-gray-100 dark:bg-gray-800 rounded-lg space-y-1">
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
                            <span className="text-gray-500 dark:text-gray-400 truncate" title={device.device_id}>
                              {device.device_name || device.device_id}
                            </span>
                          </div>
                          <div className="flex items-center space-x-2 flex-shrink-0 ml-2">
                            <span className="text-gray-500 text-[10px]">{config.label}</span>
                            <span className={device.is_online ? 'text-green-600 dark:text-green-400' : 'text-gray-500'}>
                              {device.is_online ? t('common.online') : t('common.offline')}
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
                      className="flex-1 btn bg-yellow-100 hover:bg-yellow-200 text-yellow-700 dark:bg-yellow-600/20 dark:hover:bg-yellow-600/40 dark:text-yellow-400 text-sm flex items-center justify-center space-x-1"
                    >
                      <Unlink className="w-4 h-4" />
                      <span>{t('rooms.unlinkFromHA')}</span>
                    </button>
                  ) : (
                    <button
                      onClick={() => openLinkModal(room)}
                      className="flex-1 btn bg-blue-100 hover:bg-blue-200 text-blue-600 dark:bg-blue-600/20 dark:hover:bg-blue-600/40 dark:text-blue-400 text-sm flex items-center justify-center space-x-1"
                    >
                      <LinkIcon className="w-4 h-4" />
                      <span>{t('rooms.linkToHA')}</span>
                    </button>
                  )}
                  <button
                    onClick={() => openEditModal(room)}
                    className="p-2 rounded-lg bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300"
                    aria-label={`${room.name} ${t('common.edit').toLowerCase()}`}
                  >
                    <Edit3 className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    onClick={() => deleteRoom(room)}
                    className="p-2 rounded-lg bg-red-100 hover:bg-red-200 text-red-600 dark:bg-red-600/20 dark:hover:bg-red-600/40 dark:text-red-400"
                    aria-label={`${room.name} ${t('common.delete').toLowerCase()}`}
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
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">{t('rooms.createRoom')}</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('common.name')}</label>
                <input
                  type="text"
                  value={newRoomName}
                  onChange={(e) => setNewRoomName(e.target.value)}
                  placeholder="Wohnzimmer"
                  className="input w-full"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('rooms.icon')}</label>
                <input
                  type="text"
                  value={newRoomIcon}
                  onChange={(e) => setNewRoomIcon(e.target.value)}
                  placeholder="mdi:sofa"
                  className="input w-full"
                />
                <p className="text-xs text-gray-500 mt-1">{t('rooms.iconHint')}</p>
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
                onClick={createRoom}
                className="flex-1 btn btn-primary"
              >
                {t('common.create')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Edit Room Modal */}
      {showEditModal && selectedRoom && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">{t('rooms.editRoom')}</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('common.name')}</label>
                <input
                  type="text"
                  value={editRoomName}
                  onChange={(e) => setEditRoomName(e.target.value)}
                  placeholder="Wohnzimmer"
                  className="input w-full"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('rooms.icon')}</label>
                <input
                  type="text"
                  value={editRoomIcon}
                  onChange={(e) => setEditRoomIcon(e.target.value)}
                  placeholder="mdi:sofa"
                  className="input w-full"
                />
              </div>

              <div className="text-sm text-gray-500">
                {t('rooms.alias')}: @{selectedRoom.alias}
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
                onClick={updateRoom}
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

      {/* Link to HA Area Modal */}
      {showLinkModal && selectedRoom && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-2">{t('rooms.linkToHAArea')}</h2>
            <p className="text-gray-500 dark:text-gray-400 mb-4">{t('device.room')}: {selectedRoom.name}</p>

            <div className="space-y-4">
              {loadingAreas ? (
                <div className="text-center py-4">
                  <Loader className="w-6 h-6 animate-spin mx-auto text-gray-500 dark:text-gray-400" />
                </div>
              ) : haAreas.length === 0 ? (
                <p className="text-gray-500 dark:text-gray-400 text-center py-4">
                  {t('rooms.noHAAreas')}
                </p>
              ) : (
                <div>
                  <label className="block text-sm text-gray-500 dark:text-gray-400 mb-2">{t('rooms.selectArea')}:</label>
                  <select
                    value={selectedHAArea}
                    onChange={(e) => setSelectedHAArea(e.target.value)}
                    className="input w-full"
                  >
                    <option value="">{t('rooms.selectAreaPlaceholder')}</option>
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
                    <p className="text-yellow-600 dark:text-yellow-400 text-sm mt-2">
                      {t('rooms.allAreasLinked')}
                    </p>
                  )}
                </div>
              )}
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowLinkModal(false)}
                className="flex-1 btn btn-secondary"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={linkToHAArea}
                disabled={!selectedHAArea || updating}
                className="flex-1 btn btn-primary disabled:opacity-50"
              >
                {updating ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  t('rooms.linkToHA')
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
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">{t('rooms.haSyncTitle')}</h2>

            {/* Conflict Resolution */}
            <div className="mb-6">
              <label className="block text-sm text-gray-500 dark:text-gray-400 mb-2">{t('rooms.conflictResolution')}:</label>
              <select
                value={conflictResolution}
                onChange={(e) => setConflictResolution(e.target.value)}
                className="input w-full"
              >
                <option value="skip">{t('rooms.conflictSkip')}</option>
                <option value="link">{t('rooms.conflictLink')}</option>
                <option value="overwrite">{t('rooms.conflictOverwrite')}</option>
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
                <span className="text-sm">{t('rooms.import')}</span>
              </button>
              <button
                onClick={exportToHA}
                disabled={syncing}
                className="btn bg-blue-600 hover:bg-blue-700 text-white flex flex-col items-center py-4"
              >
                <ArrowUpFromLine className="w-6 h-6 mb-2" />
                <span className="text-sm">{t('rooms.export')}</span>
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
                <span className="text-sm">{t('rooms.sync')}</span>
              </button>
            </div>

            {/* HA Areas List */}
            <div>
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-3">
                {t('rooms.haAreasCount', { count: haAreas.length })}
              </h3>
              {loadingAreas ? (
                <div className="text-center py-4">
                  <Loader className="w-6 h-6 animate-spin mx-auto text-gray-500 dark:text-gray-400" />
                </div>
              ) : haAreas.length === 0 ? (
                <p className="text-gray-500 dark:text-gray-400 text-center py-4">
                  {t('rooms.haNotConnected')}
                </p>
              ) : (
                <div className="space-y-2 max-h-60 overflow-y-auto">
                  {haAreas.map(area => (
                    <div
                      key={area.area_id}
                      className="flex items-center justify-between p-3 bg-gray-100 dark:bg-gray-800 rounded-lg"
                    >
                      <div>
                        <p className="text-gray-900 dark:text-white">{area.name}</p>
                        <p className="text-xs text-gray-500">{area.area_id}</p>
                      </div>
                      {area.is_linked ? (
                        <span className="text-green-600 dark:text-green-400 text-sm flex items-center space-x-1">
                          <LinkIcon className="w-3 h-3" />
                          <span>{area.linked_room_name}</span>
                        </span>
                      ) : (
                        <span className="text-gray-500 text-sm">{t('rooms.haNotLinked')}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowSyncPanel(false)}
                className="flex-1 btn btn-secondary"
              >
                {t('common.close')}
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
