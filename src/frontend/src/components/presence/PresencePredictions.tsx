/**
 * Presence predictions bar chart using Recharts.
 * Shows probability of a user being in each room by hour for a selected day.
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

export interface PredictionRow {
  day_of_week: number;
  room_id: number;
  room_name: string;
  hour: number;
  probability: number;
}

const DAY_KEYS = ['daySun', 'dayMon', 'dayTue', 'dayWed', 'dayThu', 'dayFri', 'daySat'] as const;

// Consistent room colors
const ROOM_COLORS: string[] = [
  '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
  '#ec4899', '#06b6d4', '#84cc16',
];

interface RoomEntry {
  id: number;
  name: string;
}

// Each chart row is keyed by hour string and carries one numeric column per room.
// `Record<string, string | number>` covers both the `hour` label and the
// dynamic per-room columns the chart consumes.
type ChartRow = Record<string, string | number>;

interface PresencePredictionsProps {
  data: PredictionRow[];
}

export default function PresencePredictions({ data }: PresencePredictionsProps) {
  const { t } = useTranslation();

  // Default to current day of week (JS: 0=Sun)
  const [selectedDay, setSelectedDay] = useState<number>(() => new Date().getDay());

  const { chartData, rooms } = useMemo<{ chartData: ChartRow[]; rooms: RoomEntry[] }>(() => {
    if (!data || data.length === 0) return { chartData: [], rooms: [] };

    // Filter by selected day
    const filtered = data.filter((d) => d.day_of_week === selectedDay);

    // Collect unique rooms
    const roomMap: Record<number, string> = {};
    for (const entry of filtered) {
      roomMap[entry.room_id] = entry.room_name;
    }
    const roomList: RoomEntry[] = Object.entries(roomMap)
      .map(([id, name]) => ({ id: parseInt(id, 10), name }))
      .sort((a, b) => a.name.localeCompare(b.name));

    // Build hour → {hour, room1: prob, room2: prob, ...}
    const hourMap: Record<number, ChartRow> = {};
    for (let h = 0; h < 24; h++) {
      const row: ChartRow = { hour: `${h}:00` };
      for (const room of roomList) {
        row[room.name] = 0;
      }
      hourMap[h] = row;
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
      <h3 className="text-base font-semibold text-gray-900 dark:text-white mb-3">
        {t('presence.predictionsTitle')}
      </h3>

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

      {rooms.length > 0 ? (
        <ResponsiveContainer width="100%" height={300}>
          <BarChart data={chartData} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
            <XAxis dataKey="hour" tick={{ fontSize: 11 }} interval={2} />
            <YAxis
              tick={{ fontSize: 11 }}
              domain={[0, 100]}
              tickFormatter={(v: number) => `${v}%`}
              width={45}
            />
            <Tooltip
              formatter={(value: number) => `${value}%`}
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
