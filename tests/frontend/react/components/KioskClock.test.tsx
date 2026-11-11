/**
 * KioskClock — the wall-time tile of /kiosk (#263). Device clock only: these
 * tests pin the formats per language, the minute-aligned tick, the re-sync
 * after a hidden period, the cleanup, the absence of motion and the absence of
 * any network use.
 */
import { describe, it, expect, beforeAll, beforeEach, afterAll, afterEach, vi } from 'vitest';
import { act, cleanup, screen } from '@testing-library/react';
import { useState } from 'react';
import i18n from '../../../../src/frontend/src/i18n';
import { renderWithProviders } from '../test-utils';
import KioskClock from '../../../../src/frontend/src/components/kiosk/KioskClock';

const ORIGINAL_TZ = process.env.TZ;

// 21:41:30 wall time in Berlin (CET, UTC+1) on Wednesday 11 November 2026.
const EVENING = new Date('2026-11-11T20:41:30Z');
// 23:59:30 wall time in Berlin on the same day.
const BEFORE_MIDNIGHT = new Date('2026-11-11T22:59:30Z');

const timeText = () => screen.getByTestId('kiosk-clock-time').textContent;
const dateText = () => screen.getByTestId('kiosk-clock-date').textContent;
const machineTime = () => screen.getByTestId('kiosk-clock').getAttribute('dateTime');

function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

