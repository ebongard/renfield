/**
 * Satellite Monitoring Page
 *
 * Admin page for monitoring and debugging satellite voice assistants.
 * Shows live status, audio levels, wake word detection, and session history.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import apiClient from '../utils/axios';
import {
  Satellite, Wifi, WifiOff, Mic, Volume2, Cpu, Thermometer,
  Activity, Clock, AlertCircle, CheckCircle, RefreshCw, ChevronDown,
  ChevronUp, Radio, Zap, MemoryStick, ArrowUpCircle, Loader2, Package
} from 'lucide-react';

// Audio level visualization component
function AudioLevelMeter({ level, maxLevel = 100, label }) {
  const percentage = Math.min((level / maxLevel) * 100, 100);
  const getColor = (pct) => {
    if (pct > 80) return 'bg-red-500';
    if (pct > 60) return 'bg-yellow-500';
    return 'bg-green-500';
  };

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-500 dark:text-gray-400 w-8">{label}</span>
      <div className="flex-1 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div
          className={`h-full ${getColor(percentage)} transition-all duration-100`}
          style={{ width: `${percentage}%` }}
        />
      </div>
      <span className="text-xs text-gray-600 dark:text-gray-400 w-16 text-right">
        {typeof level === 'number' ? level.toFixed(1) : '-'}
      </span>
    </div>
  );
}

// State indicator badge
function StateBadge({ state }) {
  const { t } = useTranslation();

  const stateConfig = {
    idle: { color: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300', icon: Radio },
    listening: { color: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400', icon: Mic },
    processing: { color: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400', icon: Zap },
    speaking: { color: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400', icon: Volume2 },
    error: { color: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400', icon: AlertCircle },
  };

  const config = stateConfig[state] || stateConfig.idle;
  const Icon = config.icon;

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${config.color}`}>
      <Icon className="w-3 h-3" />
      {t(`satellites.states.${state}`, state.toUpperCase())}
    </span>
  );
}

// Progress bar for update
function UpdateProgressBar({ progress, className = '' }) {
  return (
    <div className={`w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden ${className}`}>
      <div
        className="h-full bg-blue-500 transition-all duration-300"
        style={{ width: `${progress}%` }}
      />
    </div>
  );
}

// Single satellite card
function SatelliteCard({ satellite, expanded, onToggle, latestVersion, onUpdate }) {
  const { t } = useTranslation();
  const [updating, setUpdating] = useState(false);

  const formatDuration = (seconds) => {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  };

  const formatAgo = (seconds) => {
    if (seconds < 5) return t('satellites.justNow', 'just now');
    if (seconds < 60) return t('satellites.secondsAgo', '{{count}}s ago', { count: Math.round(seconds) });
    if (seconds < 3600) return t('satellites.minutesAgo', '{{count}}m ago', { count: Math.floor(seconds / 60) });
    return t('satellites.hoursAgo', '{{count}}h ago', { count: Math.floor(seconds / 3600) });
  };

  const handleUpdate = async () => {
    setUpdating(true);
    try {
      await onUpdate(satellite.satellite_id);
    } finally {
      setUpdating(false);
    }
  };

  const metrics = satellite.metrics || {};
  const hasActiveSession = satellite.has_active_session;
  const hasUpdate = satellite.update_available && satellite.update_status !== 'in_progress';
  const isUpdating = satellite.update_status === 'in_progress';

  return (
    <div className="card">
      {/* Header */}
      <div
        className="flex items-center justify-between cursor-pointer"
        onClick={onToggle}
      >
        <div className="flex items-center gap-3">
          <div className={`p-2 rounded-lg ${hasActiveSession ? 'bg-green-100 dark:bg-green-900/30' : 'bg-gray-100 dark:bg-gray-800'}`}>
            <Satellite className={`w-5 h-5 ${hasActiveSession ? 'text-green-600 dark:text-green-400' : 'text-gray-500'}`} />
          </div>
          <div>
            <h3 className="font-medium text-gray-900 dark:text-white">
              {satellite.satellite_id}
            </h3>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {satellite.room}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* Version badge */}
          <span className="text-xs bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 px-2 py-1 rounded-sm">
            v{satellite.version || 'unknown'}
          </span>

          {/* Update available indicator */}
          {hasUpdate && !isUpdating && (
            <span className="inline-flex items-center gap-1 text-xs bg-yellow-100 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-400 px-2 py-1 rounded-sm">
              <ArrowUpCircle className="w-3 h-3" />
              {t('satellites.updateAvailable', 'Update')}
            </span>
          )}

          {/* Updating indicator */}
          {isUpdating && (
            <span className="inline-flex items-center gap-1 text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-800 dark:text-blue-400 px-2 py-1 rounded-sm">
              <Loader2 className="w-3 h-3 animate-spin" />
              {t('satellites.updating', 'Updating...')}
            </span>
          )}

          <StateBadge state={satellite.state} />
          {expanded ? (
            <ChevronUp className="w-5 h-5 text-gray-400" />
          ) : (
            <ChevronDown className="w-5 h-5 text-gray-400" />
          )}
        </div>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700 space-y-4">
          {/* Connection info */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <span className="text-gray-500 dark:text-gray-400">{t('satellites.uptime', 'Uptime')}</span>
              <p className="font-medium text-gray-900 dark:text-white">
                {formatDuration(satellite.uptime_seconds)}
              </p>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">{t('satellites.lastHeartbeat', 'Last Heartbeat')}</span>
              <p className="font-medium text-gray-900 dark:text-white">
                {formatAgo(satellite.heartbeat_ago_seconds)}
              </p>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">{t('satellites.sessions1h', 'Sessions (1h)')}</span>
              <p className="font-medium text-gray-900 dark:text-white">
                {metrics.session_count_1h || 0}
              </p>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">{t('satellites.errors1h', 'Errors (1h)')}</span>
              <p className={`font-medium ${metrics.error_count_1h > 0 ? 'text-red-600 dark:text-red-400' : 'text-gray-900 dark:text-white'}`}>
                {metrics.error_count_1h || 0}
              </p>
            </div>
          </div>

          {/* Audio levels */}
          <div className="space-y-2">
            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
              <Mic className="w-4 h-4" />
              {t('satellites.audioLevels', 'Audio Levels')}
            </h4>
            <AudioLevelMeter
              level={metrics.audio_rms || 0}
              maxLevel={10000}
              label="RMS"
            />
            <AudioLevelMeter
              level={(metrics.audio_db || -96) + 96}
              maxLevel={96}
              label="dB"
            />
            <div className="flex items-center gap-2 text-sm">
              <span className="text-gray-500 dark:text-gray-400">{t('satellites.vad', 'VAD')}:</span>
              {metrics.is_speech ? (
                <span className="text-green-600 dark:text-green-400 flex items-center gap-1">
                  <Activity className="w-3 h-3" />
                  {t('satellites.speechDetected', 'Speech detected')}
                </span>
              ) : (
                <span className="text-gray-400">{t('satellites.silence', 'Silence')}</span>
              )}
            </div>
          </div>

          {/* System metrics */}
          {(metrics.cpu_percent != null || metrics.temperature != null) && (
            <div className="space-y-2">
              <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
                <Cpu className="w-4 h-4" />
                {t('satellites.system', 'System')}
              </h4>
              <div className="grid grid-cols-3 gap-4 text-sm">
                {metrics.cpu_percent != null && (
                  <div className="flex items-center gap-2">
                    <Cpu className="w-4 h-4 text-gray-400" />
                    <span className={metrics.cpu_percent > 80 ? 'text-red-600' : 'text-gray-900 dark:text-white'}>
                      {metrics.cpu_percent.toFixed(1)}%
                    </span>
                  </div>
                )}
                {metrics.memory_percent != null && (
                  <div className="flex items-center gap-2">
                    <MemoryStick className="w-4 h-4 text-gray-400" />
                    <span className={metrics.memory_percent > 80 ? 'text-red-600' : 'text-gray-900 dark:text-white'}>
                      {metrics.memory_percent.toFixed(1)}%
                    </span>
                  </div>
                )}
                {metrics.temperature != null && (
                  <div className="flex items-center gap-2">
                    <Thermometer className="w-4 h-4 text-gray-400" />
                    <span className={metrics.temperature > 70 ? 'text-red-600' : metrics.temperature > 60 ? 'text-yellow-600' : 'text-gray-900 dark:text-white'}>
                      {metrics.temperature.toFixed(1)}°C
                    </span>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Last wake word */}
          {metrics.last_wakeword && (
            <div className="space-y-1">
              <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {t('satellites.lastWakeword', 'Last Wake Word')}
              </h4>
              <div className="text-sm text-gray-600 dark:text-gray-400">
                <span className="font-medium text-gray-900 dark:text-white">
                  {metrics.last_wakeword.keyword}
                </span>
                {' '}({(metrics.last_wakeword.confidence * 100).toFixed(0)}%)
                {' '}{formatAgo(Date.now() / 1000 - metrics.last_wakeword.timestamp)}
              </div>
            </div>
          )}

          {/* Active session */}
          {satellite.current_session && (
            <div className="p-3 bg-green-50 dark:bg-green-900/20 rounded-lg">
              <h4 className="text-sm font-medium text-green-800 dark:text-green-400 flex items-center gap-2">
                <Activity className="w-4 h-4" />
                {t('satellites.activeSession', 'Active Session')}
              </h4>
              <div className="mt-2 text-sm text-green-700 dark:text-green-300">
                <p>{t('satellites.duration', 'Duration')}: {formatDuration(satellite.current_session.duration_seconds)}</p>
                <p>{t('satellites.audioChunks', 'Audio chunks')}: {satellite.current_session.audio_chunks_count}</p>
                {satellite.current_session.transcription && (
                  <p className="mt-1 italic">"{satellite.current_session.transcription}"</p>
                )}
              </div>
            </div>
          )}

          {/* Capabilities */}
          <div className="flex flex-wrap gap-2 text-xs">
            {satellite.capabilities?.local_wakeword && (
              <span className="px-2 py-1 bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400 rounded-sm">
                Wake Word
              </span>
            )}
            {satellite.capabilities?.speaker && (
              <span className="px-2 py-1 bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400 rounded-sm">
                Speaker
              </span>
            )}
            {satellite.capabilities?.led_count > 0 && (
              <span className="px-2 py-1 bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400 rounded-sm">
                {satellite.capabilities.led_count} LEDs
              </span>
            )}
          </div>

          {/* Software / Update section */}
          <div className="pt-4 border-t border-gray-200 dark:border-gray-700">
            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2 mb-3">
              <Package className="w-4 h-4" />
              {t('satellites.software', 'Software')}
            </h4>

            <div className="flex items-center justify-between text-sm mb-3">
              <div>
                <span className="text-gray-500 dark:text-gray-400">{t('satellites.currentVersion', 'Current Version')}:</span>
                <span className="ml-2 font-medium text-gray-900 dark:text-white">v{satellite.version || 'unknown'}</span>
              </div>
              {hasUpdate && (
                <div>
                  <span className="text-gray-500 dark:text-gray-400">{t('satellites.latestVersion', 'Latest')}:</span>
                  <span className="ml-2 font-medium text-green-600 dark:text-green-400">v{latestVersion}</span>
                </div>
              )}
            </div>

            {/* Update progress */}
            {isUpdating && (
              <div className="mb-3 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg">
                <div className="flex items-center gap-2 mb-2">
                  <Loader2 className="w-4 h-4 animate-spin text-blue-600 dark:text-blue-400" />
                  <span className="text-sm font-medium text-blue-800 dark:text-blue-400">
                    {t(`satellites.updateStage.${satellite.update_stage}`, satellite.update_stage || 'Updating...')}
                  </span>
                </div>
                <UpdateProgressBar progress={satellite.update_progress || 0} />
                <p className="mt-1 text-xs text-blue-600 dark:text-blue-400">{satellite.update_progress || 0}%</p>
              </div>
            )}

            {/* Update failed */}
            {satellite.update_status === 'failed' && satellite.update_error && (
              <div className="mb-3 p-3 bg-red-50 dark:bg-red-900/20 rounded-lg">
                <div className="flex items-center gap-2">
                  <AlertCircle className="w-4 h-4 text-red-600 dark:text-red-400" />
                  <span className="text-sm font-medium text-red-800 dark:text-red-400">
                    {t('satellites.updateFailed', 'Update Failed')}
                  </span>
                </div>
                <p className="mt-1 text-xs text-red-600 dark:text-red-400">{satellite.update_error}</p>
              </div>
            )}

            {/* Update button or status */}
            <div className="flex items-center gap-2">
              {isUpdating ? (
                <span className="text-sm text-gray-500 dark:text-gray-400">
                  {t('satellites.updateInProgress', 'Update in progress...')}
                </span>
              ) : hasUpdate ? (
                <button
                  onClick={handleUpdate}
                  disabled={updating}
                  className="inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg disabled:opacity-50"
                >
                  {updating ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <ArrowUpCircle className="w-4 h-4" />
                  )}
                  {t('satellites.updateNow', 'Update Now')}
                </button>
              ) : (
                <span className="inline-flex items-center gap-1 text-sm text-green-600 dark:text-green-400">
                  <CheckCircle className="w-4 h-4" />
                  {t('satellites.upToDate', 'Up to date')}
                </span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SatellitesPage() {
  const { t } = useTranslation();

  const [satellites, setSatellites] = useState([]);
  const [latestVersion, setLatestVersion] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());
  const [autoRefresh, setAutoRefresh] = useState(true);

  const refreshIntervalRef = useRef(null);

  const loadSatellites = useCallback(async () => {
    try {
      const response = await apiClient.get('/api/satellites');
      setSatellites(response.data.satellites || []);
      setLatestVersion(response.data.latest_version || '');
      setError(null);
    } catch (err) {
      console.error('Failed to load satellites:', err);
      setError(t('satellites.loadError', 'Failed to load satellites'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  const triggerUpdate = useCallback(async (satelliteId) => {
    try {
      await apiClient.post(`/api/satellites/${satelliteId}/update`);
      // Refresh to see update status
      await loadSatellites();
    } catch (err) {
      console.error('Failed to trigger update:', err);
      setError(t('satellites.updateError', 'Failed to trigger update'));
    }
  }, [loadSatellites, t]);

  useEffect(() => {
    loadSatellites();
  }, [loadSatellites]);

  // Auto-refresh
  useEffect(() => {
    if (autoRefresh) {
      refreshIntervalRef.current = setInterval(loadSatellites, 2000);
    }
    return () => {
      if (refreshIntervalRef.current) {
        clearInterval(refreshIntervalRef.current);
      }
    };
  }, [autoRefresh, loadSatellites]);

  const toggleExpanded = (id) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const onlineCount = satellites.length;
  const activeCount = satellites.filter(s => s.has_active_session).length;

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center gap-3 mb-6">
          <Satellite className="w-8 h-8 text-blue-500" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            {t('satellites.title', 'Satellite Monitor')}
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
          <Satellite className="w-8 h-8 text-blue-500" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
              {t('satellites.title', 'Satellite Monitor')}
            </h1>
            <p className="text-gray-600 dark:text-gray-400">
              {t('satellites.subtitle', 'Live status of connected satellites')}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          {/* Auto-refresh toggle */}
          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded-sm border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            {t('satellites.autoRefresh', 'Auto-refresh')}
          </label>

          <button
            onClick={loadSatellites}
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
              <Wifi className="w-5 h-5 text-green-600 dark:text-green-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">{onlineCount}</p>
              <p className="text-sm text-gray-500 dark:text-gray-400">{t('satellites.online', 'Online')}</p>
            </div>
          </div>
        </div>

        <div className="card p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-blue-100 dark:bg-blue-900/30 rounded-lg">
              <Activity className="w-5 h-5 text-blue-600 dark:text-blue-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">{activeCount}</p>
              <p className="text-sm text-gray-500 dark:text-gray-400">{t('satellites.active', 'Active')}</p>
            </div>
          </div>
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="mb-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg flex items-center gap-2 text-red-700 dark:text-red-400">
          <AlertCircle className="w-5 h-5" />
          {error}
        </div>
      )}

      {/* Satellite list */}
      {satellites.length === 0 ? (
        <div className="card p-12 text-center">
          <WifiOff className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
            {t('satellites.noSatellites', 'No satellites connected')}
          </h3>
          <p className="text-gray-500 dark:text-gray-400">
            {t('satellites.noSatellitesDesc', 'Satellites will appear here when they connect to the server.')}
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {satellites.map((satellite) => (
            <SatelliteCard
              key={satellite.satellite_id}
              satellite={satellite}
              expanded={expandedIds.has(satellite.satellite_id)}
              onToggle={() => toggleExpanded(satellite.satellite_id)}
              latestVersion={latestVersion}
              onUpdate={triggerUpdate}
            />
          ))}
        </div>
      )}
    </div>
  );
}
