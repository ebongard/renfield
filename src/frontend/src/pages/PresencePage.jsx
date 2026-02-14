/**
 * Presence Detection Page
 *
 * Admin page for monitoring room occupancy and managing BLE devices.
 * Shows real-time presence data with confidence indicators and
 * allows CRUD operations on tracked BLE devices.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import { useConfirmDialog } from '../components/ConfirmDialog';
import {
  MapPin, Users, Wifi, Smartphone, Plus, Trash2, RefreshCw,
  AlertCircle, Clock, Watch, Radio,
} from 'lucide-react';


// Confidence bar component
function ConfidenceBar({ value }) {
  const pct = Math.round(value * 100);
  const getColor = () => {
    if (pct > 70) return 'bg-green-500';
    if (pct > 40) return 'bg-yellow-500';
    return 'bg-red-500';
  };

  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div
          className={`h-full ${getColor()} transition-all duration-300`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs text-gray-500 dark:text-gray-400 w-10 text-right">{pct}%</span>
    </div>
  );
}


// Format relative time
function useFormatAgo(t) {
  return useCallback((timestamp) => {
    if (!timestamp) return '';
    const seconds = Math.floor(Date.now() / 1000 - timestamp);
    if (seconds < 5) return t('presence.justNow');
    if (seconds < 60) return t('presence.secondsAgo', { count: seconds });
    return t('presence.minutesAgo', { count: Math.floor(seconds / 60) });
  }, [t]);
}


// Room occupancy card
function RoomCard({ room, formatAgo }) {
  const { t } = useTranslation();

  return (
    <div className="card">
      <div className="flex items-center gap-3 mb-3">
        <div className="p-2 bg-blue-100 dark:bg-blue-900/30 rounded-lg">
          <MapPin className="w-5 h-5 text-blue-600 dark:text-blue-400" />
        </div>
        <div className="flex-1">
          <h3 className="font-medium text-gray-900 dark:text-white">
            {room.room_name || `Room ${room.room_id}`}
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {room.occupants.length} {room.occupants.length === 1 ? t('presence.trackedUsers').toLowerCase().replace(/\d+\s*/, '') : t('presence.trackedUsers').toLowerCase().replace(/\d+\s*/, '')}
          </p>
        </div>
        <span className="text-lg font-bold text-gray-900 dark:text-white">
          {room.occupants.length}
        </span>
      </div>

      <div className="space-y-3">
        {room.occupants.map((occupant) => (
          <div key={occupant.user_id} className="flex items-center gap-3 p-2 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div className="w-8 h-8 rounded-full bg-primary-600/20 flex items-center justify-center">
              <Users className="w-4 h-4 text-primary-600 dark:text-primary-400" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-gray-900 dark:text-white truncate">
                {occupant.user_name || `User ${occupant.user_id}`}
              </p>
              <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                <Clock className="w-3 h-3" />
                {formatAgo(occupant.last_seen)}
              </div>
            </div>
            <div className="w-24">
              <ConfidenceBar value={occupant.confidence} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}


// Device type icon helper
function DeviceTypeIcon({ type, className = 'w-4 h-4' }) {
  switch (type) {
    case 'watch': return <Watch className={className} />;
    case 'tracker': return <Radio className={className} />;
    default: return <Smartphone className={className} />;
  }
}


// Add device modal
function AddDeviceModal({ isOpen, onClose, onSave, users }) {
  const { t } = useTranslation();
  const [form, setForm] = useState({
    user_id: '',
    mac_address: '',
    device_name: '',
    device_type: 'phone',
  });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const macPattern = /^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    if (!form.user_id) {
      setError(t('presence.selectUser'));
      return;
    }
    if (!macPattern.test(form.mac_address)) {
      setError(t('presence.macInvalid'));
      return;
    }
    if (!form.device_name.trim()) {
      return;
    }

    setSaving(true);
    try {
      await onSave({
        user_id: parseInt(form.user_id),
        mac_address: form.mac_address.toUpperCase(),
        device_name: form.device_name.trim(),
        device_type: form.device_type,
      });
      setForm({ user_id: '', mac_address: '', device_name: '', device_type: 'phone' });
      onClose();
    } catch (err) {
      if (err.response?.status === 409) {
        setError(t('presence.macDuplicate'));
      } else {
        setError(err.response?.data?.detail || t('common.error'));
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('presence.addDevice')}>
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div className="p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-sm text-red-700 dark:text-red-400 flex items-center gap-2">
            <AlertCircle className="w-4 h-4 shrink-0" />
            {error}
          </div>
        )}

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            {t('presence.user')}
          </label>
          <select
            value={form.user_id}
            onChange={(e) => setForm({ ...form, user_id: e.target.value })}
            className="input w-full"
            required
          >
            <option value="">{t('presence.selectUser')}</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            {t('presence.macAddress')}
          </label>
          <input
            type="text"
            value={form.mac_address}
            onChange={(e) => setForm({ ...form, mac_address: e.target.value })}
            placeholder="AA:BB:CC:DD:EE:FF"
            className="input w-full font-mono"
            required
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            {t('presence.deviceName')}
          </label>
          <input
            type="text"
            value={form.device_name}
            onChange={(e) => setForm({ ...form, device_name: e.target.value })}
            placeholder="iPhone 15"
            className="input w-full"
            required
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            {t('presence.deviceType')}
          </label>
          <select
            value={form.device_type}
            onChange={(e) => setForm({ ...form, device_type: e.target.value })}
            className="input w-full"
          >
            <option value="phone">{t('presence.phone')}</option>
            <option value="watch">{t('presence.watch')}</option>
            <option value="tracker">{t('presence.tracker')}</option>
          </select>
        </div>

        <div className="flex gap-3 pt-2">
          <button type="button" onClick={onClose} className="flex-1 btn btn-secondary">
            {t('common.cancel')}
          </button>
          <button type="submit" disabled={saving} className="flex-1 btn btn-primary disabled:opacity-50">
            {saving ? t('common.loading') : t('common.create')}
          </button>
        </div>
      </form>
    </Modal>
  );
}


