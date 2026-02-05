/**
 * useNotifications Hook
 *
 * Listens for notification messages from the Device WebSocket
 * and manages a local queue for toast display.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { useDevice } from '../context/DeviceContext';

export interface NotificationData {
  notification_id: number;
  title: string;
  message: string;
  urgency: 'critical' | 'info' | 'low';
  source: string;
  room: string | null;
  tts_handled: boolean;
  created_at: string;
}

const MAX_VISIBLE = 3;
const AUTO_DISMISS_MS: Record<string, number> = {
  critical: 0,   // persistent
  info: 10000,    // 10s
  low: 10000,     // 10s
};

export function useNotifications() {
  const [notifications, setNotifications] = useState<NotificationData[]>([]);
  const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  const { sendNotificationAck } = useDevice();

  // Listen for notification events from Device WS
  useEffect(() => {
    const handler = (event: Event) => {
      const data = (event as CustomEvent).detail as NotificationData;
      setNotifications(prev => {
        const updated = [data, ...prev];
        return updated;
      });

      // Auto-dismiss for non-critical
      const dismissMs = AUTO_DISMISS_MS[data.urgency] || AUTO_DISMISS_MS.info;
      if (dismissMs > 0) {
        const timer = setTimeout(() => {
          dismiss(data.notification_id);
        }, dismissMs);
        timersRef.current.set(data.notification_id, timer);
      }
    };

    window.addEventListener('renfield-notification', handler);
    return () => {
      window.removeEventListener('renfield-notification', handler);
      // Clear all timers
      timersRef.current.forEach(timer => clearTimeout(timer));
      timersRef.current.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const acknowledge = useCallback((notificationId: number) => {
    sendNotificationAck(notificationId, 'acknowledged');
    setNotifications(prev => prev.filter(n => n.notification_id !== notificationId));
    const timer = timersRef.current.get(notificationId);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(notificationId);
    }
  }, [sendNotificationAck]);

  const dismiss = useCallback((notificationId: number) => {
    sendNotificationAck(notificationId, 'dismissed');
    setNotifications(prev => prev.filter(n => n.notification_id !== notificationId));
    const timer = timersRef.current.get(notificationId);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(notificationId);
    }
  }, [sendNotificationAck]);

  const suppress = useCallback(async (notificationId: number) => {
    try {
      await fetch(`/api/notifications/${notificationId}/suppress`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
    } catch (e) {
      console.warn('Failed to suppress notification:', e);
    }
    // Remove from local state regardless
    setNotifications(prev => prev.filter(n => n.notification_id !== notificationId));
    const timer = timersRef.current.get(notificationId);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(notificationId);
    }
  }, []);

  return {
    notifications,
    visibleNotifications: notifications.slice(0, MAX_VISIBLE),
    queuedCount: Math.max(0, notifications.length - MAX_VISIBLE),
    acknowledge,
    dismiss,
    suppress,
  };
}

export default useNotifications;
