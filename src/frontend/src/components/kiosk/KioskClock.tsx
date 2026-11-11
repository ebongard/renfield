// KioskClock — the wall-time tile of the kiosk: time and date from the DEVICE
// clock only. No network, no socket, no data poll: the single timer here only
// redraws the clock.
//
// A wall display runs for weeks, so the tick is built to stay honest:
//   - one `setTimeout` aimed at the next minute boundary, re-aimed from
//     `Date.now()` after every fire — a late fire cannot accumulate drift the
//     way a chained `setInterval` does;
//   - `visibilitychange` re-syncs after the tab was hidden or the device slept
//     (no timer runs while hidden);
//   - the state is the epoch MINUTE, so a redundant sync is a no-op render;
//   - the component is memoised and owns its state, so a tick re-renders the
//     clock alone and a re-render of the constellation does not touch it.
//
// Motion: none, in any mode — no seconds, no blinking separator, no
// transition. That satisfies `prefers-reduced-motion` without a branch.
//
// The clock deliberately does NOT take part in the staleness display: it
// states the time, which stays true while the data behind the other tiles is
// stale. Staleness is flagged by the banner in KioskPage and the core/field
// turning red; a clock that froze or dimmed would show a wrong time instead.
import { memo, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

const MINUTE_MS = 60_000;

const TIME_OPTIONS: Intl.DateTimeFormatOptions = { timeStyle: 'short' };
const DATE_OPTIONS: Intl.DateTimeFormatOptions = { weekday: 'long', day: 'numeric', month: 'long' };

function currentMinute(): number {
  return Math.floor(Date.now() / MINUTE_MS);
}

/** Milliseconds until the next minute boundary (1..60000). Every real UTC
 *  offset is a whole number of minutes, so the epoch minute boundary is also
 *  the wall-clock one. */
function msToNextMinute(): number {
  return MINUTE_MS - (Date.now() % MINUTE_MS);
}

/** An `Intl.DateTimeFormat` for the active UI language. An unusable language
 *  tag falls back to the device locale instead of throwing and blanking the
 *  kiosk. */
function formatterFor(language: string | undefined, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat(language, options);
  } catch {
    return new Intl.DateTimeFormat(undefined, options);
  }
}

function useWallMinute(): number {
  const [minute, setMinute] = useState(currentMinute);

  useEffect(() => {
    let timer: number | undefined;

    const stop = () => {
      window.clearTimeout(timer);
      timer = undefined;
    };
    const sync = () => {
      stop();
      setMinute(currentMinute());
      timer = window.setTimeout(sync, msToNextMinute());
    };
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') stop();
      else sync();
    };

    onVisibility();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  return minute;
}

function KioskClock() {
  const { i18n } = useTranslation();
  const language = i18n.resolvedLanguage ?? i18n.language;
  const minute = useWallMinute();

  const timeFormat = useMemo(() => formatterFor(language, TIME_OPTIONS), [language]);
  const dateFormat = useMemo(() => formatterFor(language, DATE_OPTIONS), [language]);

  const now = new Date(minute * MINUTE_MS);

  return (
    // No live region: a screen reader must not announce every tick.
    <time dateTime={now.toISOString()} data-testid="kiosk-clock" className="block text-white/90">
      <span data-testid="kiosk-clock-time" className="block text-5xl font-display tabular-nums">
        {timeFormat.format(now)}
      </span>
      <span data-testid="kiosk-clock-date" className="block text-sm text-white/50 mt-1 first-letter:uppercase">
        {dateFormat.format(now)}
      </span>
    </time>
  );
}

export default memo(KioskClock);
