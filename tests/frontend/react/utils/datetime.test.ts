import { describe, it, expect } from 'vitest';
import {
  toLocalDate,
  formatDate,
  formatDateTime,
  formatTime,
} from '../../../../src/frontend/src/utils/datetime';

describe('datetime utils', () => {
  describe('toLocalDate — naive-UTC normalization', () => {
    it('treats a tz-less datetime as UTC (not local)', () => {
      // Backend naive-UTC ISO with no offset → must be the same instant as +Z.
      const naive = toLocalDate('2026-07-17T10:30:00');
      const withZ = new Date('2026-07-17T10:30:00Z');
      expect(naive?.getTime()).toBe(withZ.getTime());
    });

    it('leaves an explicit-Z timestamp untouched', () => {
      const d = toLocalDate('2026-07-17T10:30:00Z');
      expect(d?.getTime()).toBe(new Date('2026-07-17T10:30:00Z').getTime());
    });

    it('leaves an explicit numeric offset untouched', () => {
      const d = toLocalDate('2026-07-17T10:30:00+02:00');
      expect(d?.getTime()).toBe(new Date('2026-07-17T10:30:00+02:00').getTime());
    });

    it('passes epoch-ms numbers through as absolute instants', () => {
      const ms = 1_752_748_200_000;
      expect(toLocalDate(ms)?.getTime()).toBe(ms);
    });

    it('returns null for empty / invalid input', () => {
      expect(toLocalDate(null)).toBeNull();
      expect(toLocalDate(undefined)).toBeNull();
      expect(toLocalDate('')).toBeNull();
      expect(toLocalDate('not-a-date')).toBeNull();
    });
  });

  describe('formatDate — calendar date, no timezone shift', () => {
    it('renders a bare YYYY-MM-DD as that exact calendar day regardless of TZ', () => {
      // Parsed via local parts → never the previous/next day.
      const out = formatDate('2026-07-14', 'en-US');
      expect(out).toBe(new Date(2026, 6, 14).toLocaleDateString('en-US'));
    });

    it('returns "" for empty input', () => {
      expect(formatDate(null)).toBe('');
      expect(formatDate('')).toBe('');
    });
  });

  describe('formatDateTime / formatTime', () => {
    it('formats a tz-less timestamp at its UTC instant', () => {
      const expected = new Date('2026-07-17T10:30:00Z').toLocaleString('en-US');
      expect(formatDateTime('2026-07-17T10:30:00', 'en-US')).toBe(expected);
    });

    it('returns "" for empty input', () => {
      expect(formatDateTime(null)).toBe('');
      expect(formatTime(undefined)).toBe('');
    });
  });
});
