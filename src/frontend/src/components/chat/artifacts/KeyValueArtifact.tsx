/**
 * KeyValueArtifact — a typed `keyvalue` artifact as a two-column grid.
 *
 * Reuses the FactSet two-column layout from AdaptiveCardRenderer (the same
 * `grid grid-cols-[auto_1fr]` shape) so it reads consistently with existing
 * cards. Keys + values are React text children → escaped.
 */
import { Fragment } from 'react';
import { useTranslation } from 'react-i18next';
import type { KeyValueData } from './artifactSchema';

export default function KeyValueArtifact({ data }: { data: KeyValueData }) {
  const { t } = useTranslation();

  if (data.pairs.length === 0) {
    return (
      <p className="text-sm italic text-accent-700 dark:text-accent-300">
        {t('chat.artifacts.emptyKeyValue')}
      </p>
    );
  }

  return (
    <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {data.pairs.map((pair, i) => (
        <Fragment key={i}>
          <span className="font-medium text-gray-600 dark:text-gray-400">{pair.key}</span>
          <span className="text-gray-800 dark:text-gray-200 tabular-nums">{pair.value}</span>
        </Fragment>
      ))}
    </div>
  );
}
