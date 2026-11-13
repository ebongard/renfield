/**
 * Web Push (#79) -- the per-device switch on "Mein Konto".
 *
 * The three things this file guards hardest:
 *  - flag off => nothing: no card, no request to /api/push/*, no look at the
 *    browser's push state;
 *  - the permission prompt is raised by the click and by nothing else;
 *  - "on" means the browser AND the server agree -- one of them alone is "off".
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithProviders } from '../test-utils';
import {
  installPushBrowser,
  uninstallPushBrowser,
  TEST_ENDPOINT,
  TEST_KEYS,
} from '../pushBrowserMock';
import PushNotificationsCard from '../../../../src/frontend/src/components/PushNotificationsCard';
import { applicationServerKeyBytes } from '../../../../src/frontend/src/utils/webPush';
import { notePushSessionUser } from '../../../../src/frontend/src/api/resources/push';

// "AQID" is base64url for the bytes 1, 2, 3.
const PUBLIC_KEY = 'AQID';
const KEY_BYTES = new Uint8Array([1, 2, 3]);

interface Api {
  calls: string[];
  subscribed: unknown[];
  removed: unknown[];
  rows: Array<{ id: number; endpoint: string; device_label: string | null; created_at: string }>;
}

function row(endpoint: string, id = 1) {
  return { id, endpoint, device_label: 'Firefox', created_at: '2026-11-11T10:00:00' };
}

function mockApi(
  options: { flag?: boolean; rows?: Api['rows']; subscribeStatus?: number; subscribeDetail?: string } = {},
): Api {
  const api: Api = { calls: [], subscribed: [], removed: [], rows: options.rows ?? [] };
  server.use(
    http.get(`${BASE_URL}/api/config/features`, () =>
      HttpResponse.json({ push_notifications_enabled: options.flag ?? true }),
    ),
    http.get(`${BASE_URL}/api/push/vapid-key`, () => {
      api.calls.push('GET vapid-key');
      return HttpResponse.json({ public_key: PUBLIC_KEY });
    }),
    http.get(`${BASE_URL}/api/push/subscriptions`, () => {
      api.calls.push('GET subscriptions');
      return HttpResponse.json({ subscriptions: api.rows });
    }),
    http.post(`${BASE_URL}/api/push/subscribe`, async ({ request }) => {
      api.calls.push('POST subscribe');
      const body = (await request.json()) as { endpoint: string };
      api.subscribed.push(body);
      if (options.subscribeStatus) {
        return HttpResponse.json(
          { detail: options.subscribeDetail },
          { status: options.subscribeStatus },
        );
      }
      api.rows = [...api.rows, row(body.endpoint, 9)];
      return HttpResponse.json(row(body.endpoint, 9), { status: 201 });
    }),
    http.delete(`${BASE_URL}/api/push/subscribe`, async ({ request }) => {
      api.calls.push('DELETE subscribe');
      const body = (await request.json()) as { endpoint: string };
      api.removed.push(body);
      api.rows = api.rows.filter((r) => r.endpoint !== body.endpoint);
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return api;
}

const ENABLE = 'Auf diesem Gerät einschalten';
const DISABLE = 'Auf diesem Gerät ausschalten';

describe('PushNotificationsCard', () => {
  beforeEach(() => server.resetHandlers());
  afterEach(() => uninstallPushBrowser());

  it('flag off: no card, no push request, the browser is not even asked', async () => {
    const browser = installPushBrowser({ permission: 'granted', subscribedWithKey: KEY_BYTES });
    const api = mockApi({ flag: false });
    const { container } = renderWithProviders(<PushNotificationsCard />);

    // Give the feature query time to answer; the card must stay absent.
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(container).toBeEmptyDOMElement();
    expect(api.calls).toEqual([]);
    expect(browser.getRegistration).not.toHaveBeenCalled();
    expect(browser.requestPermission).not.toHaveBeenCalled();
  });

  it('a browser without the Push API says so and offers no button', async () => {
    // Nothing installed: jsdom has no service worker, PushManager or Notification.
    const api = mockApi();
    renderWithProviders(<PushNotificationsCard />);
    expect(
      await screen.findByText('Dieser Browser unterstützt keine Push-Benachrichtigungen.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(api.calls).toEqual([]);
  });

  it('iPhone/iPad in a browser tab: explains that the installed app is needed', async () => {
    const original = navigator.userAgent;
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1',
      configurable: true,
    });
    try {
      mockApi();
      renderWithProviders(<PushNotificationsCard />);
      expect(await screen.findByText(/Zum Home-Bildschirm/)).toBeInTheDocument();
      expect(screen.queryByRole('button')).not.toBeInTheDocument();
    } finally {
      Object.defineProperty(navigator, 'userAgent', { value: original, configurable: true });
    }
  });

  it('permission blocked in the browser: says so, the switch is disabled', async () => {
    const browser = installPushBrowser({ permission: 'denied' });
    mockApi();
    renderWithProviders(<PushNotificationsCard />);
    expect(await screen.findByText(/Im Browser blockiert/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: ENABLE })).toBeDisabled());
    expect(browser.requestPermission).not.toHaveBeenCalled();
  });

  it('never prompts on load -- only the click does', async () => {
    const browser = installPushBrowser({ permission: 'default' });
    const api = mockApi();
    renderWithProviders(<PushNotificationsCard />);

    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByText('Aus auf diesem Gerät')).toBeInTheDocument();
    expect(browser.requestPermission).not.toHaveBeenCalled();
    expect(browser.subscribe).not.toHaveBeenCalled();
    expect(api.calls).toEqual(['GET subscriptions']);

    await userEvent.click(button);
    await waitFor(() => expect(browser.requestPermission).toHaveBeenCalledTimes(1));
  });

  it('switching on: prompt, subscribe with the instance key, bind on the server', async () => {
    const browser = installPushBrowser({ permission: 'default', promptAnswer: 'granted' });
    const api = mockApi();
    // What the auth context does when it learns who is logged in.
    localStorage.removeItem('renfield_push_owner');
    notePushSessionUser(42);
    renderWithProviders(<PushNotificationsCard />);

    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    expect(localStorage.getItem('renfield_push_owner')).toBeNull();
    await userEvent.click(button);

    expect(await screen.findByText('Ein — dieses Gerät wird benachrichtigt')).toBeInTheDocument();
    // The browser notes whose subscription this is, so that another account
    // logging in here later can be told apart.
    expect(localStorage.getItem('renfield_push_owner')).toBe('42');
    expect(browser.subscribe).toHaveBeenCalledTimes(1);
    const init = browser.subscribe.mock.calls[0][0];
    expect(init.userVisibleOnly).toBe(true);
    expect(Array.from(init.applicationServerKey as Uint8Array)).toEqual([1, 2, 3]);
    // Exactly the endpoint and the keys -- no user id travels with it.
    expect(api.subscribed).toEqual([{ endpoint: TEST_ENDPOINT, keys: TEST_KEYS }]);
    expect(api.calls.slice(0, 3)).toEqual(['GET subscriptions', 'GET vapid-key', 'POST subscribe']);
    expect(screen.getByRole('button', { name: DISABLE })).toBeInTheDocument();
    expect(screen.getByText('1 Gerät ist für Ihr Konto angemeldet.')).toBeInTheDocument();
  });

  it.each(['denied', 'default'] as const)(
    'prompt answered "%s": nothing is subscribed and nothing is sent',
    async (answer) => {
      const browser = installPushBrowser({ permission: 'default', promptAnswer: answer });
      const api = mockApi();
      renderWithProviders(<PushNotificationsCard />);

      const button = await screen.findByRole('button', { name: ENABLE });
      await waitFor(() => expect(button).toBeEnabled());
      await userEvent.click(button);

      await waitFor(() => expect(browser.requestPermission).toHaveBeenCalledTimes(1));
      expect(browser.subscribe).not.toHaveBeenCalled();
      expect(api.calls).toEqual(['GET subscriptions']);
      if (answer === 'denied') {
        expect(await screen.findByText(/Im Browser blockiert/)).toBeInTheDocument();
      }
    },
  );

  it('switching off: the server row first, then the browser subscription', async () => {
    const browser = installPushBrowser({ permission: 'granted', subscribedWithKey: KEY_BYTES });
    const api = mockApi({ rows: [row(TEST_ENDPOINT), row('https://fcm.googleapis.com/other', 2)] });
    renderWithProviders(<PushNotificationsCard />);

    expect(await screen.findByText('2 Geräte sind für Ihr Konto angemeldet.')).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: DISABLE }));

    expect(await screen.findByText('Aus auf diesem Gerät')).toBeInTheDocument();
    expect(api.removed).toEqual([{ endpoint: TEST_ENDPOINT }]);
    expect(browser.unsubscribe).toHaveBeenCalledTimes(1);
    expect(browser.subscription).toBeNull();
    expect(browser.requestPermission).not.toHaveBeenCalled();
  });

  it('a browser subscription the server does not list counts as off', async () => {
    // Expired and removed after a 410, or taken over by another account.
    installPushBrowser({ permission: 'granted', subscribedWithKey: KEY_BYTES });
    mockApi({ rows: [row('https://fcm.googleapis.com/someone-elses-device', 2)] });
    renderWithProviders(<PushNotificationsCard />);

    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByText('Aus auf diesem Gerät')).toBeInTheDocument();
  });

  it('permission already granted: switching on does not prompt again', async () => {
    const browser = installPushBrowser({ permission: 'granted' });
    mockApi();
    renderWithProviders(<PushNotificationsCard />);
    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(await screen.findByText('Ein — dieses Gerät wird benachrichtigt')).toBeInTheDocument();
    expect(browser.requestPermission).not.toHaveBeenCalled();
  });

  it('a refusal by the server is shown translated, and the browser subscription is taken back', async () => {
    const browser = installPushBrowser({ permission: 'granted' });
    mockApi({ subscribeStatus: 422, subscribeDetail: 'endpoint_not_allowed' });
    renderWithProviders(<PushNotificationsCard />);
    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    expect(
      await screen.findByText('Der Zustelldienst dieses Browsers ist auf dieser Instanz nicht zugelassen.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('endpoint_not_allowed')).not.toBeInTheDocument();
    expect(browser.unsubscribe).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Aus auf diesem Gerät')).toBeInTheDocument();
  });

  it('an unknown server error never shows raw detail text', async () => {
    installPushBrowser({ permission: 'granted' });
    mockApi({ subscribeStatus: 500, subscribeDetail: 'Traceback (most recent call last)' });
    renderWithProviders(<PushNotificationsCard />);
    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(
      await screen.findByText('Die Push-Benachrichtigungen ließen sich nicht umstellen.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument();
  });

  it('no service worker registered (dev server): a clear sentence instead of a hang', async () => {
    const browser = installPushBrowser({ permission: 'granted', registered: false });
    const api = mockApi();
    renderWithProviders(<PushNotificationsCard />);
    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(await screen.findByText(/noch nicht vollständig geladen/)).toBeInTheDocument();
    expect(browser.subscribe).not.toHaveBeenCalled();
    expect(api.subscribed).toEqual([]);
  });

  it('a subscription made with a replaced key is dropped before subscribing again', async () => {
    const browser = installPushBrowser({
      permission: 'granted',
      subscribedWithKey: new Uint8Array([9, 9, 9]),
    });
    mockApi();
    renderWithProviders(<PushNotificationsCard />);
    const button = await screen.findByRole('button', { name: ENABLE });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(await screen.findByText('Ein — dieses Gerät wird benachrichtigt')).toBeInTheDocument();
    expect(browser.unsubscribe).toHaveBeenCalledTimes(1);
    expect(browser.subscribe).toHaveBeenCalledTimes(1);
  });
});

describe('applicationServerKeyBytes', () => {
  it('decodes base64url without padding', () => {
    expect(Array.from(applicationServerKeyBytes(PUBLIC_KEY))).toEqual([1, 2, 3]);
    // "-_" are the two characters that differ from plain base64 (0xfb 0xff).
    expect(Array.from(applicationServerKeyBytes('-_8'))).toEqual([0xfb, 0xff]);
  });
});
