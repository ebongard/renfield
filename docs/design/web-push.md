# Web Push — a content-free wake-up for proactive notifications (#79)

**Status:** built, **dark by default** (`PUSH_NOTIFICATIONS_ENABLED=false`). Not rolled out on any instance.
No real push round trip has been made: the sender is tested against a stub of `pywebpush`, the worker
handlers as plain functions and once as the built bundle in a fake worker scope. The first real
delivery is a rollout step (see §7).

## 1. What it is

A proactive notification so far reaches a person only while Renfield is open: as a toast over the
device WebSocket, spoken in a room, or in the list behind `GET /api/notifications`. Web Push adds a
fourth door for a closed browser: the instance asks the browser vendor's push service to wake the
service worker, which shows a system notification.

Not built, because nothing needs it yet: notification categories (the backend has none — `event_type`
is free text chosen by the caller), per-category opt-in, a retry queue, delivery receipts, quiet hours.

## 2. Who gets a push — the same rule as in-app

Since #1244 one rule decides who may see a notification: `NotificationService.is_visible_to`
(SQL form `_visible_to_clause`). The push sender does not restate it; it calls it.

| | In-app list (`GET /api/notifications`, non-admin) | Web Push (`push_service.select_push_recipients`) |
|---|---|---|
| Rule | `_visible_to_clause(viewer_id)` | `is_visible_to(notification, subscription.user_id)` per row |
| Addressed to a user | that user only | that user's devices only |
| Unaddressed, `public` / `NULL` privacy | every logged-in user | every active user's devices |
| Unaddressed, `personal` / `confidential` / unknown | nobody | nobody |
| Admin / `notifications.manage` | sees everything (oversight) | **only what the row above gives anyone** |
| Deactivated account | cannot log in | skipped (`users.is_active`) |
| Role without `notifications.view` | lists public ones | nothing (cannot subscribe; skipped if it lost the permission later) |
| Auth off | unrestricted | feature inactive (precondition) |

`tests/backend/test_push_notifications.py::TestRecipients::test_push_reaches_exactly_whom_the_in_app_list_reaches`
runs both doors over eight notification shapes and five accounts on real Postgres and compares the sets.

Two deliberate differences, both on the narrow side:

- **Oversight is not delivery.** An admin can *list* a personal notification addressed to someone
  else; it is not pushed to the admin's phone.
- **The device WebSocket toast is a different axis** and is left untouched. It is spatial (a room's
  devices, presence-gated for non-public notifications), not personal. A push goes to the addressee's
  own browsers wherever they are, which is why it needs no presence gate — and why it carries no content.

The routes are gated on `notifications.view` (Familie and Admin hold it; Gast and the kiosk device
account do not). The notification list itself only requires a login, so a guest can see public
notifications in-app but cannot subscribe a device: push recipients ⊆ in-app recipients.

**The sender asks for the same permission again.** A role can change after a device was subscribed;
`select_push_recipients` skips a subscription whose owner no longer holds `notifications.view`
(`User.has_permission`, failing closed when the role cannot be read). The row stays — the role may
come back — and `DELETE /api/push/subscribe` is the one route that needs only a login, so such an
account can still take its device back. So the exact statement is: push recipients = in-app
recipients who hold `notifications.view`.

## 3. What leaves the house

A push travels through a third party (Google FCM, Mozilla, Apple, Microsoft WNS). The body is
end-to-end encrypted (RFC 8291) — the push service sees the endpoint, the size and the time, not the
text. The payload is minimal anyway:

```json
{"v": 1, "kind": "notification", "notification_id": 4711, "urgency": "info"}
```

No title, no message, no `event_type`, no room, no name. Reasons, in order of weight:

1. **The lock screen.** What a push shows, it shows on a locked phone lying on a table. A reminder
   used to be public to the whole household until #1244 closed that; a push with the text would
   reopen it one layer down.
2. **One rule for outbound content.** `/ws/user` already works this way: a content-free event, then
   a fetch the server filters again. A notification taken over by another account on the same
   browser, expired or dismissed is simply not shown after the click.
3. **Encryption is the standard's promise, not ours to verify.**

