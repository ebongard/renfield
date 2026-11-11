/**
 * Service-worker entry for Web Push (#79). Bundled separately into
 * `push-sw-<hash>.js` and loaded by the generated Workbox worker through
 * `importScripts` (see `vite.config.ts`). Only wiring lives here; the
 * behaviour is in `pushHandlers.ts`, where it is tested.
 *
 * With the feature off no browser holds a subscription, so neither listener
 * ever fires.
 */
import { handleNotificationClick, handlePush, type WindowClientLike } from './pushHandlers';

interface ExtendableEventLike {
  waitUntil: (work: Promise<unknown>) => void;
}

interface PushEventLike extends ExtendableEventLike {
  data: { text: () => string } | null;
}

interface NotificationClickEventLike extends ExtendableEventLike {
  notification: { close: () => void; data: unknown };
}

/** The slice of `ServiceWorkerGlobalScope` used here. The project compiles
 *  against the DOM lib, where `self` is a `Window`; the worker lib cannot be
 *  loaded next to it. */
interface PushWorkerScope {
  location: { origin: string };
  navigator: { languages?: readonly string[] };
  registration: {
    showNotification: (title: string, options: object) => Promise<void>;
  };
  clients: {
    matchAll: (options: {
      type: 'window';
      includeUncontrolled: boolean;
    }) => Promise<readonly WindowClientLike[]>;
    openWindow: (url: string) => Promise<unknown>;
  };
  addEventListener(type: 'push', listener: (event: PushEventLike) => void): void;
  addEventListener(
    type: 'notificationclick',
    listener: (event: NotificationClickEventLike) => void,
  ): void;
}

const worker = self as unknown as PushWorkerScope;

worker.addEventListener('push', (event) => {
  event.waitUntil(
    handlePush(event.data ? event.data.text() : null, {
      showNotification: (title, options) => worker.registration.showNotification(title, options),
      languages: worker.navigator.languages,
    }),
  );
});

worker.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    handleNotificationClick(event.notification.data, {
      origin: worker.location.origin,
      matchWindows: () => worker.clients.matchAll({ type: 'window', includeUncontrolled: true }),
      openWindow: (url) => worker.clients.openWindow(url),
    }),
  );
});
