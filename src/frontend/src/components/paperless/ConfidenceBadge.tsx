/** LLM-confidence percentage badge (green ≥80 / yellow ≥60 / red below).
 *  Shared between the audit page and the editable review row. */
interface ConfidenceBadgeProps {
  value: number | null | undefined;
}

export default function ConfidenceBadge({ value }: ConfidenceBadgeProps) {
  if (value == null) return <span className="text-gray-400">-</span>;
  const pct = (value * 100).toFixed(0);
  const color =
    value >= 0.8
      ? 'text-green-600 dark:text-green-400'
      : value >= 0.6
        ? 'text-yellow-600 dark:text-yellow-400'
        : 'text-red-600 dark:text-red-400';
  return <span className={`text-xs font-medium ${color}`}>{pct}%</span>;
}