The service worker shows a generic line ("Neue Benachrichtigung" / "Neue dringende Benachrichtigung")
from the `push.generic` keys of the locale files, in the browser's language. A click opens or focuses
the app and hands over the id; `useOpenPushedNotification` fetches the caller's list and replays the
entry into the existing toast.

**Not taken:** the full text in the payload as a per-user opt-in. It is a real convenience and the
natural next step; it needs a stored preference, its own consent wording, and a decision whether
`personal` and `confidential` may ever be included. Owner decision.

## 4. Preconditions — one function

`services/push_preconditions.push_blocker()` is asked by the routes, the sender and
`/api/config/features`. Push is active only with: the flag, `AUTH_ENABLED` (a row belongs to the
logged-in user), a VAPID key pair whose halves match, and a contact address. Anything missing ⇒ the
feature is off, the routes answer the 404 of an absent path (before auth and validation), nothing is
sent, the UI shows no card, and startup logs exactly one WARNING naming the reason.

**VAPID keys come from the environment.** The issue asked for keys "auto-generated at first start";
that would mean the backend writing a secret to disk or into the database on its own, and every
replica racing to do so. `bin/generate-vapid-keys.py` prints a pair; the operator stores it as a
secret. **Not taken:** generate-and-persist in `system_settings` (encrypted with `SECRET_KEY`).

## 5. Storage and routes

`push_subscriptions` (migration `pc20261111_push_subs`): `user_id` (FK, `ON DELETE CASCADE`),
`endpoint` (unique — the endpoint *is* the device), `p256dh`, `auth`, `device_label` (the request's
User-Agent), `created_at`.

- `GET /api/push/vapid-key`, `GET /api/push/subscriptions`, `POST /api/push/subscribe`,
  `DELETE /api/push/subscribe`. The owner is always the session user; a `user_id` in the body is
  ignored. Listing and removing are scoped to the owner — for admins too.
- **A second account on the same browser takes the row over** (upsert on `endpoint`), so the device
  stops receiving the first account's pushes. **Logout releases the subscription** (server row, then
  browser) before the session is revoked. *Not taken:* keeping the device subscribed across logout —
  convenient, but the device would announce activity for an account that is not logged in on it.
- **A session that ends without a logout releases it too** — expired or revoked token, failed
  refresh, a token that is gone, `/auth/me` failing (all in `AuthContext`; there is no other place
  that ends a session client-side). No session is left for the server call, so the browser
  unsubscribes by itself (`releasePushOnSessionEnd`); the push service then answers the next send
  with 410 and the server drops the row. A login that *replaces* a session (SSO return, token
  overwrite) is caught by a per-browser note of who switched push on (`renfield_push_owner` in
  localStorage, written only when push is switched on): a different account in `/auth/me` releases
  the subscription. Until one of these runs — the app has to be opened once — a shared device can
  still show the generic line for the previous account; it never shows content.
- **Endpoint allowlist** (`PUSH_ALLOWED_ENDPOINT_HOSTS`): the URL comes from a browser and the
  backend POSTs to it; without a restriction any account could aim the backend at an internal
  address. The check is on the **raw text** and strict: `https://`, a lower-case DNS name, then the
  end or `/` — no userinfo or `@` anywhere, no port (not even `:443`), no backslash, whitespace,
  control character, `%` in the host, IP literal in any spelling, non-ASCII letter or fragment. An
  endpoint that is not already canonical is refused, never normalised, so the text that was checked
  is the text that is sent. On top, urllib3's parser (the one `requests` sends with) and the
  standard library must both name the checked host; a disagreement or a missing parser refuses.
  (The first version decided with `urlsplit` alone and let `https://192.168.1.1\.push.apple.com/`
  through, which `requests` sends to `192.168.1.1`.) Asked on subscribe, when selecting recipients,
  and once more inside the function that sends.
- **No redirects.** `requests` would follow a 307/308 and repeat the POST wherever it points; the
  send uses a session with `max_redirects = 0`, so a redirect is a failed send.
- At most 20 devices per account (each row is one outbound request per notification).
- The keys are stored in the clear: they are the browser's public key and a shared secret that are
  only usable together with the VAPID private key. *Not taken:* Fernet at rest.

## 6. Sending

