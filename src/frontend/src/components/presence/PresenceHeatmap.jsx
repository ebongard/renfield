/**
 * Room x Hour heatmap using a pure CSS/HTML table.
 * Color intensity reflects entry count per cell.
 */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

function getHeatColor(value, max) {
  if (!value || max === 0) return '';
  const ratio = value / max;
  if (ratio > 0.7) return 'bg-blue-600 dark:bg-blue-500 text-white';
  if (ratio > 0.4) return 'bg-blue-400 dark:bg-blue-600 text-white';
  if (ratio > 0.15) return 'bg-blue-200 dark:bg-blue-800 text-blue-900 dark:text-blue-100';
  return 'bg-blue-100 dark:bg-blue-900/50 text-blue-800 dark:text-blue-200';
}

export default function PresenceHeatmap({ data }) {
  const { t } = useTranslation();

  const { rooms, maxCount, grid } = useMemo(() => {
    if (!data || data.length === 0) return { rooms: [], maxCount: 0, grid: {} };

    const roomMap = {};
    let max = 0;
    const g = {};

    for (const cell of data) {
      roomMap[cell.room_id] = cell.room_name;
      const key = `${cell.room_id}-${cell.hour}`;
      g[key] = cell.count;
      if (cell.count > max) max = cell.count;
    }

    const roomList = Object.entries(roomMap)
      .map(([id, name]) => ({ id: parseInt(id), name }))
      .sort((a, b) => a.name.localeCompare(b.name));

    return { rooms: roomList, maxCount: max, grid: g };
  }, [data]);

  const hours = Array.from({ length: 24 }, (_, i) => i);

  if (rooms.length === 0) {
    return (
      <div className="card p-8 text-center">
        <p className="text-gray-500 dark:text-gray-400">{t('presence.noAnalyticsData')}</p>
      </div>
    );
  }

  return (
    <div className="card overflow-hidden">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-white p-4 pb-2">
        {t('presence.heatmapTitle')}
      </h3>
      <div className="overflow-x-auto p-4 pt-0">
        <table className="w-full text-xs">
          <thead>
            <tr>
              <th className="text-left py-2 px-2 font-medium text-gray-600 dark:text-gray-400 sticky left-0 bg-white dark:bg-gray-800 z-10 min-w-[100px]">
                &nbsp;
              </th>
              {hours.map((h) => (
                <th key={h} className="py-2 px-1 font-medium text-gray-500 dark:text-gray-400 text-center min-w-[32px]">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rooms.map((room) => (
              <tr key={room.id}>
                <td className="py-1 px-2 font-medium text-gray-900 dark:text-white truncate sticky left-0 bg-white dark:bg-gray-800 z-10">
                  {room.name}
                </td>
                {hours.map((h) => {
                  const count = grid[`${room.id}-${h}`] || 0;
                  return (
                    <td key={h} className="py-1 px-0.5 text-center">
                      <div
                        className={`rounded px-1 py-0.5 text-[10px] leading-tight ${
                          count > 0
                            ? getHeatColor(count, maxCount)
                            : 'bg-gray-50 dark:bg-gray-800/50 text-gray-300 dark:text-gray-600'
                        }`}
                        title={`${room.name} @ ${h}:00 — ${count} ${count === 1 ? 'entry' : 'entries'}`}
                      >
                        {count > 0 ? count : ''}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
