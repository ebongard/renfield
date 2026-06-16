/**
 * TableArtifact — a typed `table` artifact rendered as a real semantic <table>.
 *
 * Every cell is a React text child → React auto-escapes; no HTML is parsed, so
 * `<img onerror>` / `</td><script>` payloads render as inert visible text. The
 * table is wrapped in an `overflow-x:auto` container bounded to the bubble width
 * (§9 responsive) so a wide table scrolls horizontally instead of widening the
 * chat column. Numerics use DM Sans `tabular-nums` (DESIGN.md §45).
 */
import { useTranslation } from 'react-i18next';
import type { TableData } from './artifactSchema';

export default function TableArtifact({ data }: { data: TableData }) {
  const { t } = useTranslation();

  if (data.rows.length === 0) {
    return (
      <p className="text-sm italic text-accent-700 dark:text-accent-300">
        {t('chat.artifacts.emptyTable')}
      </p>
    );
  }

  return (
    <div className="overflow-x-auto max-w-full">
      <table className="w-full text-sm tabular-nums border-collapse">
        {data.columns.length > 0 && (
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-600">
              {data.columns.map((col, i) => (
                <th
                  key={i}
                  scope="col"
                  className="px-3 py-1.5 text-left font-medium text-gray-600 dark:text-gray-300 whitespace-nowrap"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {data.rows.map((row, r) => (
            <tr
              key={r}
              className={
                r % 2 === 1
                  ? 'bg-gray-50 dark:bg-gray-700/40'
                  : 'bg-transparent'
              }
            >
              {row.map((cell, c) => (
                <td
                  key={c}
                  className="px-3 py-1.5 text-gray-800 dark:text-gray-200 align-top"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
