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
  forgetPushOwner,
  getDeviceSubscription,
  mayHoldPushSubscription,
  pushOwner,
  rememberPushOwner,
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
  if (sessionUserId !== null) rememberPushOwner(sessionUserId);
  return body.endpoint;
}

/** Who is logged in on this browser, as the auth context last learned it. */
let sessionUserId: number | null = null;

/**
 * Browser side only, no server call: for every way a session ends WITHOUT a
 * logout (expired or revoked token, failed refresh, another account taking
 * the browser over). There is no session left to remove the server row with;
 * once the browser has unsubscribed, the push service answers the next send
 * with 410 and the server drops the row by itself. Never rejects.
 */
export async function releasePushLocally(): Promise<void> {
  forgetPushOwner();
  try {
    const subscription = await getDeviceSubscription();
    await subscription?.unsubscribe();
  } catch {
    // Best effort: there is nobody to report this to.
  }
}

/**
 * The session on this browser is gone and nobody pressed "log out". Without
 * this the device stayed bound to the previous account, and the next person
 * on a shared tablet was shown that account's alerts. A no-op (one synchronous
 * check) wherever push was never switched on.
 */
export function releasePushOnSessionEnd(): void {
  sessionUserId = null;
  if (mayHoldPushSubscription()) void releasePushLocally();
}

/**
 * The app has just learned who is logged in. If this browser's subscription
 * was made by a DIFFERENT account (a login or SSO return that replaced the
 * session without ending it), it is released.
 */
export function notePushSessionUser(userId: number): void {
  sessionUserId = userId;
  if (!mayHoldPushSubscription()) return;
  const owner = pushOwner();
  if (owner !== null && owner !== userId) void releasePushLocally();
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
  forgetPushOwner();
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
