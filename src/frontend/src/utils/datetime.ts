/**
 * Shared date/time formatting.
 *
 * Backend timestamps are stored as **naive UTC** (`models.database._utcnow` =
 * `datetime.now(UTC).replace(tzinfo=None)`) and serialized with `.isoformat()`,
 * so they reach the browser with NO timezone designator, e.g.
 * `"2026-07-17T10:30:00.123456"`. Per ECMAScript, `new Date()` parses a
 * date-time string *without* an offset as **local time** — so
 * `new Date(value).toLocaleString()` renders the UTC wall-clock as if it were
 * local and every timestamp shows off by the viewer's UTC offset (~2h behind in
 * CEST). These helpers normalize a tz-less datetime to UTC before formatting.
 *
 * Numbers (epoch ms) and Date objects are already absolute instants and pass
 * through unchanged. Empty / invalid input returns '' (callers that want a
 * different placeholder keep their own conditional).
 */
export type DateInput = string | number | Date | null | undefined;

const _HAS_TIME = /\d{2}:\d{2}/;
const _HAS_TZ = /([zZ])|([+-]\d{2}:?\d{2})$/;

/** Parse a backend value into a Date, treating a tz-less datetime as UTC.
 *  Returns null for empty / unparseable input. */
export function toLocalDate(value: DateInput): Date | null {
  if (value == null || value === '') return null;
  let d: Date;
  if (typeof value === 'string') {
    // A time component but no timezone marker → the backend's naive UTC.
    const s = _HAS_TIME.test(value) && !_HAS_TZ.test(value) ? `${value}Z` : value;
    d = new Date(s);
  } else {
    d = new Date(value);
  }
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Localized date + time. '' when absent/invalid. */
export function formatDateTime(
  value: DateInput,
  locale?: string,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d = toLocalDate(value);
  return d ? d.toLocaleString(locale, options) : '';
}

/** Localized calendar date. A bare `YYYY-MM-DD` must NOT shift by timezone, so
 *  it is parsed as a local calendar date; full timestamps use the UTC-normalized
 *  instant. '' when absent/invalid. */
export function formatDate(
  value: DateInput,
  locale?: string,
  options?: Intl.DateTimeFormatOptions,
): string {
  if (typeof value === 'string') {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (m) {
      return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).toLocaleDateString(
        locale,
        options,
      );
    }
  }
  const d = toLocalDate(value);
  return d ? d.toLocaleDateString(locale, options) : '';
}

/** Localized time-of-day. '' when absent/invalid. */
export function formatTime(
  value: DateInput,
  locale?: string,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d = toLocalDate(value);
  return d ? d.toLocaleTimeString(locale, options) : '';
}
