/**
 * Presence predictions bar chart using Recharts.
 * Shows probability of a user being in each room by hour for a selected day.
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts';

const DAY_KEYS = ['daySun', 'dayMon', 'dayTue', 'dayWed', 'dayThu', 'dayFri', 'daySat'];

// Consistent room colors
const ROOM_COLORS = [
  '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
  '#ec4899', '#06b6d4', '#84cc16',
];

export default function PresencePredictions({ data }) {
  const { t } = useTranslation();

  // Default to current day of week (JS: 0=Sun)
  const [selectedDay, setSelectedDay] = useState(() => new Date().getDay());

  const { chartData, rooms } = useMemo(() => {
    if (!data || data.length === 0) return { chartData: [], rooms: [] };

    // Filter by selected day
    const filtered = data.filter((d) => d.day_of_week === selectedDay);

    // Collect unique rooms
    const roomMap = {};
    for (const entry of filtered) {
      roomMap[entry.room_id] = entry.room_name;
    }
    const roomList = Object.entries(roomMap)
      .map(([id, name]) => ({ id: parseInt(id), name }))
      .sort((a, b) => a.name.localeCompare(b.name));

    // Build hour → {hour, room1: prob, room2: prob, ...}
    const hourMap = {};
    for (let h = 0; h < 24; h++) {
      hourMap[h] = { hour: `${h}:00` };
      for (const room of roomList) {
        hourMap[h][room.name] = 0;
      }
    }
    for (const entry of filtered) {
      const roomName = roomMap[entry.room_id];
      hourMap[entry.hour][roomName] = Math.round(entry.probability * 100);
    }

    return {
      chartData: Object.values(hourMap),
      rooms: roomList,
    };
  }, [data, selectedDay]);

  if (!data || data.length === 0) {
    return (
      <div className="card p-8 text-center">
        <p className="text-gray-500 dark:text-gray-400">{t('presence.noAnalyticsData')}</p>
      </div>
    );
  }

  return (
    <div className="card p-4">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-3">
        {t('presence.predictionsTitle')}
      </h3>

      {/* Day selector pills */}
      <div className="flex gap-1 mb-4 flex-wrap">
        {DAY_KEYS.map((key, idx) => (
          <button
            key={idx}
            onClick={() => setSelectedDay(idx)}
            className={`px-3 py-1.5 text-sm rounded-full transition-colors ${
              selectedDay === idx
                ? 'bg-blue-600 text-white'
                : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
            }`}
          >
            {t(`presence.${key}`)}
          </button>
        ))}
      </div>

      {/* Chart */}
      {rooms.length > 0 ? (
        <ResponsiveContainer width="100%" height={300}>
          <BarChart data={chartData} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
            <XAxis
              dataKey="hour"
              tick={{ fontSize: 11 }}
              interval={2}
            />
            <YAxis
              tick={{ fontSize: 11 }}
              domain={[0, 100]}
              tickFormatter={(v) => `${v}%`}
              width={45}
            />
            <Tooltip
              formatter={(value) => `${value}%`}
              contentStyle={{
                backgroundColor: 'var(--color-bg-tooltip, #fff)',
                borderColor: 'var(--color-border-tooltip, #e5e7eb)',
                borderRadius: '8px',
                fontSize: '12px',
              }}
            />
            <Legend wrapperStyle={{ fontSize: '12px' }} />
            {rooms.map((room, idx) => (
              <Bar
                key={room.id}
                dataKey={room.name}
                fill={ROOM_COLORS[idx % ROOM_COLORS.length]}
                radius={[2, 2, 0, 0]}
                maxBarSize={20}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <p className="text-gray-500 dark:text-gray-400 text-center py-8">
          {t('presence.noAnalyticsData')}
        </p>
      )}
    </div>
  );
}
