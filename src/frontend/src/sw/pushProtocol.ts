/**
 * Web Push (#79) -- the two names the service worker and the app agree on.
 * Kept apart from `pushHandlers.ts` so the app does not import worker code.
 */

/** Posted to an open tab on a notification click; `useOpenPushedNotification` listens. */
export const OPEN_NOTIFICATION_MESSAGE = 'renfield:open-notification';

/** Query parameter carrying the id when the click has to open a new window. */
export const OPEN_NOTIFICATION_PARAM = 'notification';
