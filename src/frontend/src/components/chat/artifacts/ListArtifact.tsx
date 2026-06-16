/**
 * ListArtifact — a typed `list` artifact as a semantic <ul>/<ol>.
 *
 * Items are React text children → escaped. Empty list → warm empty state
 * (distinct from the render-fail fallback; an empty shopping list is meaningful).
 */
import { useTranslation } from 'react-i18next';
import type { ListData } from './artifactSchema';

export default function ListArtifact({ data }: { data: ListData }) {
  const { t } = useTranslation();

  if (data.items.length === 0) {
    return (
      <p className="text-sm italic text-accent-700 dark:text-accent-300">
        {t('chat.artifacts.emptyList')}
      </p>
    );
  }

  const itemClass = 'text-sm text-gray-800 dark:text-gray-200';
  const items = data.items.map((item, i) => (
    <li key={i} className={itemClass}>
      {item}
    </li>
  ));

  return data.ordered ? (
    <ol className="list-decimal pl-5 space-y-1">{items}</ol>
  ) : (
    <ul className="list-disc pl-5 space-y-1">{items}</ul>
  );
}