export default function PresencePage() {
  const { t } = useTranslation();
  const formatAgo = useFormatAgo(t);
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [rooms, setRooms] = useState([]);
  const [devices, setDevices] = useState([]);
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [showAddDevice, setShowAddDevice] = useState(false);
  const [presenceEnabled, setPresenceEnabled] = useState(null);

  const refreshIntervalRef = useRef(null);

  const loadPresence = useCallback(async () => {
    try {
      const response = await apiClient.get('/api/presence/rooms');
      setRooms(response.data || []);
      setError(null);
    } catch (err) {
      console.error('Failed to load presence:', err);
      setError(t('presence.loadError'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  const loadDevices = useCallback(async () => {
    try {
      const response = await apiClient.get('/api/presence/devices');
      setDevices(response.data || []);
    } catch {
      // Non-critical, may fail if not admin
    }
  }, []);

  const loadUsers = useCallback(async () => {
    try {
      const response = await apiClient.get('/api/users');
      const data = response.data;
      setUsers(Array.isArray(data) ? data : data?.users || []);
    } catch {
      // Non-critical
    }
  }, []);

  // Initial load
  useEffect(() => {
    loadPresence();
    loadDevices();
    loadUsers();
    apiClient.get('/api/presence/status')
      .then(res => setPresenceEnabled(res.data?.enabled ?? false))
      .catch(() => setPresenceEnabled(false));
  }, [loadPresence, loadDevices, loadUsers]);

  // Auto-refresh presence data
  useEffect(() => {
    if (autoRefresh) {
      refreshIntervalRef.current = setInterval(loadPresence, 5000);
    }
    return () => {
      if (refreshIntervalRef.current) {
        clearInterval(refreshIntervalRef.current);
      }
    };
  }, [autoRefresh, loadPresence]);

  const handleAddDevice = async (deviceData) => {
    await apiClient.post('/api/presence/devices', deviceData);
    await loadDevices();
  };

  const handleDeleteDevice = async (device) => {
    const confirmed = await confirm({
      title: t('presence.deleteDevice'),
      message: t('presence.deleteDeviceConfirm', { name: device.device_name }),
    });
    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/presence/devices/${device.id}`);
      await loadDevices();
    } catch {
      setError(t('common.error'));
    }
  };

  // Stats
  const totalUsers = new Set(rooms.flatMap(r => r.occupants.map(o => o.user_id))).size;
  const occupiedRooms = rooms.length;

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center gap-3 mb-6">
          <MapPin className="w-8 h-8 text-blue-500" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            {t('presence.title')}
          </h1>
        </div>
        <div className="flex items-center justify-center p-12">
          <RefreshCw className="w-8 h-8 animate-spin text-blue-500" />
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <MapPin className="w-8 h-8 text-blue-500" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
              {t('presence.title')}
            </h1>
            <p className="text-gray-600 dark:text-gray-400">
              {t('presence.subtitle')}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded-sm border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            {t('presence.autoRefresh')}
          </label>

          <button
            onClick={loadPresence}
            className="p-2 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            title={t('common.refresh')}
          >
            <RefreshCw className="w-5 h-5" />
          </button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <div className="card p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-green-100 dark:bg-green-900/30 rounded-lg">
              <Users className="w-5 h-5 text-green-600 dark:text-green-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">{totalUsers}</p>
              <p className="text-sm text-gray-500 dark:text-gray-400">{t('presence.trackedUsers')}</p>
            </div>
          </div>
        </div>

        <div className="card p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-blue-100 dark:bg-blue-900/30 rounded-lg">
              <MapPin className="w-5 h-5 text-blue-600 dark:text-blue-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">{occupiedRooms}</p>
              <p className="text-sm text-gray-500 dark:text-gray-400">{t('presence.occupiedRooms')}</p>
            </div>
          </div>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg flex items-center gap-2 text-red-700 dark:text-red-400">
          <AlertCircle className="w-5 h-5" />
          {error}
        </div>
      )}

      {/* Room Occupancy Section */}
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
        {t('presence.roomOccupancy')}
      </h2>

      {rooms.length === 0 ? (
        <div className="card p-12 text-center mb-8">
          <Wifi className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
            {t('presence.noData')}
          </h3>
          <p className="text-gray-500 dark:text-gray-400">
            {presenceEnabled === false
              ? t('presence.presenceDisabled')
              : t('presence.noOccupants')}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
          {rooms.map((room) => (
            <RoomCard key={room.room_id} room={room} formatAgo={formatAgo} />
          ))}
        </div>
      )}

      {/* BLE Device Management Section */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
          {t('presence.devices')}
        </h2>
        <button
          onClick={() => setShowAddDevice(true)}
          className="btn btn-primary inline-flex items-center gap-2 text-sm"
        >
          <Plus className="w-4 h-4" />
          {t('presence.addDevice')}
        </button>
      </div>

      {devices.length === 0 ? (
        <div className="card p-12 text-center">
          <Smartphone className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
            {t('presence.noDevices')}
          </h3>
          <p className="text-gray-500 dark:text-gray-400">
            {t('presence.noDevicesDesc')}
          </p>
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-3 px-4 font-medium text-gray-600 dark:text-gray-400">{t('presence.deviceName')}</th>
                  <th className="text-left py-3 px-4 font-medium text-gray-600 dark:text-gray-400">{t('presence.macAddress')}</th>
                  <th className="text-left py-3 px-4 font-medium text-gray-600 dark:text-gray-400">{t('presence.user')}</th>
                  <th className="text-left py-3 px-4 font-medium text-gray-600 dark:text-gray-400">{t('presence.deviceType')}</th>
                  <th className="text-left py-3 px-4 font-medium text-gray-600 dark:text-gray-400">{t('presence.enabled')}</th>
                  <th className="text-right py-3 px-4"></th>
                </tr>
              </thead>
              <tbody>
                {devices.map((device) => {
                  const user = users.find(u => u.id === device.user_id);
                  return (
                    <tr key={device.id} className="border-b border-gray-100 dark:border-gray-800 last:border-0">
                      <td className="py-3 px-4">
                        <div className="flex items-center gap-2">
                          <DeviceTypeIcon type={device.device_type} className="w-4 h-4 text-gray-400" />
                          <span className="text-gray-900 dark:text-white">{device.device_name}</span>
                        </div>
                      </td>
                      <td className="py-3 px-4 font-mono text-gray-600 dark:text-gray-400">{device.mac_address}</td>
                      <td className="py-3 px-4 text-gray-900 dark:text-white">{user?.username || `User ${device.user_id}`}</td>
                      <td className="py-3 px-4 text-gray-600 dark:text-gray-400">{t(`presence.${device.device_type}`)}</td>
                      <td className="py-3 px-4">
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                          device.is_enabled
                            ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400'
                            : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                        }`}>
                          {device.is_enabled ? t('presence.enabled') : t('common.offline')}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-right">
                        <button
                          onClick={() => handleDeleteDevice(device)}
                          className="p-1.5 text-gray-400 hover:text-red-600 dark:hover:text-red-400 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
                          title={t('presence.deleteDevice')}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Modals */}
      <AddDeviceModal
        isOpen={showAddDevice}
        onClose={() => setShowAddDevice(false)}
        onSave={handleAddDevice}
        users={users}
      />
      {ConfirmDialogComponent}
    </div>
  );
}
