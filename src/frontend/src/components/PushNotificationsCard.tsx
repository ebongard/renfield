/**
 * Web Push (#79) -- the per-device switch on "Mein Konto".
 *
 * Renders NOTHING unless the instance reports the feature as active
 * (`push_notifications_enabled`, which is true only with every server-side
 * precondition met). With it off there is no card, no request to
 * `/api/push/*` and no look at the browser's push state.
 *
 * The permission prompt is raised in exactly one place: the click on
 * "einschalten". Never on mount.
 *
 * The switch is per DEVICE, because a subscription is: "on" means this
 * browser holds one AND the server lists it for the logged-in account. A
 * browser subscription the server no longer knows (expired and removed, or
 * taken over by another account) counts as off.
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bell, BellOff, BellRing, Loader } from 'lucide-react';

import { useFeatureFlags } from '../api/resources/brain';
import { useDisablePush, useEnablePush, usePushSubscriptions } from '../api/resources/push';
import {
  detectPushSupport,
  getDeviceSubscription,
  pushPermission,
  requestPushPermission,
} from '../utils/webPush';
import Alert from './Alert';

/** Codes the server (or the browser helper) answers with; anything else gets
 *  the generic sentence -- raw detail text never reaches the page. */
const ERROR_CODES = ['endpoint_not_allowed', 'subscription_limit_reached', 'no_service_worker'] as const;
type ErrorCode = (typeof ERROR_CODES)[number];

function errorCode(err: unknown): ErrorCode | null {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const candidate = typeof detail === 'string' ? detail : err instanceof Error ? err.message : null;
  return ERROR_CODES.find((code) => code === candidate) ?? null;
}

export default function PushNotificationsCard() {
  const { t } = useTranslation();
  const { data: flags } = useFeatureFlags();
  const active = flags?.push_notifications_enabled === true;

  const support = useMemo(() => detectPushSupport(), []);
  const usable = active && support === 'supported';

  const subscriptions = usePushSubscriptions(usable);
  const enable = useEnablePush();
  const disable = useDisablePush();

  // `undefined` = not read yet. Nothing is concluded from an unknown answer.
  const [deviceEndpoint, setDeviceEndpoint] = useState<string | null | undefined>(undefined);
  const [permission, setPermission] = useState<NotificationPermission>(() =>
    support === 'supported' ? pushPermission() : 'default',
  );
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    if (!usable) return undefined;
    let cancelled = false;
    getDeviceSubscription()
      .then((subscription) => {
        if (!cancelled) setDeviceEndpoint(subscription?.endpoint ?? null);
      })
      .catch(() => {
        if (!cancelled) setDeviceEndpoint(null);
      });
    return () => {
      cancelled = true;
    };
  }, [usable]);

  if (!active) return null;

  const rows = subscriptions.data ?? [];
  const known = deviceEndpoint !== undefined && subscriptions.data !== undefined;
  const on = known && deviceEndpoint !== null && rows.some((row) => row.endpoint === deviceEndpoint);
  const denied = usable && permission === 'denied';
  const busy = enable.isPending || disable.isPending;

  const fail = (err: unknown) => {
    const code = errorCode(err);
    setError(code ? t(`push.errors.${code}`) : t('push.failed'));
  };

  const handleEnable = async () => {
    setError(null);
    setSuccess(null);
    // The prompt -- here, on the click, and nowhere else.
    const granted = permission === 'granted' ? permission : await requestPushPermission();
    setPermission(granted);
    if (granted !== 'granted') return;
    try {
      setDeviceEndpoint(await enable.mutateAsync());
      setSuccess(t('push.enabled'));
    } catch (err) {
      fail(err);
    }
  };

  const handleDisable = async () => {
    setError(null);
    setSuccess(null);
    try {
      await disable.mutateAsync();
      setDeviceEndpoint(null);
      setSuccess(t('push.disabled'));
    } catch (err) {
      fail(err);
    }
  };

  let StateIcon = Bell;
  let stateText = t('push.stateOff');
  let stateTone = 'text-gray-500 dark:text-gray-400';
  if (support === 'needs_install') {
    StateIcon = BellOff;
    stateText = t('push.stateNeedsInstall');
  } else if (support === 'unsupported') {
    StateIcon = BellOff;
    stateText = t('push.stateUnsupported');
  } else if (denied) {
    StateIcon = BellOff;
    stateText = t('push.stateDenied');
    stateTone = 'text-amber-600 dark:text-amber-400';
  } else if (!known && !subscriptions.isError) {
    stateText = t('push.stateLoading');
  } else if (on) {
    StateIcon = BellRing;
    stateText = t('push.stateOn');
    stateTone = 'text-accent-700 dark:text-accent-300';
  }

  return (
    <div className="card mt-4">
      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}
      {success && <Alert variant="success" onClose={() => setSuccess(null)}>{success}</Alert>}

      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <h2 className="flex items-center gap-2 text-lg font-medium text-gray-900 dark:text-white">
            <Bell className="w-5 h-5" aria-hidden="true" />
            {t('push.title')}
          </h2>
          <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">{t('push.explain')}</p>
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{t('push.contentNote')}</p>

          {/* Symbol AND text -- colour is never the only signal (DESIGN.md). */}
          <p
            className={`mt-3 flex items-center gap-2 text-sm font-medium ${stateTone}`}
            aria-live="polite"
          >
            <StateIcon className="w-4 h-4 shrink-0" aria-hidden="true" />
            {stateText}
          </p>

          {usable && rows.length > 0 && (
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400 tabular-nums">
              {t('push.deviceCount', { count: rows.length })}
            </p>
          )}
        </div>

        {usable && (
          <button
            type="button"
            onClick={on ? handleDisable : handleEnable}
            disabled={busy || !known || (!on && denied)}
            className={on ? 'btn-secondary min-h-11' : 'btn-primary min-h-11'}
          >
            {busy && (
              <Loader
                className="w-4 h-4 mr-2 animate-spin motion-reduce:animate-none"
                aria-hidden="true"
              />
            )}
            {on ? t('push.disable') : t('push.enable')}
          </button>
        )}
      </div>
    </div>
  );
}
