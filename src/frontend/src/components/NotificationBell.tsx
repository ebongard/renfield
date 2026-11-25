import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bell, Check, CheckCheck } from 'lucide-react';

import {
  UNREAD_STATUSES,
  useAcknowledgeAllNotifications,
  useAcknowledgeNotification,
  useMyNotifications,
  useUnreadNotificationCount,
  type BellNotification,
} from '../api/resources/notifications';

/** The backend sends naive UTC (`isoformat()` without offset). */
function parseServerTime(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
}

function relativeTime(value: string, locale: string): string {
  const seconds = Math.round((parseServerTime(value).getTime() - Date.now()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  const abs = Math.abs(seconds);
  if (abs < 60) return rtf.format(seconds, 'second');
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), 'hour');
  return rtf.format(Math.round(seconds / 86400), 'day');
}

interface NotificationBellProps {
  /** Feature flag AND a signed-in (or auth-off) viewer; false => renders nothing, fetches nothing. */
  enabled: boolean;
}

/**
 * Header bell with an unread badge (#1653). Lists the viewer's own notifications
 * plus public ones -- personal calls, voicemails and reminders that are never
 * broadcast to shared devices. The count refreshes live via the content-free
 * `notifications_changed` user event (useUserEvents), not by polling.
 */
export default function NotificationBell({ enabled }: NotificationBellProps) {
  const { t, i18n } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const panelId = useId();

  const count = useUnreadNotificationCount(enabled);
  const list = useMyNotifications(enabled && isOpen);
  const acknowledge = useAcknowledgeNotification();
  const acknowledgeAll = useAcknowledgeAllNotifications();

  useEffect(() => {
    if (!isOpen) return undefined;
    const onDocClick = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (containerRef.current && target && !containerRef.current.contains(target)) setIsOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsOpen(false);
        buttonRef.current?.focus();
      }
    };
    panelRef.current?.focus();
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [isOpen]);

  if (!enabled) return null;

  const unread = count.data ?? 0;
  const display = unread > 99 ? '99+' : String(unread);
  const items: BellNotification[] = list.data ?? [];

  return (
    <div className="relative" ref={containerRef}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setIsOpen((open) => !open)}
        className="relative p-2.5 rounded-lg transition-colors text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-700"
        aria-label={unread ? t('notifications.bell.labelUnread', { count: unread }) : t('notifications.bell.label')}
        aria-expanded={isOpen}
        aria-controls={panelId}
        data-testid="notification-bell"
      >
        <Bell className="w-5 h-5" aria-hidden="true" />
        {unread > 0 && (
          <span
            className="absolute top-1 right-1 inline-flex items-center justify-center min-w-[1.125rem] h-[1.125rem] px-1 rounded-full text-xs font-medium leading-none bg-primary-600 text-white dark:bg-primary-400 dark:text-gray-900"
            aria-hidden="true"
            data-testid="notification-bell-badge"
          >
            {display}
          </span>
        )}
      </button>

      {isOpen && (
        <div
          ref={panelRef}
          tabIndex={-1}
          id={panelId}
          className="absolute right-0 mt-2 w-80 focus:outline-hidden max-w-[calc(100vw-1rem)] rounded-lg shadow-lg bg-white border border-gray-200 dark:bg-gray-800 dark:border-gray-700 z-50"
          role="dialog"
          aria-label={t('notifications.title')}
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-white">{t('notifications.title')}</h2>
            {unread > 0 && (
              <button
                type="button"
                onClick={() => acknowledgeAll.mutate()}
                disabled={acknowledgeAll.isPending}
                className="inline-flex items-center gap-1 text-xs text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300 disabled:opacity-50"
              >
                <CheckCheck className="w-4 h-4" aria-hidden="true" />
                {t('notifications.bell.markAllRead')}
              </button>
            )}
          </div>

          {(acknowledge.errorMessage || acknowledgeAll.errorMessage) && (
            <p className="px-4 py-2 text-xs text-red-600 dark:text-red-400" role="alert">
              {acknowledge.errorMessage ?? acknowledgeAll.errorMessage}
            </p>
          )}

          <ul className="max-h-96 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700">
            {list.isLoading && (
              <li className="px-4 py-6 text-sm text-center text-gray-500 dark:text-gray-400">{t('common.loading')}</li>
            )}
            {list.errorMessage && (
              <li className="px-4 py-6 text-sm text-center text-red-600 dark:text-red-400">{list.errorMessage}</li>
            )}
            {!list.isLoading && !list.errorMessage && items.length === 0 && (
              <li className="px-4 py-6 text-sm text-center text-gray-500 dark:text-gray-400">{t('notifications.bell.empty')}</li>
            )}
            {items.map((n) => {
              const isUnread = UNREAD_STATUSES.has(n.status);
              return (
                <li key={n.id} className="flex items-start gap-3 px-4 py-3" data-testid="notification-bell-item">
                  <span
                    className={`mt-1.5 w-2 h-2 shrink-0 rounded-full ${isUnread ? 'bg-primary-600 dark:bg-primary-400' : 'bg-transparent'}`}
                    aria-hidden="true"
                  />
                  <div className="min-w-0 flex-1">
                    <p className={`text-sm truncate ${isUnread ? 'font-medium text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400'}`}>
                      {n.title}
                    </p>
                    <p className="text-xs text-gray-600 dark:text-gray-300 line-clamp-2">{n.message}</p>
                    <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">{relativeTime(n.created_at, i18n.language)}</p>
                  </div>
                  {isUnread && (
                    <button
                      type="button"
                      onClick={() => acknowledge.mutate(n.id)}
                      className="p-1.5 rounded-md text-gray-400 hover:text-gray-700 hover:bg-gray-100 dark:text-gray-500 dark:hover:text-gray-200 dark:hover:bg-gray-700"
                      aria-label={t('notifications.bell.markRead')}
                      title={t('notifications.bell.markRead')}
                    >
                      <Check className="w-4 h-4" aria-hidden="true" />
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