`NotificationService._deliver` is the one place a notification goes out. It awaits the device
delivery as before and then calls `push_service.schedule_push`, which returns immediately: nothing
while the feature is inactive, otherwise a task. The task opens its own session, selects recipients,
and runs `pywebpush` (blocking, `requests`) in a worker thread under a timeout. 404/410 from the push
service removes the row; any other failure is logged without the endpoint or the response text and
dropped — there is no retry. Senders in the worker pods (document-worker and others) push as well,
which is a gain: the device WebSocket only exists in the API pod.

`pywebpush` is imported inside the sender. Without the package the module still imports and each
send fails softly.

## 7. Frontend

- **Service worker.** The worker stays generated by Workbox (`generateSW`). `vite.config.ts` bundles
  `src/sw/push-sw.ts` with esbuild into `push-sw-<hash>.js` and lists it in `workbox.importScripts`.
  The content hash matters: nginx serves `.js` as immutable for a year. `sw.js` therefore differs
  from before by one `importScripts` line even with the feature off — the flag is a runtime setting
  of the backend, the worker a build artefact; with no subscription the handlers never fire.
  *Not taken:* switching to `injectManifest` with a hand-written worker (it would re-implement
  precaching, the navigation fallback denylist and the update flow).
- **Toggle** on `/settings/account` (`PushNotificationsCard`), per device. The permission prompt is
  raised only by the click. States shown honestly: unsupported browser; iPhone/iPad outside the
  installed app (Web Push exists only for a Home-Screen PWA there, iOS 16.4+); blocked in the browser;
  off; on. "On" means browser and server agree.
- Works with both session models through `apiClient`: Bearer from localStorage (household) and the
  HttpOnly cookie with the `X-CSRF-Token` echo on POST/DELETE (xidra).

## 8. Rollout (per instance — both run this code)

0. **Before merging: the migration shares its parent with another open PR.** `pc20261111_push_subs`
   and the tombstone PR's `pc20261111_tombstone_idx` both have `down_revision =
   "pc20261110_routing_traces"`. Whichever merges SECOND must rebase its `down_revision` onto the
   first one's revision (and the assertion in its migration test) — not add a merge revision —
   otherwise Alembic has two heads, `upgrade head` fails and the deploy gate's image-head check
   breaks. If one is already deployed, query the LIVE head first. The order is the owner's call.
1. **Migration before the rollout** (`bin/deploy-production.sh … --migrate`). A new pod before the
   migration fails every query on `push_subscriptions`; with the flag off nothing queries it, but the
   deploy gate compares heads regardless.
2. `python bin/generate-vapid-keys.py` — **one pair per instance**. Private key, public key and
   `PUSH_VAPID_CLAIMS_EMAIL` go into the instance's Secret (`renfield-env-private`), never into the
   ConfigMap or git. xidra's values live in the private repo.
3. `PUSH_NOTIFICATIONS_ENABLED=true` in the instance's ConfigMap, committed; roll **all four**
   backend-image deployments (workers send too). The image must have been built after `pywebpush`
   entered `requirements.txt`; compare `pip show cryptography` old against new image (pywebpush needs ≥ 47).
4. Check the startup log for the absence of the WARNING, then `GET /api/config/features` →
   `push_notifications_enabled: true`.
5. First real round trip in a browser: switch on under "Mein Konto", fire a notification addressed to
   that account, close the tab, expect the system notification, click it. Chrome/Android and an
   installed iOS PWA behave differently enough to try both.
6. Egress: the backend and worker pods must reach the push-service hosts on 443.

Replacing a key pair invalidates every subscription: sends answer 403/404/410, the rows go on the
next push where the answer is 404/410, and each device has to be switched on again.

## 9. Open decisions for the owner

1. Full text in the payload as a per-user opt-in (§3).
2. Release the subscription on logout (taken) vs. keep it (§5).
3. Routes gated on `notifications.view` (taken) vs. any logged-in user, as the list is (§2).
4. Admin oversight is not pushed (taken) (§2).
5. Push requires `AUTH_ENABLED` (taken); an auth-off household would need a device-bound model.
6. The generic line follows the browser language, not the language chosen in the app (the worker
   cannot read localStorage). Storing the language with the subscription would fix it.
7. A `.claude/rules/web-push.md` and its row in the `CLAUDE.md` index were not added.
