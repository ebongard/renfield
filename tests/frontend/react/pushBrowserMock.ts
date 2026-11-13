/**
 * A stand-in for the three browser APIs Web Push needs (#79): the service
 * worker registration with its `pushManager`, `window.PushManager` and
 * `Notification`. jsdom ships none of them, which is also exactly the
 * "unsupported browser" state -- so a test that installs nothing tests that.
 */
import { vi } from 'vitest';

export const TEST_ENDPOINT = 'https://fcm.googleapis.com/fcm/send/this-device';
export const TEST_KEYS = { p256dh: 'device-public-key', auth: 'device-auth-secret' };

export interface PushBrowser {
  /** The browser's current subscription, as the fake `pushManager` reports it. */
  subscription: PushSubscription | null;
  permission: NotificationPermission;
  requestPermission: ReturnType<typeof vi.fn<() => Promise<NotificationPermission>>>;
  getRegistration: ReturnType<typeof vi.fn<() => Promise<ServiceWorkerRegistration | undefined>>>;
  subscribe: ReturnType<typeof vi.fn<(options: PushSubscriptionOptionsInit) => Promise<PushSubscription>>>;
  unsubscribe: ReturnType<typeof vi.fn<() => Promise<boolean>>>;
  /** Deliver a message "from the service worker" to the page. */
  postFromWorker: (data: unknown) => void;
}

export interface PushBrowserOptions {
  permission?: NotificationPermission;
  /** What the prompt answers when the page asks. */
  promptAnswer?: NotificationPermission;
  /** Start with a subscription made with this application server key. */
  subscribedWithKey?: Uint8Array | null;
  /** `false` = no service worker registered (the dev server). */
  registered?: boolean;
}

function defineGlobal(target: object, name: string, value: unknown): void {
  Object.defineProperty(target, name, { value, configurable: true, writable: true });
}

export function installPushBrowser(options: PushBrowserOptions = {}): PushBrowser {
  const messageListeners = new Set<(event: MessageEvent) => void>();

  const browser: PushBrowser = {
    subscription: null,
    permission: options.permission ?? 'default',
    requestPermission: vi.fn(async () => {
      browser.permission = options.promptAnswer ?? 'granted';
      return browser.permission;
    }),
    getRegistration: vi.fn(async () =>
      options.registered === false ? undefined : (registration as unknown as ServiceWorkerRegistration),
    ),
    subscribe: vi.fn(async (init: PushSubscriptionOptionsInit) => {
      browser.subscription = makeSubscription(init.applicationServerKey as Uint8Array);
      return browser.subscription;
    }),
    unsubscribe: vi.fn(async () => {
      browser.subscription = null;
      return true;
    }),
    postFromWorker: (data) => {
      messageListeners.forEach((listener) => listener(new MessageEvent('message', { data })));
    },
  };

  function makeSubscription(key: Uint8Array): PushSubscription {
    const fake = {
      endpoint: TEST_ENDPOINT,
      options: { applicationServerKey: key.buffer, userVisibleOnly: true },
      toJSON: () => ({ endpoint: TEST_ENDPOINT, keys: TEST_KEYS }),
      unsubscribe: browser.unsubscribe,
    };
    return fake as unknown as PushSubscription;
  }

  const registration = {
    pushManager: {
      getSubscription: async () => browser.subscription,
      subscribe: browser.subscribe,
    },
  };

  if (options.subscribedWithKey) {
    browser.subscription = makeSubscription(options.subscribedWithKey);
  }

  defineGlobal(navigator, 'serviceWorker', {
    getRegistration: browser.getRegistration,
    addEventListener: (_type: string, listener: (event: MessageEvent) => void) => {
      messageListeners.add(listener);
    },
    removeEventListener: (_type: string, listener: (event: MessageEvent) => void) => {
      messageListeners.delete(listener);
    },
  });
  defineGlobal(window, 'PushManager', class PushManager {});
  defineGlobal(window, 'Notification', {
    get permission() {
      return browser.permission;
    },
    requestPermission: browser.requestPermission,
  });

  return browser;
}

/** Back to jsdom's native state: no push support at all. */
export function uninstallPushBrowser(): void {
  Reflect.deleteProperty(navigator, 'serviceWorker');
  Reflect.deleteProperty(window, 'PushManager');
  Reflect.deleteProperty(window, 'Notification');
}
