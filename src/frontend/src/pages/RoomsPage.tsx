import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Home, Plus, Edit3, Trash2, Loader,
  RefreshCw, Link as LinkIcon, Unlink, Radio,
  ArrowDownToLine, ArrowUpFromLine, ArrowLeftRight,
  Monitor, Tablet, Smartphone, Tv, User,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import apiClient from '../utils/axios';
import { extractApiError } from '../utils/axios';
import RoomOutputSettings from '../components/RoomOutputSettings';
import { useConfirmDialog } from '../components/ConfirmDialog';
import Modal from '../components/Modal';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';

type DeviceTypeKey = 'satellite' | 'web_panel' | 'web_tablet' | 'web_browser' | 'web_kiosk';

interface DeviceTypeConfig {
  icon: LucideIcon;
  label: string;
  color: string;
}

interface RoomDevice {
  device_id: string;
  device_name?: string | null;
  device_type: DeviceTypeKey;
  is_online: boolean;
}

interface Room {
  id: number;
  name: string;
  alias: string;
  icon?: string | null;
  source?: 'homeassistant' | 'satellite' | 'renfield' | string;
  ha_area_id?: string | null;
  owner_id?: number | null;
  owner_name?: string | null;
  device_count?: number;
  online_count?: number;
  devices?: RoomDevice[];
}

interface HAArea {
  area_id: string;
  name: string;
  is_linked?: boolean;
  linked_room_name?: string;
}

interface SimpleUser {
  id: number;
  username: string;
  first_name?: string;
}

type ConflictResolution = 'skip' | 'link' | 'overwrite';

