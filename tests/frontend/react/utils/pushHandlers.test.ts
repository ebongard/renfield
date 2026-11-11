/**
 * Web Push (#79) -- the service-worker handlers, exercised as plain functions.
 *
 * What cannot be covered here: that a browser actually dispatches `push` and
 * `notificationclick` to the built worker. `push-sw.ts` is wiring only.
 */
import { describe, it, expect, vi } from 'vitest';

import {
  handleNotificationClick,
  handlePush,
  notificationFor,
  parsePushPayload,
  pickLanguage,
  targetUrl,
  type ClickDeps,
  type ShownNotification,
  type WindowClientLike,
} from '../../../../src/frontend/src/sw/pushHandlers';
import {
  OPEN_NOTIFICATION_MESSAGE,
  OPEN_NOTIFICATION_PARAM,
} from '../../../../src/frontend/src/sw/pushProtocol';
import de from '../../../../src/frontend/src/i18n/locales/de.json';
import en from '../../../../src/frontend/src/i18n/locales/en.json';
import it_ from '../../../../src/frontend/src/i18n/locales/it.json';

const SERVER_PAYLOAD = '{"v":1,"kind":"notification","notification_id":42,"urgency":"critical"}';

describe('parsePushPayload', () => {
  it('reads what the backend sends', () => {
    expect(parsePushPayload(SERVER_PAYLOAD)).toEqual({ notificationId: 42, urgency: 'critical' });
  });

  it.each([
    ['nothing', null],
    ['an empty body', ''],
    ['not JSON', 'hello'],
    ['a JSON scalar', '7'],
    ['a JSON null', 'null'],
    ['a wrong id type', '{"notification_id":"42","urgency":"low"}'],
    ['a negative id', '{"notification_id":-1}'],
    ['a fractional id', '{"notification_id":1.5}'],
  ])('falls back to the generic notification for %s', (_name, raw) => {
    expect(parsePushPayload(raw).notificationId).toBeNull();
  });

  it('never trusts an unknown urgency', () => {
    expect(parsePushPayload('{"notification_id":1,"urgency":"<b>now</b>"}')).toEqual({
      notificationId: 1,
      urgency: 'info',
    });
  });
});

describe('pickLanguage', () => {
  it.each([
    [['it-IT', 'en'], 'it'],
    [['en-GB'], 'en'],
    [['fr-FR', 'en-US'], 'en'],
    [['DE'], 'de'],
    [['fr', 'es'], 'de'],
    [[], 'de'],
    [undefined, 'de'],
  ] as const)('%j -> %s', (languages, expected) => {
    expect(pickLanguage(languages)).toBe(expected);
  });
});

describe('notificationFor', () => {
  it('shows the generic line from the locale file, never content', () => {
    const shown = notificationFor({ notificationId: 42, urgency: 'info' }, 'de');
    expect(shown.title).toBe(de.push.generic.title);
    expect(shown.options.body).toBe(de.push.generic.body);
    expect(shown.options.requireInteraction).toBe(false);
    expect(shown.options.tag).toBe('renfield-notification-42');
    expect(shown.options.data).toEqual({ notificationId: 42, url: '/?notification=42' });
  });

  it('marks an urgent one, in the browser language', () => {
    const shown = notificationFor({ notificationId: 42, urgency: 'critical' }, 'it');
    expect(shown.options.body).toBe(it_.push.generic.bodyCritical);
    expect(shown.options.requireInteraction).toBe(true);
  });

  it('has the generic lines in every language', () => {
    for (const locale of [de, en, it_]) {
      expect(locale.push.generic.title).toBeTruthy();
      expect(locale.push.generic.body).toBeTruthy();
      expect(locale.push.generic.bodyCritical).toBeTruthy();
    }
    expect(notificationFor({ notificationId: null, urgency: 'info' }, 'en').options.body).toBe(
      en.push.generic.body,
    );
  });

  it('without an id the click still leads into the app', () => {
    const payload = { notificationId: null, urgency: 'info' } as const;
    expect(targetUrl(payload)).toBe('/');
    expect(notificationFor(payload, 'de').options.tag).toBe('renfield-notification');
  });

  it('the URL carries the id under the agreed parameter', () => {
    expect(targetUrl({ notificationId: 7, urgency: 'low' })).toBe(`/?${OPEN_NOTIFICATION_PARAM}=7`);
  });
});

