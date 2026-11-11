/**
 * Web Push (#79) -- `/api/push/*`. The routes exist only while the feature is
 * active on the instance (`push_notifications_enabled` in the feature list);
 * callers gate on that flag and never probe.
 *
 * Everything goes through `apiClient`, so both session models work unchanged:
 * the Bearer header from localStorage (household) and the HttpOnly cookie with
 * the `X-CSRF-Token` echo on POST/DELETE (xidra).
 */
import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import {
  getDeviceSubscription,
  subscribeDevice,
  subscriptionBody,
} from '../../utils/webPush';
import { useApiMutation, useApiQuery } from '../hooks';
import { keys } from '../keys';

export interface PushSubscriptionRow {
  id: number;
  endpoint: string;
  device_label: string | null;
  created_at: string;
}

async function fetchPushSubscriptions(): Promise<PushSubscriptionRow[]> {
  const response = await apiClient.get<{ subscriptions: PushSubscriptionRow[] }>(
    '/api/push/subscriptions',
  );
  return response.data.subscriptions ?? [];
}

async function fetchVapidKey(): Promise<string> {
  const response = await apiClient.get<{ public_key: string }>('/api/push/vapid-key');
  return response.data.public_key;
}

/** The caller's own subscriptions. Disabled => no request at all. */
export function usePushSubscriptions(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.push.subscriptions(),
      queryFn: fetchPushSubscriptions,
      enabled,
    },
    'push.failed',
  );
}

/** Subscribe THIS browser and bind it to the logged-in account. */
async function enablePushOnThisDevice(): Promise<string> {
  const subscription = await subscribeDevice(await fetchVapidKey());
  const body = subscriptionBody(subscription);
  try {
    await apiClient.post('/api/push/subscribe', body);
  } catch (error) {
    // The server did not take it: do not leave a subscription behind that
    // nobody sends to and the UI would have to explain.
    await subscription.unsubscribe().catch(() => undefined);
    throw error;
  }
  return body.endpoint;
}

/**
 * Remove this browser's subscription: the server row first (while the
 * endpoint is still known), then the browser's -- ALWAYS the browser's. Once
 * the browser has unsubscribed, the push service answers the next send with
 * 410 and the server drops the row by itself, so a failed server call delays
 * the cleanup but cannot leave this device being notified.
 */
export async function disablePushOnThisDevice(
  headers?: Record<string, string>,
): Promise<void> {
  const subscription = await getDeviceSubscription();
  if (!subscription) return;
  try {
    await apiClient.delete('/api/push/subscribe', {
      data: { endpoint: subscription.endpoint },
      headers,
    });
  } catch {
    // Self-healing, see above.
  }
  await subscription.unsubscribe();
}

/** How long a logout waits for the release before it goes ahead regardless. */
const LOGOUT_RELEASE_TIMEOUT_MS = 3000;

/**
 * Best effort, bounded, never rejects: a logout must not fail or hang because
 * a push service or the network is slow.
 */
export function releasePushOnLogout(headers?: Record<string, string>): Promise<void> {
  return Promise.race([
    disablePushOnThisDevice(headers).catch(() => undefined),
    new Promise<void>((resolve) => {
      setTimeout(resolve, LOGOUT_RELEASE_TIMEOUT_MS);
    }),
  ]);
}

export function useEnablePush() {
  const queryClient = useQueryClient();
  return useApiMutation<string, void>(
    {
      mutationFn: enablePushOnThisDevice,
      onSettled: () => queryClient.invalidateQueries({ queryKey: keys.push.all }),
    },
    'push.failed',
  );
}

export function useDisablePush() {
  const queryClient = useQueryClient();
  return useApiMutation<void, void>(
    {
      mutationFn: () => disablePushOnThisDevice(),
      onSettled: () => queryClient.invalidateQueries({ queryKey: keys.push.all }),
    },
    'push.failed',
  );
}
