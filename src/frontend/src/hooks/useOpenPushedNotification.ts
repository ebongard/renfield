/**
 * Web Push (#79) -- what a click on a push leads to.
 *
 * The push itself carries no content, only an id. The service worker hands
 * that id to the app in one of two ways: a message to an already open tab, or
 * `?notification=<id>` on the URL of a newly opened one. This hook picks it
 * up, fetches the caller's notifications over the authenticated API -- where
 * the server filters them again -- and shows the one that was meant in the
 * existing toast (`renfield-notification`, the event `useNotifications`
 * already listens to).
 *
 * A notification that is no longer in the caller's list (expired, dismissed,
 * addressed to another account on this browser) is simply not shown.
 */
import { useCallback, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router';

import apiClient from '../utils/axios';
import { OPEN_NOTIFICATION_MESSAGE, OPEN_NOTIFICATION_PARAM } from '../sw/pushProtocol';
import type { NotificationData } from './useNotifications';

interface NotificationRow {
  id: number;
  title: string;
  message: string;
  urgency: string;
  source: string;
  room_name: string | null;
  created_at: string;
}

function toUrgency(value: string): NotificationData['urgency'] {
  return value === 'critical' || value === 'low' ? value : 'info';
}

function validId(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null;
}

export function useOpenPushedNotification(enabled: boolean): void {
  const location = useLocation();
  const navigate = useNavigate();

  const show = useCallback(async (id: number) => {
    try {
      const response = await apiClient.get<{ notifications: NotificationRow[] }>(
        '/api/notifications',
        { params: { limit: 200 } },
      );
      const row = (response.data.notifications ?? []).find((n) => n.id === id);
      if (!row) return;
      const detail: NotificationData = {
        notification_id: row.id,
        title: row.title,
        message: row.message,
        urgency: toUrgency(row.urgency),
        source: row.source,
        room: row.room_name,
        // Shown because the person tapped it -- not something to read aloud.
        tts_handled: true,
        created_at: row.created_at,
      };
      window.dispatchEvent(new CustomEvent<NotificationData>('renfield-notification', { detail }));
    } catch {
      // The push was only a hint; a failed fetch leaves the app as it was.
    }
  }, []);

  // A window opened by the click: the id is on the URL. Take it off again so a
  // reload does not replay it.
  useEffect(() => {
    if (!enabled) return;
    const params = new URLSearchParams(location.search);
    const raw = params.get(OPEN_NOTIFICATION_PARAM);
    if (raw === null) return;
    params.delete(OPEN_NOTIFICATION_PARAM);
    const search = params.toString();
    navigate(
      { pathname: location.pathname, search: search ? `?${search}` : '', hash: location.hash },
      { replace: true },
    );
    const id = validId(Number(raw));
    if (id !== null) void show(id);
  }, [enabled, location.pathname, location.search, location.hash, navigate, show]);

  // A tab that was already open: the worker focuses it and posts the id.
  useEffect(() => {
    if (!enabled || !('serviceWorker' in navigator)) return undefined;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; notificationId?: unknown } | null;
      if (!data || data.type !== OPEN_NOTIFICATION_MESSAGE) return;
      const id = validId(data.notificationId);
      if (id !== null) void show(id);
    };
    const container = navigator.serviceWorker;
    container.addEventListener('message', onMessage);
    return () => container.removeEventListener('message', onMessage);
  }, [enabled, show]);
}

export default useOpenPushedNotification;