describe('handlePush', () => {
  it('always ends in a visible notification', async () => {
    const showNotification = vi.fn<
      (title: string, options: ShownNotification['options']) => Promise<void>
    >(async () => undefined);
    await handlePush(SERVER_PAYLOAD, { showNotification, languages: ['en-US'] });
    expect(showNotification).toHaveBeenCalledTimes(1);
    const [title, options] = showNotification.mock.calls[0];
    expect(title).toBe('Renfield');
    expect(options.body).toBe(en.push.generic.bodyCritical);
  });

  it('even for a payload it cannot read (a silent push costs the subscription)', async () => {
    const showNotification = vi.fn<
      (title: string, options: ShownNotification['options']) => Promise<void>
    >(async () => undefined);
    await handlePush('garbage', { showNotification, languages: undefined });
    expect(showNotification).toHaveBeenCalledTimes(1);
    expect(showNotification.mock.calls[0][1].body).toBe(de.push.generic.body);
  });
});

function clickDeps(windows: WindowClientLike[]): ClickDeps & {
  openWindow: ReturnType<typeof vi.fn<(url: string) => Promise<unknown>>>;
} {
  return {
    origin: 'https://renfield.example',
    matchWindows: async () => windows,
    openWindow: vi.fn<(url: string) => Promise<unknown>>(async () => null),
  };
}

function fakeWindow(url: string) {
  return {
    url,
    focus: vi.fn<() => Promise<unknown>>(async () => null),
    postMessage: vi.fn<(message: { type: string; notificationId: number | null }) => void>(),
  };
}

describe('handleNotificationClick', () => {
  it('opens the app on the notification when no tab is open', async () => {
    const deps = clickDeps([]);
    await handleNotificationClick({ notificationId: 42, url: '/?notification=42' }, deps);
    expect(deps.openWindow).toHaveBeenCalledWith('/?notification=42');
  });

  it('focuses an open tab and hands it the id instead of opening a second one', async () => {
    const tab = fakeWindow('https://renfield.example/wissen/dokumente');
    const deps = clickDeps([tab]);
    await handleNotificationClick({ notificationId: 42 }, deps);
    expect(tab.focus).toHaveBeenCalledTimes(1);
    expect(tab.postMessage).toHaveBeenCalledWith({
      type: OPEN_NOTIFICATION_MESSAGE,
      notificationId: 42,
    });
    expect(deps.openWindow).not.toHaveBeenCalled();
  });

  it('ignores the wall display and windows of other origins', async () => {
    const kiosk = fakeWindow('https://renfield.example/kiosk');
    const foreign = fakeWindow('https://elsewhere.example/');
    const broken = fakeWindow('not a url');
    const deps = clickDeps([kiosk, foreign, broken]);
    await handleNotificationClick({ notificationId: 42 }, deps);
    expect(kiosk.focus).not.toHaveBeenCalled();
    expect(foreign.postMessage).not.toHaveBeenCalled();
    expect(deps.openWindow).toHaveBeenCalledWith('/?notification=42');
  });

  it.each([[undefined], [null], ['text'], [{ notificationId: 'x' }], [{ url: 'https://evil.example/' }]])(
    'never follows stored data it cannot read (%j) -- it opens the app root',
    async (data) => {
      const deps = clickDeps([]);
      await handleNotificationClick(data, deps);
      expect(deps.openWindow).toHaveBeenCalledWith('/');
    },
  );
});
