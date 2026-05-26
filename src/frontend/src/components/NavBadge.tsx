import { useTranslation } from 'react-i18next';

interface NavBadgeProps {
  count: number;
  ariaLabelKey?: string;
}

/**
 * Numeric chip displayed next to a sidebar nav entry — e.g. the count of
 * skill drafts waiting in the admin Skills Inbox. Hidden when count is 0
 * so an inbox-at-zero stays visually quiet. Uses DESIGN.md accent color
 * tokens.
 */
export default function NavBadge({ count, ariaLabelKey = 'selfLearning.nav.draftCount' }: NavBadgeProps) {
  const { t } = useTranslation();
  if (!count) return null;
  const display = count > 99 ? '99+' : String(count);
  return (
    <span
      className="ml-auto inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full text-xs font-medium bg-accent-500/20 text-accent-700 dark:bg-accent-400/20 dark:text-accent-300"
      aria-label={t(ariaLabelKey, { count })}
      data-testid="nav-badge"
    >
      {display}
    </span>
  );
}
