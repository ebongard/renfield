/**
 * Web Push (#79) -- the browser side of one device's subscription.
 *
 * Nothing here runs on page load: every function is called from an explicit
 * user action (the toggle on "Mein Konto") or reads state without changing it.
 * `Notification.requestPermission()` in particular is only ever reached through
 * `requestPushPermission`.
 */

export type PushSupport =
  /** Service worker, Push API and Notification API are all present. */
  | 'supported'
  /** iOS/iPadOS in a browser tab: Web Push exists only for an installed app. */
  | 'needs_install'
  | 'unsupported';

export interface PushSubscriptionBody {
  endpoint: string;
  keys: { p256dh: string; auth: string };
}

function isAppleMobile(): boolean {
  const ua = navigator.userAgent || '';
  // iPadOS reports itself as a Mac; the touch points give it away.
  return /iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
}

export function detectPushSupport(): PushSupport {
  const hasApis =
    typeof navigator !== 'undefined'
    && 'serviceWorker' in navigator
    && typeof window !== 'undefined'
    && 'PushManager' in window
    && 'Notification' in window;
  if (hasApis) return 'supported';
  return typeof navigator !== 'undefined' && isAppleMobile() ? 'needs_install' : 'unsupported';
}

export function pushPermission(): NotificationPermission {
  return 'Notification' in window ? Notification.permission : 'denied';
}

/** The ONE place the permission prompt is raised. Call from a click handler. */
export async function requestPushPermission(): Promise<NotificationPermission> {
  return Notification.requestPermission();
}

/** VAPID keys travel base64url-encoded; `subscribe` wants the bytes. */
export function applicationServerKeyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = base64url.replace(/-/g, '+').replace(/_/g, '/')
    + '='.repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function sameBytes(a: ArrayBuffer | null | undefined, b: Uint8Array): boolean {
  if (!a) return false;
  const left = new Uint8Array(a);
  return left.length === b.length && left.every((value, index) => value === b[index]);
}

/**
 * The registration, or `null` when no worker is registered (the dev server
 * runs without one). Deliberately not `serviceWorker.ready`: that promise
 * never settles without a worker and would hang the toggle.
 */
async function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator)) return null;
  return (await navigator.serviceWorker.getRegistration()) ?? null;
}

/** This browser's current subscription. Reads only -- never subscribes. */
export async function getDeviceSubscription(): Promise<PushSubscription | null> {
  const reg = await registration();
  if (!reg || !('pushManager' in reg)) return null;
  return reg.pushManager.getSubscription();
}

export function subscriptionBody(subscription: PushSubscription): PushSubscriptionBody {
  const json = subscription.toJSON();
  const p256dh = json.keys?.p256dh;
  const auth = json.keys?.auth;
  if (!json.endpoint || !p256dh || !auth) throw new Error('incomplete_subscription');
  return { endpoint: json.endpoint, keys: { p256dh, auth } };
}

/**
 * Subscribe this browser with the instance's public key. A subscription made
 * with a DIFFERENT key (the operator replaced the pair) is dropped first --
 * the push service would refuse every send for it.
 */
export async function subscribeDevice(publicKey: string): Promise<PushSubscription> {
  const reg = await registration();
  if (!reg || !('pushManager' in reg)) throw new Error('no_service_worker');
  const key = applicationServerKeyBytes(publicKey);
  const existing = await reg.pushManager.getSubscription();
  if (existing) {
    if (sameBytes(existing.options.applicationServerKey, key)) return existing;
    await existing.unsubscribe();
  }
  return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
}

/**
 * Which account switched push on in THIS browser. A per-browser note, written
 * only when push is switched on: it lets the app notice that somebody else is
 * logged in now. Storage can be unavailable (private window) -- then there is
 * simply no note.
 */
const PUSH_OWNER_KEY = 'renfield_push_owner';

export function pushOwner(): number | null {
  try {
    const raw = localStorage.getItem(PUSH_OWNER_KEY);
    const id = raw === null ? Number.NaN : Number(raw);
    return Number.isInteger(id) ? id : null;
  } catch {
    return null;
  }
}

export function rememberPushOwner(userId: number): void {
  try {
    localStorage.setItem(PUSH_OWNER_KEY, String(userId));
  } catch {
    // No note, see above.
  }
}

export function forgetPushOwner(): void {
  try {
    localStorage.removeItem(PUSH_OWNER_KEY);
  } catch {
    // Nothing to forget.
  }
}

/** True when the browser MAY hold a subscription worth releasing -- a cheap,
 *  synchronous check, so a logout without push stays exactly as it was. */
export function mayHoldPushSubscription(): boolean {
  return detectPushSupport() === 'supported' && pushPermission() === 'granted';
}
