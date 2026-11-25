/**
 * Notification bell (#1653) -- `/api/notifications`. Auth on, the bell holds only
 * the notifications addressed to the viewer (admins too); `mine=true` and the
 * count use the same server-side rule. These routes exist only while
 * `notification_bell_enabled` is on; callers gate on that flag. Live refresh: the
 * content-free `notifications_changed` user event (and a socket reconnect)
 * invalidates `keys.notifications.all`; returning to the tab refetches too.
 */
import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiMutation, useApiQuery } from '../hooks';
import { keys } from '../keys';

export interface BellNotification {
  id: number;
  event_type: string;
  title: string;
  message: string;
  urgency: string;
  status: string;
  created_at: string;
  acknowledged_at: string | null;
}

/** Statuses the backend counts as unread (NotificationService.UNREAD_STATUSES). */
export const UNREAD_STATUSES: ReadonlySet<string> = new Set(['pending', 'delivered']);

const BELL_LIST_LIMIT = 20;

async function fetchUnreadCount(): Promise<number> {
  const response = await apiClient.get<{ count: number }>('/api/notifications/unread-count');
  return response.data.count ?? 0;
}

async function fetchMine(): Promise<BellNotification[]> {
  const response = await apiClient.get<{ notifications: BellNotification[] }>('/api/notifications', {
    params: { mine: true, limit: BELL_LIST_LIMIT },
  });
  // The server already leaves dismissed ones out; acknowledged ones stay, dimmed.
  return response.data.notifications ?? [];
}

export function useUnreadNotificationCount(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.notifications.unreadCount(),
      queryFn: fetchUnreadCount,
      enabled,
      // Safety net for a missed event or USER_EVENTS_ENABLED=false: one cheap
      // COUNT when the user comes back to the tab (the app-wide default is off).
      refetchOnWindowFocus: true,
    },
    'notifications.bell.failed',
  );
}

export function useMyNotifications(enabled: boolean) {
  return useApiQuery(
    { queryKey: keys.notifications.mine(), queryFn: fetchMine, enabled },
    'notifications.bell.failed',
  );
}

export function useAcknowledgeNotification() {
  const queryClient = useQueryClient();
  return useApiMutation<void, number>(
    {
      mutationFn: async (id) => {
        await apiClient.patch(`/api/notifications/${id}/acknowledge`);
      },
      onSettled: () => queryClient.invalidateQueries({ queryKey: keys.notifications.all }),
    },
    'notifications.bell.failed',
  );
}

export function useAcknowledgeAllNotifications() {
  const queryClient = useQueryClient();
  return useApiMutation<void, void>(
    {
      mutationFn: async () => {
        await apiClient.post('/api/notifications/acknowledge-all');
      },
      onSettled: () => queryClient.invalidateQueries({ queryKey: keys.notifications.all }),
    },
    'notifications.bell.failed',
  );
}
