/**
 * Web Push (#79) -- what the service worker does with a push and with a click.
 *
 * Plain functions over small structural interfaces, so they run under a test
 * without a service-worker global. `push-sw.ts` is the only file that touches
 * `self`; it is bundled on its own (see `vite.config.ts`) and pulled into the
 * generated Workbox worker through `importScripts`.
 *
 * The push carries NO content: an id and an urgency. The line shown here is a
 * generic one from the locale files, and the click hands the id to the app,
 * which fetches the notification from the instance over its authenticated
 * API. Nothing readable ever passes through the push service.
 *
 * The named JSON imports keep the bundle to the `push` section of each locale.
 */
import { push as de } from '../i18n/locales/de.json';
import { push as en } from '../i18n/locales/en.json';
import { push as it } from '../i18n/locales/it.json';
import { OPEN_NOTIFICATION_MESSAGE, OPEN_NOTIFICATION_PARAM } from './pushProtocol';

export type PushUrgency = 'critical' | 'info' | 'low';
export type PushLanguage = 'de' | 'en' | 'it';

export interface PushPayload {
  notificationId: number | null;
  urgency: PushUrgency;
}

const GENERIC: Record<PushLanguage, { title: string; body: string; bodyCritical: string }> = {
  de: de.generic,
  en: en.generic,
  it: it.generic,
};

const URGENCIES: readonly PushUrgency[] = ['critical', 'info', 'low'];

/**
 * Read the payload, tolerating anything. A push MUST end in a visible
 * notification (browsers revoke a subscription that pushes silently), so a
 * payload this code cannot read still yields the generic line.
 */
export function parsePushPayload(raw: string | null | undefined): PushPayload {
  const fallback: PushPayload = { notificationId: null, urgency: 'info' };
  if (!raw) return fallback;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return fallback;
  }
  if (typeof parsed !== 'object' || parsed === null) return fallback;
  const record = parsed as Record<string, unknown>;
  const id = record.notification_id;
  const urgency = record.urgency;
  return {
    notificationId: typeof id === 'number' && Number.isInteger(id) && id > 0 ? id : null,
    urgency: URGENCIES.find((u) => u === urgency) ?? 'info',
  };
}

/** First supported browser language; German otherwise (the app's fallback). */
export function pickLanguage(languages: readonly string[] | undefined): PushLanguage {
  for (const language of languages ?? []) {
    const base = language.toLowerCase().split('-')[0];
    if (base === 'de' || base === 'en' || base === 'it') return base;
  }
  return 'de';
}

/** Where a click leads: the app, with the id for the follow-up fetch. */
export function targetUrl(payload: PushPayload): string {
  return payload.notificationId === null
    ? '/'
    : `/?${OPEN_NOTIFICATION_PARAM}=${payload.notificationId}`;
}

export interface ShownNotification {
  title: string;
  options: {
    body: string;
    tag: string;
    icon: string;
    badge: string;
    requireInteraction: boolean;
    data: { notificationId: number | null; url: string };
  };
}

export function notificationFor(payload: PushPayload, language: PushLanguage): ShownNotification {
  const text = GENERIC[language];
  const critical = payload.urgency === 'critical';
  return {
    title: text.title,
    options: {
      body: critical ? text.bodyCritical : text.body,
      // One entry per notification; a re-sent push replaces instead of stacking.
      tag:
        payload.notificationId === null
          ? 'renfield-notification'
          : `renfield-notification-${payload.notificationId}`,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      requireInteraction: critical,
      data: { notificationId: payload.notificationId, url: targetUrl(payload) },
    },
  };
}

export interface PushDeps {
  showNotification: (title: string, options: ShownNotification['options']) => Promise<void>;
  languages: readonly string[] | undefined;
}

export async function handlePush(raw: string | null | undefined, deps: PushDeps): Promise<void> {
  const shown = notificationFor(parsePushPayload(raw), pickLanguage(deps.languages));
  await deps.showNotification(shown.title, shown.options);
}

export interface WindowClientLike {
  url: string;
  focus: () => Promise<unknown>;
  postMessage: (message: { type: string; notificationId: number | null }) => void;
}

export interface ClickDeps {
  origin: string;
  matchWindows: () => Promise<readonly WindowClientLike[]>;
  openWindow: (url: string) => Promise<unknown>;
}

function isAppWindow(client: WindowClientLike, origin: string): boolean {
  try {
    const url = new URL(client.url);
    // The wall display is not somebody's inbox.
    return url.origin === origin && !url.pathname.startsWith('/kiosk');
  } catch {
    return false;
  }
}

/**
 * Bring the app forward. An open tab is focused and told the id; otherwise a
 * new window opens on the URL that carries it. `data` is whatever was stored
 * on the notification -- read defensively, it crosses a worker restart.
 */
export async function handleNotificationClick(data: unknown, deps: ClickDeps): Promise<void> {
  const record = typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {};
  const id = record.notificationId;
  const payload: PushPayload = {
    notificationId: typeof id === 'number' && Number.isInteger(id) && id > 0 ? id : null,
    urgency: 'info',
  };

  const open = (await deps.matchWindows()).find((client) => isAppWindow(client, deps.origin));
  if (open) {
    await open.focus();
    open.postMessage({ type: OPEN_NOTIFICATION_MESSAGE, notificationId: payload.notificationId });
    return;
  }
  await deps.openWindow(targetUrl(payload));
}
