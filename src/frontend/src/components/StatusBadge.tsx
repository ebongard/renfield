import { useTranslation } from 'react-i18next';

import type { SkillStatus } from '../api/resources/skills';

const STATUS_CLASS: Record<SkillStatus, string> = {
  draft: 'bg-amber-100 text-amber-800 dark:bg-amber-500/20 dark:text-amber-300',
  approved: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-500/20 dark:text-emerald-300',
  rejected: 'bg-rose-100 text-rose-800 dark:bg-rose-500/20 dark:text-rose-300',
  archived: 'bg-gray-200 text-gray-700 dark:bg-gray-700/40 dark:text-gray-400',
};

const STATUS_SYMBOL: Record<SkillStatus, string> = {
  draft: '◇',
  approved: '✓',
  rejected: '✕',
  archived: '◌',
};

interface StatusBadgeProps {
  status: SkillStatus;
  className?: string;
}

/**
 * Lifecycle badge for a procedural skill. Per DESIGN.md, color is never
 * the only signal — every status carries an Unicode mark + the localized
 * label, so the badge stays readable for color-blind users and in
 * grayscale screenshots.
 */
export default function StatusBadge({ status, className = '' }: StatusBadgeProps) {
  const { t } = useTranslation();
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${STATUS_CLASS[status]} ${className}`}
      data-testid={`status-badge-${status}`}
    >
      <span aria-hidden="true">{STATUS_SYMBOL[status]}</span>
      <span>{t(`selfLearning.skills.status.${status}`)}</span>
    </span>
  );
}
