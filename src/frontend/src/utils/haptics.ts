/**
 * Haptic feedback for the mobile PWA (#84) -- a thin, never-throwing wrapper
 * around the Web Vibration API.
 *
 * Three gates, all of which must pass before anything vibrates:
 *   1. the per-device preference (localStorage, default OFF),
 *   2. the API exists (`navigator.vibrate` -- absent on iOS Safari and on most
 *      desktop browsers),
 *   3. the OS does not ask for reduced motion. `prefers-reduced-motion` is the
 *      only OS-level signal a browser exposes; there is no query for the
 *      system "vibration off" switch (the browser simply ignores the call).
 *
 * The preference is read on every call rather than cached: the read is a
 * single localStorage lookup, and a cached copy would go stale in a second tab
 * or after the toggle on the account page.
 */

export type HapticPattern = 'tap' | 'success' | 'error' | 'notification';

/** Durations in ms; an array alternates vibrate / pause / vibrate. */
export const HAPTIC_PATTERNS: Readonly<Record<HapticPattern, number | readonly number[]>> = {
  tap: 10,
  success: [10, 50, 10],
  error: 200,
  notification: [10, 30, 10, 30, 10],
};

export const HAPTICS_STORAGE_KEY = 'renfield_haptics_enabled';

/** Whether this browser has a Vibration API at all. */
export function isHapticsSupported(): boolean {
  return typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function';
}

/** Reduced motion means no haptics. An unreadable media query counts as "no preference". */
export function prefersReducedMotion(): boolean {
  try {
    return (
      typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  } catch {
    return false;
  }
}

/** The stored preference. Anything but an explicit `'true'` -- including a blocked storage -- is OFF. */
export function loadHapticsEnabled(): boolean {
  try {
    return localStorage.getItem(HAPTICS_STORAGE_KEY) === 'true';
  } catch {
    // localStorage not available (e.g., private browsing)
    return false;
  }
}

/**
 * Persist the preference.
 *
 * @returns whether the write went through. A caller that shows the state must
 * not claim "on" after a `false`: `haptic()` reads the storage and stays off.
 */
export function saveHapticsEnabled(enabled: boolean): boolean {
  try {
    localStorage.setItem(HAPTICS_STORAGE_KEY, String(enabled));
    return true;
  } catch {
    return false;
  }
}

/**
 * Play a pattern if the preference is on and the device can. Never throws:
 * browsers reject the call without a prior user gesture (returning `false` or,
 * in some embedded contexts, throwing), and tactile feedback is never worth a
 * broken click handler.
 */
export function haptic(pattern: HapticPattern): void {
  if (!isHapticsSupported() || !loadHapticsEnabled() || prefersReducedMotion()) return;
  try {
    const value = HAPTIC_PATTERNS[pattern];
    navigator.vibrate(typeof value === 'number' ? value : [...value]);
  } catch {
    // Blocked (no user activation, permissions policy) -- stay silent.
  }
}
