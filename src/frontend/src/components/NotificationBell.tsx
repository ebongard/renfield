import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isAxiosError } from 'axios';
import { Bell, Check, CheckCheck, NotebookPen } from 'lucide-react';

import {
  UNREAD_STATUSES,
  useAcknowledgeAllNotifications,
  useAcknowledgeNotification,
  useMyNotifications,
  useUnreadNotificationCount,
  type BellNotification,
} from '../api/resources/notifications';
import { useCreateNote, type Note } from '../api/resources/notes';
import apiClient from '../utils/axios';

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

/** A note from a notification: title + date/time to the second (the title is the
 *  note's unique [[link]] key; ``attempt`` > 1 adds " (n)"), the message as body,
 *  tier 0 (only the viewer). The message may carry a caller's words (voicemail
 *  transcript): every bracket becomes its fullwidth form, so that text can never
 *  form a [[link]] in the knowledge graph -- for any run of brackets. */
export function noteFromNotification(
  n: Pick<BellNotification, 'title' | 'message' | 'created_at'>,
  locale: string,
  attempt = 1,
) {
  const when = new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'medium' })
    .format(parseServerTime(n.created_at));
  const unlink = (text: string) => text.replace(/\[/g, '［').replace(/\]/g, '］');
  const tail = ` – ${when}${attempt > 1 ? ` (${attempt})` : ''}`;
  // Cut the notification title, never the date (by code point: no split surrogate).
  const head = Array.from(unlink(n.title)).slice(0, Math.max(1, 255 - tail.length)).join('');
  return { title: `${head}${tail}`, body: unlink(n.message), circle_tier: 0 };
}

/** Try " (2)" .. up to this when the title is taken by a DIFFERENT note. */
const MAX_NOTE_ATTEMPTS = 5;

interface NotificationBellProps {
  /** Feature flag AND a signed-in (or auth-off) viewer; false => renders nothing, fetches nothing. */
  enabled: boolean;
  /** NOTES_ENABLED: offer "save as note" on each notification. */
  notesEnabled?: boolean;
}

/**
 * Header bell with an unread badge (#1653). Lists the viewer's own notifications
 * plus public ones -- personal calls, voicemails and reminders that are never
 * broadcast to shared devices. The count refreshes live via the content-free
 * `notifications_changed` user event (useUserEvents), not by polling.
 */
export default function NotificationBell({ enabled, notesEnabled = false }: NotificationBellProps) {
  const { t, i18n } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const panelId = useId();
  // Expanded messages by id. A long message (a voicemail transcript sits at its end)
  // is clamped to two lines until opened.
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  // Messages that do not fit their two clamped lines -- measured, not guessed from a
  // character count (the width depends on the panel, the read state and the screen).
  const [overflowing, setOverflowing] = useState<Set<number>>(() => new Set());
  const messageEls = useRef(new Map<number, HTMLParagraphElement>());
  const createNote = useCreateNote();
  const [savedAsNote, setSavedAsNote] = useState<Set<number>>(() => new Set());
  const [noteError, setNoteError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const markSaved = (id: number) => {
    setSavedAsNote((prev) => new Set(prev).add(id));
    setAnnouncement(t('notifications.bell.savedAsNote'));
  };
  const saveAsNote = async (n: BellNotification) => {
    setNoteError(null);
    for (let attempt = 1; attempt <= MAX_NOTE_ATTEMPTS; attempt += 1) {
      const note = noteFromNotification(n, i18n.language, attempt);
      try {
        await createNote.mutateAsync(note);
        markSaved(n.id);
        return;
      } catch (error) {
        if (!(isAxiosError(error) && error.response?.status === 409)) return; // shown via createNote.errorMessage
        createNote.reset();
        // The title is taken. Saved already only if that note holds THIS message;
        // another note of the same title (same minute, a hand-written one) is not it.
        try {
          const existing = (await apiClient.get<Note[]>('/api/notes')).data;
          if (Array.isArray(existing) && existing.some((e) => e.title === note.title && e.body === note.body)) {
            markSaved(n.id);
            return;
          }
        } catch {
          break;
        }
      }
    }
    setNoteError(t('notes.failedToSave'));
  };
  const toggleExpanded = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const resetNoteError = createNote.reset;
  useEffect(() => {
    if (!isOpen) {
      resetNoteError();
      setNoteError(null);
    }
  }, [isOpen, resetNoteError]);

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

  // After each render of the open list: which clamped messages are cut off? Only the
  // collapsed ones are measured (an expanded one keeps its toggle via ``expanded``).
  useLayoutEffect(() => {
    if (!isOpen) return;
    const next = new Set<number>();
    messageEls.current.forEach((el, id) => {
      if (el.scrollHeight > el.clientHeight + 1) next.add(id);
    });
    setOverflowing((prev) => {
      const same = prev.size === next.size && [...next].every((id) => prev.has(id));
      return same ? prev : next;
    });
  }, [isOpen, list.data, expanded]);

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

          {(acknowledge.errorMessage || acknowledgeAll.errorMessage || createNote.errorMessage || noteError) && (
            <p className="px-4 py-2 text-xs text-red-600 dark:text-red-400" role="alert">
              {acknowledge.errorMessage ?? acknowledgeAll.errorMessage ?? createNote.errorMessage ?? noteError}
            </p>
          )}
          <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

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
              const isExpanded = expanded.has(n.id);
              const isLong = isExpanded || overflowing.has(n.id);
              const isSaved = savedAsNote.has(n.id);
              const messageId = `${panelId}-message-${n.id}`;
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
                    <p
                      id={messageId}
                      ref={(el) => {
                        if (el) messageEls.current.set(n.id, el);
                        else messageEls.current.delete(n.id);
                      }}
                      className={`text-xs text-gray-600 dark:text-gray-300 break-words ${isExpanded ? '' : 'line-clamp-2'}`}
                      data-testid="notification-bell-message"
                    >
                      {n.message}
                    </p>
                    {(isLong || notesEnabled) && (
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
                        {isLong && (
                          <button
                            type="button"
                            onClick={() => toggleExpanded(n.id)}
                            aria-expanded={isExpanded}
                            aria-controls={messageId}
                            aria-describedby={messageId}
                            className="text-xs font-medium text-primary-600 hover:underline dark:text-primary-400"
                          >
                            {isExpanded ? t('notifications.bell.showLess') : t('notifications.bell.showMore')}
                          </button>
                        )}
                        {notesEnabled && (
                          // Stays mounted when saved (disabled), so keyboard focus is not lost.
                          <button
                            type="button"
                            onClick={() => void saveAsNote(n)}
                            disabled={isSaved || createNote.isPending}
                            aria-describedby={messageId}
                            className={`inline-flex items-center gap-1 text-xs font-medium ${isSaved
                              ? 'text-gray-500 dark:text-gray-400'
                              : 'text-primary-600 hover:underline dark:text-primary-400 disabled:opacity-50'}`}
                          >
                            {isSaved
                              ? <Check className="w-3.5 h-3.5" aria-hidden="true" />
                              : <NotebookPen className="w-3.5 h-3.5" aria-hidden="true" />}
                            {isSaved ? t('notifications.bell.savedAsNote') : t('notifications.bell.saveAsNote')}
                          </button>
                        )}
                      </div>
                    )}
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
