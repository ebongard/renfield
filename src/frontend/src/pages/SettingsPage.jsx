/**
 * System Settings Page
 *
 * Admin page for managing system-wide settings like wake word configuration.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import {
  Settings, Mic, Loader, AlertCircle, CheckCircle, RefreshCw, Save,
  Satellite, Monitor, XCircle, Clock
} from 'lucide-react';

export default function SettingsPage() {
  const { t } = useTranslation();
  const { getAccessToken } = useAuth();

  // State
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [settings, setSettings] = useState(null);

  // Form state
  const [keyword, setKeyword] = useState('alexa');
  const [threshold, setThreshold] = useState(0.5);
  const [cooldownMs, setCooldownMs] = useState(2000);

  // Track if form has changes
  const [hasChanges, setHasChanges] = useState(false);

  // Device sync status
  const [syncStatus, setSyncStatus] = useState(null);
  const [showSyncStatus, setShowSyncStatus] = useState(false);
  const syncPollingRef = useRef(null);

  // Load settings
  const loadSettings = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const token = await getAccessToken();
      const response = await apiClient.get('/api/settings/wakeword', {
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      });

      setSettings(response.data);
      setKeyword(response.data.keyword);
      setThreshold(response.data.threshold);
      setCooldownMs(response.data.cooldown_ms);
      setHasChanges(false);
    } catch (err) {
      console.error('Failed to load settings:', err);
      setError(t('settings.failedToLoad'));
    } finally {
      setLoading(false);
    }
  }, [getAccessToken, t]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  // Load device sync status
  const loadSyncStatus = useCallback(async () => {
    try {
      const token = await getAccessToken();
      const response = await apiClient.get('/api/settings/wakeword/sync-status', {
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      });
      setSyncStatus(response.data);
      return response.data;
    } catch (err) {
      console.error('Failed to load sync status:', err);
      return null;
    }
  }, [getAccessToken]);

  // Start polling sync status after saving
  const startSyncStatusPolling = useCallback(async () => {
    setShowSyncStatus(true);
    await loadSyncStatus();

    // Poll every 2 seconds for up to 30 seconds
    let pollCount = 0;
    const maxPolls = 15;

    syncPollingRef.current = setInterval(async () => {
      pollCount++;
      const status = await loadSyncStatus();

      // Stop polling if all synced or max polls reached
      if (status?.all_synced || pollCount >= maxPolls) {
        clearInterval(syncPollingRef.current);
        syncPollingRef.current = null;
      }
    }, 2000);
  }, [loadSyncStatus]);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (syncPollingRef.current) {
        clearInterval(syncPollingRef.current);
      }
    };
  }, []);

  // Check for changes
  useEffect(() => {
    if (settings) {
      const changed = keyword !== settings.keyword ||
                      threshold !== settings.threshold ||
                      cooldownMs !== settings.cooldown_ms;
      setHasChanges(changed);
    }
  }, [keyword, threshold, cooldownMs, settings]);

  // Save settings
  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);

    try {
      const token = await getAccessToken();
      const response = await apiClient.put('/api/settings/wakeword', {
        keyword,
        threshold,
        cooldown_ms: cooldownMs
      }, {
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      });

      setSettings(response.data);
      setHasChanges(false);
      setSuccess(t('settings.settingsSaved'));

      // Start polling sync status to show device sync progress
      startSyncStatusPolling();

      // Clear success message after 3 seconds
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      console.error('Failed to save settings:', err);
      if (err.response?.status === 403) {
        setError(t('errors.forbidden'));
      } else {
        setError(err.response?.data?.detail || t('settings.failedToSave'));
      }
    } finally {
      setSaving(false);
    }
  };

  // Format threshold as percentage
  const thresholdPercent = Math.round(threshold * 100);

  // Format cooldown as seconds
  const cooldownSeconds = (cooldownMs / 1000).toFixed(1);

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center gap-3 mb-6">
          <Settings className="w-8 h-8 text-blue-500" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
              {t('settings.title')}
            </h1>
            <p className="text-gray-600 dark:text-gray-400">{t('settings.subtitle')}</p>
          </div>
        </div>
        <div className="flex items-center justify-center p-12">
          <Loader className="w-8 h-8 animate-spin text-blue-500" />
          <span className="ml-3 text-gray-600 dark:text-gray-400">{t('common.loading')}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-4xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Settings className="w-8 h-8 text-blue-500" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
              {t('settings.title')}
            </h1>
            <p className="text-gray-600 dark:text-gray-400">{t('settings.subtitle')}</p>
          </div>
        </div>
        <button
          onClick={loadSettings}
          className="p-2 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          title={t('common.refresh')}
        >
          <RefreshCw className="w-5 h-5" />
        </button>
      </div>

      {/* Error/Success Messages */}
      {error && (
        <div className="mb-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg flex items-center gap-2 text-red-700 dark:text-red-400">
          <AlertCircle className="w-5 h-5" />
          {error}
        </div>
      )}

      {success && (
        <div className="mb-4 p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg flex items-center gap-2 text-green-700 dark:text-green-400">
          <CheckCircle className="w-5 h-5" />
          {success}
        </div>
      )}

      {/* Wake Word Settings Card */}
      <div className="card">
        <div className="flex items-center gap-2 mb-4">
          <Mic className="w-6 h-6 text-blue-500" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('settings.wakeword.title')}
          </h2>
        </div>

        <p className="text-sm text-gray-600 dark:text-gray-400 mb-6">
          {t('settings.wakeword.description')}
        </p>

        {/* Keyword Selection */}
        <div className="mb-6">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            {t('settings.wakeword.keyword')}
          </label>
          <select
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            className="input w-full md:w-auto"
          >
            {settings?.available_keywords?.map((kw) => (
              <option key={kw.id} value={kw.id}>
                {kw.label}
              </option>
            ))}
          </select>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {settings?.available_keywords?.find(k => k.id === keyword)?.description}
          </p>
        </div>

        {/* Threshold Slider */}
        <div className="mb-6">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            {t('settings.wakeword.threshold')}
            <span className="ml-2 text-blue-500 font-semibold">{thresholdPercent}%</span>
          </label>
          <div className="flex items-center gap-4">
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {t('settings.wakeword.moreSensitive')}
            </span>
            <input
              type="range"
              min="0.1"
              max="1.0"
              step="0.05"
              value={threshold}
              onChange={(e) => setThreshold(parseFloat(e.target.value))}
              className="flex-1 h-2 bg-gray-200 dark:bg-gray-700 rounded-lg appearance-none cursor-pointer accent-blue-500"
            />
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {t('settings.wakeword.lessFalsePositives')}
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {t('settings.wakeword.thresholdHint')}
          </p>
        </div>

        {/* Cooldown Slider */}
        <div className="mb-6">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            {t('settings.wakeword.cooldown')}
            <span className="ml-2 text-blue-500 font-semibold">{cooldownSeconds}s</span>
          </label>
          <input
            type="range"
            min="500"
            max="10000"
            step="500"
            value={cooldownMs}
            onChange={(e) => setCooldownMs(parseInt(e.target.value))}
            className="w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-lg appearance-none cursor-pointer accent-blue-500"
          />
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {t('settings.wakeword.cooldownHint')}
          </p>
        </div>

        {/* Connected Devices Info */}
        {settings?.subscriber_count !== undefined && (
          <div className="mb-6 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {t('settings.wakeword.connectedDevices', { count: settings.subscriber_count })}
            </p>
          </div>
        )}

        {/* Device Sync Status (shown after saving) */}
        {showSyncStatus && syncStatus && syncStatus.devices && syncStatus.devices.length > 0 && (
          <div className="mb-6 p-4 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {t('settings.wakeword.syncStatus')}
              </h3>
              {!syncStatus.all_synced && syncPollingRef.current && (
                <Loader className="w-4 h-4 animate-spin text-blue-500" />
              )}
              {syncStatus.all_synced && (
                <CheckCircle className="w-4 h-4 text-green-500" />
              )}
            </div>

            <div className="space-y-2">
              {syncStatus.devices.map((device) => (
                <div
                  key={device.device_id}
                  className={`flex items-center justify-between p-2 rounded ${
                    device.synced
                      ? 'bg-green-50 dark:bg-green-900/20'
                      : 'bg-amber-50 dark:bg-amber-900/20'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    {device.device_type === 'satellite' ? (
                      <Satellite className="w-4 h-4 text-gray-500" />
                    ) : (
                      <Monitor className="w-4 h-4 text-gray-500" />
                    )}
                    <span className="text-sm text-gray-700 dark:text-gray-300">
                      {device.device_id}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {device.synced ? (
                      <span className="flex items-center gap-1 text-sm text-green-600 dark:text-green-400">
                        <CheckCircle className="w-4 h-4" />
                        {device.active_keywords?.join(', ')}
                      </span>
                    ) : device.error ? (
                      <span className="flex items-center gap-1 text-sm text-red-600 dark:text-red-400">
                        <XCircle className="w-4 h-4" />
                        {device.error}
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-sm text-amber-600 dark:text-amber-400">
                        <Clock className="w-4 h-4" />
                        {t('settings.wakeword.pending')}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>

            {syncStatus.failed_count > 0 && (
              <p className="mt-2 text-sm text-amber-600 dark:text-amber-400">
                {t('settings.wakeword.syncWarning', { count: syncStatus.failed_count })}
              </p>
            )}
          </div>
        )}

        {/* Save Button */}
        <div className="flex items-center justify-end gap-4 pt-4 border-t border-gray-200 dark:border-gray-700">
          {hasChanges && (
            <span className="text-sm text-amber-600 dark:text-amber-400">
              {t('settings.unsavedChanges')}
            </span>
          )}
          <button
            onClick={handleSave}
            disabled={!hasChanges || saving}
            className={`btn btn-primary flex items-center gap-2 ${
              !hasChanges ? 'opacity-50 cursor-not-allowed' : ''
            }`}
          >
            {saving ? (
              <Loader className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {t('common.save')}
          </button>
        </div>

        {/* Broadcast Hint */}
        <p className="mt-4 text-sm text-gray-500 dark:text-gray-400 italic">
          {t('settings.wakeword.broadcastHint')}
        </p>
      </div>
    </div>
  );
}