function stubReducedMotion(): void {
  vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
    matches: query.includes('prefers-reduced-motion'),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

beforeAll(() => {
  process.env.TZ = 'Europe/Berlin';
});

afterAll(() => {
  process.env.TZ = ORIGINAL_TZ;
});

beforeEach(async () => {
  await i18n.changeLanguage('de');
  vi.useFakeTimers();
  vi.setSystemTime(EVENING);
});

afterEach(async () => {
  // Unmount first: react-i18next re-renders mounted components on every
  // languageChanged event, which would be an update outside act().
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
  await i18n.changeLanguage('de');
});

describe('KioskClock', () => {
  it('renders wall time and date in the German format', () => {
    renderWithProviders(<KioskClock />);
    expect(timeText()).toBe('21:41');
    expect(dateText()).toBe('Mittwoch, 11. November');
  });

  it('renders wall time and date in the English format', async () => {
    await i18n.changeLanguage('en');
    renderWithProviders(<KioskClock />);
    // Intl separates the day period with a (narrow) no-break space.
    expect(timeText()).toMatch(/^9:41\sPM$/);
    expect(dateText()).toBe('Wednesday, November 11');
  });

  it('follows a language switch while mounted', async () => {
    renderWithProviders(<KioskClock />);
    expect(dateText()).toBe('Mittwoch, 11. November');
    await act(async () => {
      await i18n.changeLanguage('en');
    });
    expect(dateText()).toBe('Wednesday, November 11');
  });

  it('falls back to the device locale instead of blanking on an unusable language tag', () => {
    const RealFormat = Intl.DateTimeFormat;
    const requested: Array<string | undefined> = [];
    const spy = vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(function (
      locale?: Intl.LocalesArgument,
      options?: Intl.DateTimeFormatOptions,
    ) {
      requested.push(typeof locale === 'string' ? locale : undefined);
      if (locale !== undefined) throw new RangeError('Incorrect locale information provided');
      return new RealFormat('de', options);
    });
    renderWithProviders(<KioskClock />);
    spy.mockRestore();
    expect(timeText()).toBe('21:41');
    expect(dateText()).toBe('Mittwoch, 11. November');
    // Both formatters asked for the UI language first, then for the device locale.
    expect(requested).toEqual(['de', undefined, 'de', undefined]);
  });

  it('exposes a machine-readable instant on a <time> element, without a live region', () => {
    renderWithProviders(<KioskClock />);
    const clock = screen.getByTestId('kiosk-clock');
    expect(clock.tagName).toBe('TIME');
    expect(machineTime()).toBe('2026-11-11T20:41:00.000Z');
    expect(clock.closest('[aria-live], [role="status"], [role="timer"], [role="alert"]')).toBeNull();
    expect(clock.querySelector('[aria-live], [role="status"], [role="timer"], [role="alert"]')).toBeNull();
  });

  it('advances exactly at the minute boundary, not before', () => {
    renderWithProviders(<KioskClock />);
    // 21:41:30 → 21:41:59.999
    act(() => {
      vi.advanceTimersByTime(29_999);
    });
    expect(timeText()).toBe('21:41');
    // → 21:42:00.000
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(timeText()).toBe('21:42');
    expect(machineTime()).toBe('2026-11-11T20:42:00.000Z');
  });

  it('keeps ticking on the boundary for the following minutes', () => {
    renderWithProviders(<KioskClock />);
    act(() => {
      vi.advanceTimersByTime(30_000 + 3 * 60_000);
    });
    expect(timeText()).toBe('21:45');
    // Exactly one pending timer at any time — nothing accumulates.
    expect(vi.getTimerCount()).toBe(1);
  });

  it('changes the date when it crosses midnight', () => {
    vi.setSystemTime(BEFORE_MIDNIGHT);
    renderWithProviders(<KioskClock />);
    expect(timeText()).toBe('23:59');
    expect(dateText()).toBe('Mittwoch, 11. November');
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(timeText()).toBe('00:00');
    expect(dateText()).toBe('Donnerstag, 12. November');
  });

  it('re-syncs when the tab becomes visible after a hidden period', () => {
    renderWithProviders(<KioskClock />);
    setVisibility('hidden');
    // Hidden: no timer is kept alive.
    expect(vi.getTimerCount()).toBe(0);
    // The device slept for 2 h 17 min; no timer fired meanwhile.
    vi.setSystemTime(new Date('2026-11-11T22:58:40Z'));
    expect(timeText()).toBe('21:41');

    setVisibility('visible');
    expect(timeText()).toBe('23:58');
    // …and the tick is re-aimed at the NEW minute boundary (20 s away).
    act(() => {
      vi.advanceTimersByTime(19_999);
    });
    expect(timeText()).toBe('23:58');
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(timeText()).toBe('23:59');
    expect(vi.getTimerCount()).toBe(1);
  });

  it('corrects itself on the next fire when the clock jumped without a visibility event', () => {
    renderWithProviders(<KioskClock />);
    // The wall clock jumps (sleep / NTP step) while the pending timer stays put.
    vi.setSystemTime(new Date('2026-11-11T21:10:10Z'));
    act(() => {
      vi.runOnlyPendingTimers();
    });
    expect(timeText()).toBe('22:10');
    // Re-aimed from the real time: next fire at 22:11:00, not 60 s after the late one.
    act(() => {
      vi.advanceTimersByTime(50_000);
    });
    expect(timeText()).toBe('22:11');
  });

  it('clears its timer and its listener on unmount', () => {
    const add = vi.spyOn(document, 'addEventListener');
    const remove = vi.spyOn(document, 'removeEventListener');
    const { unmount } = renderWithProviders(<KioskClock />);
    expect(vi.getTimerCount()).toBe(1);
    const added = add.mock.calls.filter(([type]) => type === 'visibilitychange');
    expect(added).toHaveLength(1);

    unmount();
    expect(vi.getTimerCount()).toBe(0);
    const removed = remove.mock.calls.filter(([type]) => type === 'visibilitychange');
    expect(removed).toHaveLength(1);
    // The SAME handler that was added — otherwise the listener leaks.
    expect(removed[0][1]).toBe(added[0][1]);
    add.mockRestore();
    remove.mockRestore();
  });

  it('shows no seconds and no animation under reduced motion', () => {
    stubReducedMotion();
    renderWithProviders(<KioskClock />);
    expect(timeText()).toBe('21:41');
    const clock = screen.getByTestId('kiosk-clock');
    for (const el of [clock, ...Array.from(clock.querySelectorAll('*'))]) {
      expect(el.getAttribute('class') ?? '').not.toMatch(/animate-|transition|duration-|k-/);
      expect(el.getAttribute('style')).toBeNull();
    }
    // A second later nothing has moved: no ticking seconds, no blinking separator.
    const before = clock.outerHTML;
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(clock.outerHTML).toBe(before);
  });

  it('makes no fetch and opens no socket', () => {
    const fetchSpy = vi.fn();
    const socketSpy = vi.fn();
    const eventSourceSpy = vi.fn();
    const xhrSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    vi.stubGlobal('WebSocket', socketSpy);
    vi.stubGlobal('EventSource', eventSourceSpy);
    vi.stubGlobal('XMLHttpRequest', xhrSpy);

    renderWithProviders(<KioskClock />);
    act(() => {
      vi.advanceTimersByTime(5 * 60_000);
    });
    setVisibility('hidden');
    setVisibility('visible');

    expect(timeText()).toBe('21:46');
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(socketSpy).not.toHaveBeenCalled();
    expect(eventSourceSpy).not.toHaveBeenCalled();
    expect(xhrSpy).not.toHaveBeenCalled();
  });

  it('re-renders alone: a tick does not render the parent, a parent render does not render the clock', () => {
    let parentRenders = 0;
    let rerenderParent: () => void = () => {};
    // The clock derives its `dateTime` attribute once per render, so this spy
    // counts the clock's renders without touching the component.
    const clockRender = vi.spyOn(Date.prototype, 'toISOString');

    function Parent() {
      const [, setCount] = useState(0);
      parentRenders += 1;
      rerenderParent = () => setCount((c) => c + 1);
      return <KioskClock />;
    }

    renderWithProviders(<Parent />);
    const parentAfterMount = parentRenders;
    const clockAfterMount = clockRender.mock.calls.length;
    expect(clockAfterMount).toBeGreaterThan(0);

    // A tick renders the clock but not the parent.
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(timeText()).toBe('21:42');
    expect(parentRenders).toBe(parentAfterMount);
    const clockAfterTick = clockRender.mock.calls.length;
    expect(clockAfterTick).toBeGreaterThan(clockAfterMount);

    // A parent render leaves the memoised clock alone.
    act(() => {
      rerenderParent();
    });
    expect(parentRenders).toBe(parentAfterMount + 1);
    expect(clockRender.mock.calls.length).toBe(clockAfterTick);
    clockRender.mockRestore();
  });
});
