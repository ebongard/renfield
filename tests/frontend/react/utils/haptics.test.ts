/**
 * haptic() -- the three gates (preference, API, reduced motion) and the promise
 * that it never throws into a click handler.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';

import {
  HAPTICS_STORAGE_KEY,
  haptic,
  isHapticsSupported,
  loadHapticsEnabled,
  prefersReducedMotion,
  saveHapticsEnabled,
  type HapticPattern,
} from '../../../../src/frontend/src/utils/haptics';
import {
  blockStorage,
  installVibrate,
  removeVibrate,
  resetHapticsEnvironment,
  setHapticsPreference,
  setReducedMotion,
} from '../haptics-test-helpers';

afterEach(resetHapticsEnvironment);

describe('haptic — patterns', () => {
  const cases: Array<[HapticPattern, number | number[]]> = [
    ['tap', 10],
    ['success', [10, 50, 10]],
    ['error', 200],
    ['notification', [10, 30, 10, 30, 10]],
  ];

  it.each(cases)('%s vibrates with its pattern when enabled', (pattern, expected) => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    haptic(pattern);
    expect(vibrate).toHaveBeenCalledTimes(1);
    expect(vibrate).toHaveBeenCalledWith(expected);
  });
});

describe('haptic — gates', () => {
  it('is off by default (nothing stored)', () => {
    const vibrate = installVibrate();
    haptic('tap');
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('does nothing when the preference is explicitly off', () => {
    const vibrate = installVibrate();
    setHapticsPreference(false);
    haptic('tap');
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('treats any stored value other than "true" as off', () => {
    const vibrate = installVibrate();
    window.localStorage.setItem(HAPTICS_STORAGE_KEY, '1');
    haptic('tap');
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('does nothing — and does not throw — when the Vibration API is missing', () => {
    removeVibrate();
    setHapticsPreference(true);
    expect(isHapticsSupported()).toBe(false);
    expect(() => haptic('tap')).not.toThrow();
  });

  it('does nothing under prefers-reduced-motion', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    setReducedMotion(true);
    expect(prefersReducedMotion()).toBe(true);
    haptic('notification');
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('reads the preference on every call (a change in another tab takes effect)', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    haptic('tap');
    setHapticsPreference(false);
    haptic('tap');
    expect(vibrate).toHaveBeenCalledTimes(1);
  });
});

describe('haptic — never throws', () => {
  it('swallows a throwing navigator.vibrate', () => {
    const vibrate = installVibrate(() => {
      throw new Error('NotAllowedError: no user activation');
    });
    setHapticsPreference(true);
    expect(() => haptic('error')).not.toThrow();
    expect(vibrate).toHaveBeenCalledTimes(1);
  });

  it('tolerates navigator.vibrate returning false', () => {
    const vibrate = installVibrate(() => false);
    setHapticsPreference(true);
    expect(() => haptic('tap')).not.toThrow();
    expect(vibrate).toHaveBeenCalledTimes(1);
  });

  it('a throwing storage read degrades to off', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    blockStorage('getItem');
    expect(loadHapticsEnabled()).toBe(false);
    expect(() => haptic('tap')).not.toThrow();
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('a throwing matchMedia counts as "no preference", not as a crash', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    vi.mocked(window.matchMedia).mockImplementation(() => {
      throw new Error('matchMedia unavailable');
    });
    expect(prefersReducedMotion()).toBe(false);
    haptic('tap');
    expect(vibrate).toHaveBeenCalledWith(10);
  });
});

describe('preference storage', () => {
  it('round-trips through localStorage', () => {
    expect(loadHapticsEnabled()).toBe(false);
    expect(saveHapticsEnabled(true)).toBe(true);
    expect(window.localStorage.getItem(HAPTICS_STORAGE_KEY)).toBe('true');
    expect(loadHapticsEnabled()).toBe(true);
    expect(saveHapticsEnabled(false)).toBe(true);
    expect(loadHapticsEnabled()).toBe(false);
  });

  it('reports a write that did not go through instead of throwing', () => {
    blockStorage('setItem');
    expect(saveHapticsEnabled(true)).toBe(false);
  });
});
