import { useTranslation } from 'react-i18next';
import { Loader, Clock, CheckCircle, AlertCircle } from 'lucide-react';

import type { Meeting } from '../../api/resources/meetings';

/** Icon + localized label for a meeting's processing status. Shared by the
 *  list cards (MeetingsPage) and the dedicated detail page (Track D). */
export default function StatusBadge({ status }: { status: Meeting['status'] }) {
  const { t } = useTranslation();
  const meta = {
    pending: { Icon: Clock, cls: 'text-gray-500 dark:text-gray-400', spin: false },
    processing: { Icon: Loader, cls: 'text-primary-500', spin: true },
    completed: { Icon: CheckCircle, cls: 'text-green-600 dark:text-green-400', spin: false },
    failed: { Icon: AlertCircle, cls: 'text-red-500', spin: false },
  }[status];
  const { Icon, cls, spin } = meta;
  return (
    <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${cls}`}>
      <Icon className={`w-4 h-4 ${spin ? 'animate-spin' : ''}`} aria-hidden="true" />
      {t(`meetings.status.${status}`)}
    </span>
  );
}
