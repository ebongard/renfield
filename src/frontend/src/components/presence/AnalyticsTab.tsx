/**
 * Analytics tab — user/time-range selectors + heatmap + predictions.
 */
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { BarChart3, RefreshCw } from 'lucide-react';

import apiClient from '../../utils/axios';
import type { HeatmapCell } from './PresenceHeatmap';
import PresenceHeatmap from './PresenceHeatmap';
import type { PredictionRow } from './PresencePredictions';
import PresencePredictions from './PresencePredictions';

export interface PresenceUser {
  id: string | number;
  username: string;
}

interface TimeRange {
  days: number;
  key: 'days7' | 'days30' | 'days60' | 'days90';
}

const TIME_RANGES: TimeRange[] = [
  { days: 7, key: 'days7' },
  { days: 30, key: 'days30' },
  { days: 60, key: 'days60' },
  { days: 90, key: 'days90' },
];

interface AnalyticsTabProps {
  users: PresenceUser[];
}

interface HeatmapQuery {
  days: number;
  user_id?: string | number;
}

export default function AnalyticsTab({ users }: AnalyticsTabProps) {
  const { t } = useTranslation();

  const [selectedUserId, setSelectedUserId] = useState<string>('');
  const [days, setDays] = useState<number>(30);
  const [heatmapData, setHeatmapData] = useState<HeatmapCell[]>([]);
  const [predictionsData, setPredictionsData] = useState<PredictionRow[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const loadHeatmap = useCallback(async (): Promise<void> => {
    try {
      const params: HeatmapQuery = { days };
      if (selectedUserId) params.user_id = selectedUserId;
      const res = await apiClient.get<HeatmapCell[]>('/api/presence/analytics/heatmap', { params });
      setHeatmapData(res.data ?? []);
    } catch {
      setHeatmapData([]);
    }
  }, [days, selectedUserId]);

  const loadPredictions = useCallback(async (): Promise<void> => {
    if (!selectedUserId) {
      setPredictionsData([]);
      return;
    }
    try {
      const res = await apiClient.get<PredictionRow[]>('/api/presence/analytics/predictions', {
        params: { user_id: selectedUserId, days },
      });
      setPredictionsData(res.data ?? []);
    } catch {
      setPredictionsData([]);
    }
  }, [selectedUserId, days]);

  const loadAll = useCallback(async (): Promise<void> => {
    setLoading(true);
    await Promise.all([loadHeatmap(), loadPredictions()]);
    setLoading(false);
  }, [loadHeatmap, loadPredictions]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {t('presence.user')}:
          </label>
          <select
            value={selectedUserId}
            onChange={(e) => setSelectedUserId(e.target.value)}
            className="input text-sm py-1.5"
          >
            <option value="">{t('presence.allUsers')}</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {t('presence.timeRange')}:
          </span>
          <div className="flex gap-1">
            {TIME_RANGES.map(({ days: d, key }) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-3 py-1 text-sm rounded-full transition-colors ${
                  days === d
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                }`}
              >
                {t(`presence.${key}`)}
              </button>
            ))}
          </div>
        </div>

        <button
          onClick={loadAll}
          disabled={loading}
          className="p-2 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          title={t('common.refresh')}
        >
          <RefreshCw className={`w-5 h-5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <PresenceHeatmap data={heatmapData} />

      {selectedUserId ? (
        <PresencePredictions data={predictionsData} />
      ) : (
        <div className="card p-8 text-center">
          <BarChart3 className="w-10 h-10 text-gray-400 mx-auto mb-3" />
          <p className="text-gray-500 dark:text-gray-400">
            {t('presence.selectUserForPrediction')}
          </p>
        </div>
      )}
    </div>
  );
}
