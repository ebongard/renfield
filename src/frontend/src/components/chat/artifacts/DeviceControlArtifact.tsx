/**
 * DeviceControlArtifact — an INTERACTIVE device-control widget (Gen-UI item 10):
 * on/off toggles for lights & switches, a brightness slider for on-lights, a
 * "run" button for scenes, and a thermostat setpoint stepper for climate.
 * Clicking/dragging a control calls `onAction(entity_id, action, value?)` which
 * sends a `device_action` WS frame; the backend gates on HA_CONTROL and
 * re-validates entity/action/value, so the widget grants no control the user
 * lacks via the agent. We optimistically update and reconcile to the resolved
 * value the backend returns.
 *
 * No model HTML — typed JSON → React controls. `onAction` is threaded as a prop
 * (not pulled from ChatContext) so the artifact layer stays decoupled/testable;
 * when absent the controls render disabled.
 */
import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Lightbulb, Play, Loader, Thermometer, Minus, Plus } from 'lucide-react';
import type { DeviceControlData, DeviceControlDevice } from './artifactSchema';

export type DeviceActionResult = {
  success: boolean;
  state?: string;
  brightness?: number;
  targetTemp?: number;
};
export type DeviceActionFn = (
  entityId: string,
  action: string,
  value?: number,
) => Promise<DeviceActionResult>;

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
  // Local display state seeded from props, keyed by entity_id.
  const [states, setStates] = useState<Record<string, string>>(() =>
    Object.fromEntries(data.devices.map((d) => [d.entity_id, d.state])),
  );
  const [brightness, setBrightness] = useState<Record<string, number | undefined>>(() =>
    Object.fromEntries(data.devices.map((d) => [d.entity_id, d.brightness])),
  );
  const [targetTemp, setTargetTemp] = useState<Record<string, number | undefined>>(() =>
    Object.fromEntries(data.devices.map((d) => [d.entity_id, d.targetTemp])),
  );
  const [pending, setPending] = useState<Set<string>>(() => new Set());
  const brightnessTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const setBusy = (id: string, busy: boolean) =>
    setPending((prev) => {
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });

  // Toggle a light/switch, or run a scene.
  const handleToggle = async (d: DeviceControlDevice) => {
    if (!onAction || pending.has(d.entity_id)) return;
    const scene = d.domain === 'scene';
    const action = scene ? 'activate' : 'toggle';
    const prev = states[d.entity_id];
    if (!scene) setStates((s) => ({ ...s, [d.entity_id]: isOn(prev) ? 'off' : 'on' }));
    setBusy(d.entity_id, true);
    const res = await onAction(d.entity_id, action);
    setBusy(d.entity_id, false);
    if (!scene) {
      setStates((s) => ({ ...s, [d.entity_id]: res.success ? (res.state ?? s[d.entity_id]) : prev }));
    }
  };

  // Brightness slider — local update on every move, debounced send.
  const handleBrightnessInput = (d: DeviceControlDevice, value: number) => {
    setBrightness((b) => ({ ...b, [d.entity_id]: value }));
    if (states[d.entity_id] !== 'on' && value > 0) setStates((s) => ({ ...s, [d.entity_id]: 'on' }));
    if (!onAction) return;
    clearTimeout(brightnessTimers.current[d.entity_id]);
    brightnessTimers.current[d.entity_id] = setTimeout(async () => {
      setBusy(d.entity_id, true);
      const res = await onAction(d.entity_id, 'set_brightness', value);
      setBusy(d.entity_id, false);
      if (res.success) {
        if (res.brightness !== undefined) setBrightness((b) => ({ ...b, [d.entity_id]: res.brightness }));
        if (res.state) setStates((s) => ({ ...s, [d.entity_id]: res.state as string }));
      }
    }, 350);
  };

  // Thermostat setpoint stepper — discrete +/- sends immediately.
  const handleTempStep = async (d: DeviceControlDevice, delta: number) => {
    if (!onAction || pending.has(d.entity_id)) return;
    const cur = targetTemp[d.entity_id] ?? d.targetTemp ?? d.currentTemp ?? 20;
    let next = cur + delta;
    if (d.minTemp !== undefined) next = Math.max(d.minTemp, next);
    if (d.maxTemp !== undefined) next = Math.min(d.maxTemp, next);
    next = Math.round(next * 10) / 10;
    setTargetTemp((tt) => ({ ...tt, [d.entity_id]: next }));
    setBusy(d.entity_id, true);
    const res = await onAction(d.entity_id, 'set_temperature', next);
    setBusy(d.entity_id, false);
    if (res.success && res.targetTemp !== undefined) {
      setTargetTemp((tt) => ({ ...tt, [d.entity_id]: res.targetTemp }));
    } else if (!res.success) {
      setTargetTemp((tt) => ({ ...tt, [d.entity_id]: cur }));
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
        const busy = pending.has(d.entity_id);
        const on = isOn(states[d.entity_id] ?? d.state);

        // --- Climate: current temp + a setpoint stepper ---
        if (d.domain === 'climate') {
          const target = targetTemp[d.entity_id] ?? d.targetTemp;
          const step = d.tempStep ?? 0.5;
          return (
            <div key={d.entity_id} className="flex items-center justify-between gap-3 py-2">
              <span className="flex min-w-0 items-center gap-2 text-sm text-gray-800 dark:text-gray-200">
                <Thermometer className="h-4 w-4 shrink-0 text-accent-500" aria-hidden="true" />
                <span className="truncate">
                  {d.name}
                  {d.currentTemp !== undefined && (
                    <span className="ml-1.5 text-xs text-gray-500 dark:text-gray-400 tabular-nums">
                      {Math.round(d.currentTemp * 10) / 10}°
                    </span>
                  )}
                </span>
              </span>
              <span className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => handleTempStep(d, -step)}
                  disabled={busy || !onAction}
                  aria-label={t('chat.artifacts.deviceControl.tempDown', { name: d.name })}
                  className="flex h-11 w-11 items-center justify-center rounded-lg bg-gray-100 text-gray-700 hover:bg-gray-200 disabled:opacity-50 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
                >
                  <Minus className="h-4 w-4" aria-hidden="true" />
                </button>
                <span className="min-w-[3rem] text-center text-sm font-semibold tabular-nums text-gray-900 dark:text-gray-100" aria-live="polite">
                  {target !== undefined ? `${Math.round(target * 10) / 10}°` : '–'}
                </span>
                <button
                  type="button"
                  onClick={() => handleTempStep(d, step)}
                  disabled={busy || !onAction}
                  aria-label={t('chat.artifacts.deviceControl.tempUp', { name: d.name })}
                  className="flex h-11 w-11 items-center justify-center rounded-lg bg-gray-100 text-gray-700 hover:bg-gray-200 disabled:opacity-50 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
                >
                  <Plus className="h-4 w-4" aria-hidden="true" />
                </button>
              </span>
            </div>
          );
        }

        // --- Scene: a run button ---
        if (d.domain === 'scene') {
          return (
            <div key={d.entity_id} className="flex items-center justify-between gap-3 py-1">
              <span className="flex min-w-0 items-center gap-2 text-sm text-gray-800 dark:text-gray-200">
                <Play className="h-4 w-4 shrink-0 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                <span className="truncate">{d.name}</span>
              </span>
              <button
                type="button"
                onClick={() => handleToggle(d)}
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
            </div>
          );
        }

        // --- Light / switch: a toggle, plus a brightness slider for an on-light ---
        const showSlider = d.domain === 'light' && on && brightness[d.entity_id] !== undefined;
        return (
          <div key={d.entity_id} className="py-1">
            <div className="flex items-center justify-between gap-3">
              <span className="flex min-w-0 items-center gap-2 text-sm text-gray-800 dark:text-gray-200">
                <Lightbulb
                  className={`h-4 w-4 shrink-0 ${on && d.domain === 'light' ? 'text-amber-500' : 'text-gray-400 dark:text-gray-500'}`}
                  aria-hidden="true"
                />
                <span className="truncate">{d.name}</span>
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={on}
                aria-label={d.name}
                onClick={() => handleToggle(d)}
                disabled={busy || !onAction}
                className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 disabled:cursor-not-allowed"
              >
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
                      <Loader className="h-3 w-3 animate-spin text-gray-500 motion-reduce:animate-none" aria-hidden="true" />
                    )}
                  </span>
                </span>
              </button>
            </div>
            {showSlider && (
              <div className="mt-1 flex items-center gap-2 pl-6">
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={brightness[d.entity_id] ?? 0}
                  onChange={(e) => handleBrightnessInput(d, Number(e.target.value))}
                  disabled={!onAction}
                  aria-label={t('chat.artifacts.deviceControl.brightness', { name: d.name })}
                  className="h-1.5 w-full cursor-pointer accent-accent-500"
                />
                <span className="w-9 shrink-0 text-right text-xs tabular-nums text-gray-500 dark:text-gray-400">
                  {brightness[d.entity_id] ?? 0}%
                </span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
