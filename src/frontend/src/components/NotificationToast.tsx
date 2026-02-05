/**
 * NotificationToast — Slide-in toast notifications (top-right)
 *
 * - Urgency-based styling: critical=red, info=blue, low=gray
 * - Auto-dismiss: 10s for info/low, persistent for critical
 * - Dismiss + Acknowledge buttons
 * - Max 3 visible, rest queued
 * - Dark mode + i18n
 */

import { X, Check, Bell, AlertTriangle, Info, BellOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNotifications, type NotificationData } from '../hooks/useNotifications';

const urgencyStyles: Record<string, { container: string; icon: string; IconComponent: typeof Bell }> = {
  critical: {
    container: 'border-red-500 bg-red-50 dark:bg-red-900/30',
    icon: 'text-red-600 dark:text-red-400',
    IconComponent: AlertTriangle,
  },
  info: {
    container: 'border-blue-500 bg-blue-50 dark:bg-blue-900/30',
    icon: 'text-blue-600 dark:text-blue-400',
    IconComponent: Info,
  },
  low: {
    container: 'border-gray-400 bg-gray-50 dark:bg-gray-800',
    icon: 'text-gray-500 dark:text-gray-400',
    IconComponent: Bell,
  },
};

function SingleToast({
  notification,
  onAcknowledge,
  onDismiss,
  onSuppress,
}: {
  notification: NotificationData;
  onAcknowledge: (id: number) => void;
  onDismiss: (id: number) => void;
  onSuppress: (id: number) => void;
}) {
  const { t } = useTranslation();
  const style = urgencyStyles[notification.urgency] || urgencyStyles.info;
  const Icon = style.IconComponent;

  return (
    <div
      className={`
        pointer-events-auto w-80 max-w-sm rounded-lg border-l-4 shadow-lg
        ${style.container}
        animate-slide-in-right
        transition-all duration-300
      `}
      role="alert"
      aria-live={notification.urgency === 'critical' ? 'assertive' : 'polite'}
    >
      <div className="p-4">
        {/* Header */}
        <div className="flex items-start gap-3">
          <Icon className={`w-5 h-5 mt-0.5 shrink-0 ${style.icon}`} aria-hidden="true" />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-gray-900 dark:text-white truncate">
              {notification.title}
            </p>
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-300 line-clamp-3">
              {notification.message}
            </p>
            {notification.room && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {notification.room}
              </p>
            )}
          </div>
          <button
            onClick={() => onDismiss(notification.notification_id)}
            className="shrink-0 p-1 rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors"
            aria-label={t('notifications.dismiss')}
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Actions */}
        <div className="mt-3 flex justify-end gap-2">
          <button
            onClick={() => onSuppress(notification.notification_id)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md
              text-gray-500 dark:text-gray-400
              hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
            aria-label={t('notifications.suppress')}
            title={t('notifications.suppress')}
          >
            <BellOff className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => onAcknowledge(notification.notification_id)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md
              text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700
              border border-gray-300 dark:border-gray-600
              hover:bg-gray-50 dark:hover:bg-gray-600 transition-colors"
          >
            <Check className="w-3.5 h-3.5" />
            {t('notifications.acknowledge')}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function NotificationToast() {
  const { t } = useTranslation();
  const { visibleNotifications, queuedCount, acknowledge, dismiss, suppress } = useNotifications();

  if (visibleNotifications.length === 0) {
    return null;
  }

  return (
    <div
      className="fixed top-20 right-4 z-50 flex flex-col gap-3 pointer-events-none"
      aria-label={t('notifications.title')}
    >
      {visibleNotifications.map((notification) => (
        <SingleToast
          key={notification.notification_id}
          notification={notification}
          onAcknowledge={acknowledge}
          onDismiss={dismiss}
          onSuppress={suppress}
        />
      ))}
      {queuedCount > 0 && (
        <div className="pointer-events-auto text-center text-xs text-gray-500 dark:text-gray-400">
          {t('notifications.moreQueued', { count: queuedCount })}
        </div>
      )}
    </div>
  );
}
