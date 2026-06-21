/**
 * PresenceMapArtifact — a read-only presence-map widget (Gen-UI): rooms, each
 * with the people currently present. Pure typed JSON → React (no actions).
 */
import { useTranslation } from 'react-i18next';
import { MapPin, User } from 'lucide-react';
import type { PresenceMapData } from './artifactSchema';

export default function PresenceMapArtifact({ data }: { data: PresenceMapData }) {
  const { t } = useTranslation();

  if (data.rooms.length === 0) {
    return (
      <p className="text-sm italic text-accent-700 dark:text-accent-300">
        {t('chat.artifacts.presenceMap.empty')}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {data.rooms.map((r, i) => (
        <div key={i} className="flex items-start justify-between gap-3">
          <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium text-gray-700 dark:text-gray-300">
            <MapPin className="h-4 w-4 shrink-0 text-accent-500" aria-hidden="true" />
            <span className="truncate">{r.room}</span>
          </span>
          <span className="flex flex-wrap justify-end gap-1.5">
            {r.users.length === 0 ? (
              <span className="text-xs italic text-gray-400 dark:text-gray-500">
                {t('chat.artifacts.presenceMap.nobody')}
              </span>
            ) : (
              r.users.map((u, j) => (
                <span
                  key={j}
                  className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200"
                >
                  <User className="h-3 w-3" aria-hidden="true" />
                  {u}
                </span>
              ))
            )}
          </span>
        </div>
      ))}
    </div>
  );
}