// Device type icons and labels
const DEVICE_TYPE_CONFIG: Record<DeviceTypeKey, DeviceTypeConfig> = {
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
  const [rooms, setRooms] = useState<Room[]>([]);
  const [haAreas, setHAAreas] = useState<HAArea[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingAreas, setLoadingAreas] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showLinkModal, setShowLinkModal] = useState(false);
  const [showSyncPanel, setShowSyncPanel] = useState(false);
  const [selectedRoom, setSelectedRoom] = useState<Room | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Form state
  const [newRoomName, setNewRoomName] = useState('');
  const [newRoomIcon, setNewRoomIcon] = useState('');
  const [editRoomName, setEditRoomName] = useState('');
  const [editRoomIcon, setEditRoomIcon] = useState('');
  const [editRoomOwnerId, setEditRoomOwnerId] = useState('');
  const [users, setUsers] = useState<SimpleUser[]>([]);
  const [updating, setUpdating] = useState(false);
  const [selectedHAArea, setSelectedHAArea] = useState('');
  const [conflictResolution, setConflictResolution] = useState<ConflictResolution>('link');

  const loadRooms = useCallback(async () => {
    try {
      setLoading(true);
      const response = await apiClient.get<Room[]>('/api/rooms');
      setRooms(response.data);
    } catch (err) {
      console.error('Failed to load rooms:', err);
      setError(t('rooms.couldNotLoad'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  // Load data on mount
  useEffect(() => {
    loadRooms();
  }, [loadRooms]);

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

  const loadUsers = async () => {
    try {
      const response = await apiClient.get<SimpleUser[] | { users?: SimpleUser[] }>('/api/users');
      const data = response.data;
      setUsers(Array.isArray(data) ? data : (data?.users ?? []));
    } catch (err) {
      console.error('Failed to load users:', err);
    }
  };

  const loadHAAreas = async () => {
    try {
      setLoadingAreas(true);
      const response = await apiClient.get<HAArea[]>('/api/rooms/ha/areas');
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
      setError(extractApiError(err, t('common.error')));
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

      // Update owner separately via dedicated endpoint
      const newOwnerId = editRoomOwnerId === '' ? null : parseInt(editRoomOwnerId, 10);
      if (newOwnerId !== (selectedRoom.owner_id || null)) {
        await apiClient.patch(`/api/rooms/${selectedRoom.id}/owner`, null, {
          params: { owner_id: newOwnerId }
        });
      }

      setSuccess(t('rooms.roomUpdated', { name: editRoomName }));
      setShowEditModal(false);
      loadRooms();
    } catch (err) {
      console.error('Failed to update room:', err);
      setError(extractApiError(err, t('common.error')));
    } finally {
      setUpdating(false);
    }
  };

  const deleteRoom = async (room: Room) => {
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
      setError(extractApiError(err, t('rooms.linkFailed')));
    } finally {
      setUpdating(false);
    }
  };

  const unlinkFromHA = async (room: Room) => {
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
      const response = await apiClient.post<{ imported: number; linked: number; skipped: number }>('/api/rooms/ha/import', {
        conflict_resolution: conflictResolution,
      });

      const { imported, linked, skipped } = response.data;
      setSuccess(t('rooms.importResult', { imported, linked, skipped }));
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to import from HA:', err);
      setError(extractApiError(err, t('rooms.importFailed')));
    } finally {
      setSyncing(false);
    }
  };

  const exportToHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post<{ exported: number; linked: number }>('/api/rooms/ha/export');

      const { exported, linked } = response.data;
      setSuccess(t('rooms.exportResult', { exported, linked }));
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to export to HA:', err);
      setError(extractApiError(err, t('rooms.exportFailed')));
    } finally {
      setSyncing(false);
    }
  };

  const syncWithHA = async () => {
    try {
      setSyncing(true);
      const response = await apiClient.post<{
        import_results: { imported: number; linked: number };
        export_results: { exported: number; linked: number };
      }>(`/api/rooms/ha/sync?conflict_resolution=${conflictResolution}`);

      const { import_results, export_results } = response.data;
      const imported = import_results.imported + import_results.linked;
      const exported = export_results.exported + export_results.linked;
      setSuccess(t('rooms.syncResult', { imported, exported }));
      loadRooms();
      loadHAAreas();
    } catch (err) {
      console.error('Failed to sync with HA:', err);
      setError(extractApiError(err, t('rooms.syncFailed')));
    } finally {
      setSyncing(false);
    }
  };

  const deleteDevice = async (device: RoomDevice) => {
    const confirmed = await confirm({
      title: t('rooms.deleteDevice'),
      message: t('rooms.deleteDeviceConfirm', { name: device.device_name || device.device_id }),
      confirmLabel: t('common.delete'),
      cancelLabel: t('common.cancel'),
      variant: 'danger',
    });

    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/rooms/devices/${device.device_id}`);
      loadRooms();
    } catch (err) {
      console.error('Failed to delete device:', err);
      setError(t('common.error'));
    }
  };

  const openEditModal = (room: Room) => {
    setSelectedRoom(room);
    setEditRoomName(room.name);
    setEditRoomIcon(room.icon || '');
    setEditRoomOwnerId(room.owner_id != null ? String(room.owner_id) : '');
    loadUsers();
    setShowEditModal(true);
  };

  const openLinkModal = (room: Room) => {
    setSelectedRoom(room);
    setSelectedHAArea('');
    loadHAAreas();
    setShowLinkModal(true);
  };

  const openSyncPanel = () => {
    loadHAAreas();
    setShowSyncPanel(true);
  };

  const getSourceBadge = (source: Room['source']) => {
    switch (source) {
      case 'homeassistant':
        return <Badge color="blue">HA</Badge>;
      case 'satellite':
        return <Badge color="green">Satellite</Badge>;
      default:
        return <Badge color="gray">Renfield</Badge>;
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <PageHeader icon={Home} title={t('rooms.title')} subtitle={t('rooms.subtitle')}>
        <button onClick={loadRooms} className="btn-icon btn-icon-ghost" aria-label={t('rooms.refreshRooms')}>
          <RefreshCw className="w-5 h-5" aria-hidden="true" />
        </button>
      </PageHeader>

      {/* Alerts */}
      {error && <Alert variant="error">{error}</Alert>}

      {success && <Alert variant="success">{success}</Alert>}

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
          className="btn btn-secondary flex items-center space-x-2"
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

                {/* Owner */}
                {room.owner_name && (
                  <div className="flex items-center justify-between text-sm mb-2">
                    <span className="text-gray-500 dark:text-gray-400">{t('rooms.owner')}:</span>
                    <span className="text-gray-600 dark:text-gray-300 flex items-center space-x-1">
                      <User className="w-3 h-3" />
                      <span>{room.owner_name}</span>
                    </span>
                  </div>
                )}

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
                            <DeviceIcon className={`w-3 h-3 shrink-0 ${config.color}`} />
                            <span className="text-gray-500 dark:text-gray-400 truncate" title={device.device_id}>
                              {device.device_name || device.device_id}
                            </span>
                          </div>
                          <div className="flex items-center space-x-2 shrink-0 ml-2">
                            <span className="text-gray-500 text-[10px]">{config.label}</span>
                            <span className={device.is_online ? 'text-green-600 dark:text-green-400' : 'text-gray-500'}>
                              {device.is_online ? t('common.online') : t('common.offline')}
                            </span>
                            {!device.is_online && (
                              <button
                                onClick={() => deleteDevice(device)}
                                className="p-0.5 rounded hover:bg-red-100 dark:hover:bg-red-600/20 text-red-400 hover:text-red-600 dark:hover:text-red-400"
                                aria-label={t('rooms.deleteDevice')}
                              >
                                <Trash2 className="w-3 h-3" aria-hidden="true" />
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Output Device Settings */}
                <RoomOutputSettings roomId={room.id} roomName={room.name} />
                <RoomOutputSettings roomId={room.id} roomName={room.name} outputType="visual" />

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
                    className="btn-icon btn-icon-ghost"
                    aria-label={`${room.name} ${t('common.edit').toLowerCase()}`}
                  >
                    <Edit3 className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    onClick={() => deleteRoom(room)}
                    className="btn-icon btn-icon-danger"
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
      <Modal isOpen={showCreateModal} onClose={() => setShowCreateModal(false)} title={t('rooms.createRoom')}>
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
      </Modal>

      {/* Edit Room Modal */}
      <Modal isOpen={showEditModal && !!selectedRoom} onClose={() => setShowEditModal(false)} title={t('rooms.editRoom')}>
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

          <div>
            <label className="block text-sm text-gray-500 dark:text-gray-400 mb-1">{t('rooms.owner')}</label>
            <select
              value={editRoomOwnerId}
              onChange={(e) => setEditRoomOwnerId(e.target.value)}
              className="input w-full"
            >
              <option value="">{t('rooms.noOwner')}</option>
              {users.map(u => (
                <option key={u.id} value={u.id}>
                  {u.first_name || u.username}
                </option>
              ))}
            </select>
            <p className="text-xs text-gray-500 mt-1">{t('rooms.ownerHint')}</p>
          </div>

          {selectedRoom && (
            <div className="text-sm text-gray-500">
              {t('rooms.alias')}: @{selectedRoom.alias}
            </div>
          )}
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
      </Modal>

      {/* Link to HA Area Modal */}
      <Modal isOpen={showLinkModal && !!selectedRoom} onClose={() => setShowLinkModal(false)} title={t('rooms.linkToHAArea')}>
        {selectedRoom && (
          <p className="text-gray-500 dark:text-gray-400 mb-4">{t('device.room')}: {selectedRoom.name}</p>
        )}

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
      </Modal>

      {/* HA Sync Panel Modal */}
      <Modal isOpen={showSyncPanel} onClose={() => setShowSyncPanel(false)} title={t('rooms.haSyncTitle')} maxWidth="max-w-lg">
        {/* Conflict Resolution */}
        <div className="mb-6">
          <label className="block text-sm text-gray-500 dark:text-gray-400 mb-2">{t('rooms.conflictResolution')}:</label>
          <select
            value={conflictResolution}
            onChange={(e) => setConflictResolution(e.target.value as ConflictResolution)}
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
          <h3 className="text-base font-semibold text-gray-900 dark:text-white mb-3">
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
      </Modal>

      {/* Confirm Dialog */}
      {ConfirmDialogComponent}
    </div>
  );
}
