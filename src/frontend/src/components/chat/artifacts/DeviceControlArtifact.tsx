/**
 * DeviceControlArtifact — an INTERACTIVE device-control widget (Gen-UI item 10):
 * on/off toggles for lights & switches, a "run" button for scenes. Clicking a
 * control calls `onAction(entity_id, action)` which sends a `device_action` WS
 * frame; the backend gates on HA_CONTROL and re-validates the entity/action, so
 * the widget grants no control the user lacks via the agent. We optimistically
 * flip the toggle and reconcile to the re-read state the backend returns.
 *
 * No model HTML — typed JSON → React controls (the Lane-A invariant). `onAction`
 * is threaded as a prop (not pulled from ChatContext) so the artifact layer
 * stays decoupled and unit-testable; when absent the controls render disabled.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Lightbulb, Play, Loader } from 'lucide-react';
import type { DeviceControlData, DeviceControlDevice } from './artifactSchema';

export type DeviceActionFn = (
  entityId: string,
  action: string,
) => Promise<{ success: boolean; state?: string }>;

function isOn(state: string | undefined): boolean {
  return state === 'on';
}

export default function DeviceControlArtifact({
  data,
  onAction,
}: {
  data: DeviceControlData;
  onAction?: DeviceActionFn;
}) {
  const { t } = useTranslation();
  // Local display state seeded from props (entity_id → state) + in-flight set.
  const [states, setStates] = useState<Record<string, string>>(() =>
    Object.fromEntries(data.devices.map((d) => [d.entity_id, d.state])),
  );
  const [pending, setPending] = useState<Set<string>>(() => new Set());

  const setBusy = (id: string, busy: boolean) =>
    setPending((prev) => {
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });

  const handle = async (d: DeviceControlDevice) => {
    if (!onAction || pending.has(d.entity_id)) return;
    const scene = d.domain === 'scene';
    const action = scene ? 'activate' : 'toggle';
    const prev = states[d.entity_id];
    // Optimistic flip for toggles; scenes have no on/off state.
    if (!scene) setStates((s) => ({ ...s, [d.entity_id]: isOn(prev) ? 'off' : 'on' }));
    setBusy(d.entity_id, true);
    const res = await onAction(d.entity_id, action);
    setBusy(d.entity_id, false);
    if (!scene) {
      setStates((s) => ({
        ...s,
        [d.entity_id]: res.success ? (res.state ?? s[d.entity_id]) : prev,
      }));
    }
  };

  if (data.devices.length === 0) {
    return (
      <p className="text-sm italic text-accent-700 dark:text-accent-300">
        {t('chat.artifacts.deviceControl.empty')}
      </p>
    );
  }

  return (
    <div className="flex flex-col divide-y divide-gray-100 dark:divide-gray-700">
      {data.devices.map((d) => {
        const on = isOn(states[d.entity_id] ?? d.state);
        const busy = pending.has(d.entity_id);
        const scene = d.domain === 'scene';
        return (
          <div key={d.entity_id} className="flex items-center justify-between gap-3 py-1">
            <span className="flex min-w-0 items-center gap-2 text-sm text-gray-800 dark:text-gray-200">
              <Lightbulb
                className={`h-4 w-4 shrink-0 ${on && !scene ? 'text-amber-500' : 'text-gray-400 dark:text-gray-500'}`}
                aria-hidden="true"
              />
              <span className="truncate">{d.name}</span>
            </span>

            {scene ? (
              <button
                type="button"
                onClick={() => handle(d)}
                disabled={busy || !onAction}
                aria-label={t('chat.artifacts.deviceControl.run', { name: d.name })}
                className="inline-flex min-h-[44px] items-center gap-1.5 rounded-lg bg-accent-100 px-3 text-sm text-accent-800 hover:bg-accent-200 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 dark:bg-accent-900/40 dark:text-accent-200 dark:hover:bg-accent-900/60"
              >
                {busy ? (
                  <Loader className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                ) : (
                  <Play className="h-4 w-4" aria-hidden="true" />
                )}
                <span>{t('chat.artifacts.deviceControl.runLabel')}</span>
              </button>
            ) : (
              <button
                type="button"
                role="switch"
                aria-checked={on}
                aria-label={d.name}
                onClick={() => handle(d)}
                disabled={busy || !onAction}
                className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 disabled:cursor-not-allowed"
              >
                {/* Visual track (the button is the 44px touch target). */}
                <span
                  className={`relative inline-flex h-7 w-12 items-center rounded-full transition-colors ${
                    on ? 'bg-accent-500' : 'bg-gray-300 dark:bg-gray-600'
                  } ${busy ? 'opacity-60' : ''}`}
                >
                  <span
                    className={`inline-flex h-5 w-5 items-center justify-center rounded-full bg-white shadow transition-transform ${
                      on ? 'translate-x-6' : 'translate-x-1'
                    }`}
                  >
                    {busy && (
                      <Loader
                        className="h-3 w-3 animate-spin text-gray-500 motion-reduce:animate-none"
                        aria-hidden="true"
                      />
                    )}
                  </span>
                </span>
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
