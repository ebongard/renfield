/**
 * Shared scaffolding for the haptics tests: jsdom has no `navigator.vibrate`,
 * so each test installs (or deliberately withholds) one.
 */
import { vi, type Mock } from 'vitest';

import { HAPTICS_STORAGE_KEY } from '../../../src/frontend/src/utils/haptics';

export type VibrateMock = Mock<(pattern: number | number[]) => boolean>;

/** Give this "browser" a Vibration API and return the spy. */
export function installVibrate(impl: (pattern: number | number[]) => boolean = () => true): VibrateMock {
  const vibrate: VibrateMock = vi.fn(impl);
  Object.defineProperty(navigator, 'vibrate', { configurable: true, writable: true, value: vibrate });
  return vibrate;
}

/** Back to jsdom's default: no Vibration API (iOS Safari, desktop). */
export function removeVibrate(): void {
  Reflect.deleteProperty(navigator, 'vibrate');
}

export function setHapticsPreference(enabled: boolean): void {
  window.localStorage.setItem(HAPTICS_STORAGE_KEY, String(enabled));
}

const storageRestorers: Array<() => void> = [];

/**
 * Make one storage call throw, as a browser with blocked site data does.
 *
 * setup.ts replaces `window.localStorage` with a plain object of `vi.fn`s, so
 * neither `Storage.prototype` nor `vi.restoreAllMocks()` reaches it: the method
 * is swapped on that object and put back by `resetHapticsEnvironment`.
 */
export function blockStorage(method: 'getItem' | 'setItem'): void {
  const storage = window.localStorage;
  const original = Object.getOwnPropertyDescriptor(storage, method);
  if (!original) throw new Error(`localStorage mock has no own ${method}`);
  Object.defineProperty(storage, method, {
    configurable: true,
    writable: true,
    value: (): never => {
      throw new Error('SecurityError: storage blocked');
    },
  });
  storageRestorers.push(() => Object.defineProperty(storage, method, original));
}

/** Answer `(prefers-reduced-motion: reduce)` with `reduced`; every other query stays false. */
export function setReducedMotion(reduced: boolean): void {
  vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
    matches: reduced && query === '(prefers-reduced-motion: reduce)',
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

/** Undo everything the helpers above touch. Call from `afterEach`. */
export function resetHapticsEnvironment(): void {
  while (storageRestorers.length > 0) storageRestorers.pop()?.();
  removeVibrate();
  setReducedMotion(false);
  window.localStorage.removeItem(HAPTICS_STORAGE_KEY);
}
