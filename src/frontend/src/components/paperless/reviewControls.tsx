/**
 * Standard-library edit controls for the Paperless-audit review row.
 *
 * Combobox → Headless UI `Combobox` (accessible, keyboard, floating-ui anchoring
 * + portal via the `anchor` prop, so it escapes the table's overflow-x-auto).
 * Calendar → react-day-picker (accessible month grid + keyboard) inside a small
 * portal/positioning wrapper. These replace the previous hand-rolled widgets.
 */
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import type { TFunction } from 'i18next';
import {
  Combobox,
  ComboboxInput as HuiComboboxInput,
  ComboboxOptions,
  ComboboxOption,
} from '@headlessui/react';
import { DayPicker } from 'react-day-picker';
import { de, enUS } from 'date-fns/locale';
import { Check, Plus, RotateCcw } from 'lucide-react';
// react-day-picker's base stylesheet is imported once globally in main.tsx.

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function parseIso(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d); // local midnight — no TZ shift
}

// ---------------------------------------------------------------------------
// Combobox — pick an existing value or (when creatable) create a new one.
// ---------------------------------------------------------------------------

interface ComboboxInputProps {
  value: string;
  options: string[];
  creatable: boolean;
  placeholder?: string;
  ariaLabel: string;
  t: TFunction;
  /** Discard an in-progress entry on close instead of committing it — used for
   *  the tag add-input so a half-typed fragment can't be saved. */
  discardOnBlur?: boolean;
  onCommit: (value: string) => void;
  onCancel: () => void;
}

const OPTION_CLASS =
  'flex cursor-pointer items-center gap-2 px-2.5 py-1.5 data-[focus]:bg-gray-100 dark:data-[focus]:bg-gray-700';

export function ComboboxInput({ value, options, creatable, placeholder, ariaLabel, t, discardOnBlur = false, onCommit, onCancel }: ComboboxInputProps) {
  const [query, setQuery] = useState('');
  const committed = useRef(false);

  const q = query.trim().toLowerCase();
  // Existing-only: keep the current value reachable even if absent from the list.
  const base = !creatable && value && !options.includes(value) ? [value, ...options] : options;
  const filtered = q ? base.filter((o) => o.toLowerCase().includes(q)) : base;
  const exact = base.some((o) => o.toLowerCase() === q);
  const createValue = creatable && query.trim() !== '' && !exact ? query.trim() : null;

  const handleChange = (v: string | null) => {
    if (v == null) return;
    committed.current = true;
    onCommit(v);
  };
  // Escape/outside/blur close without a selection → commit the typed text
  // (creatable, visible not hidden) or, for existing-only, only an exact match.
  const handleClose = () => {
    if (committed.current) return;
    const typed = query.trim();
    if (discardOnBlur || typed === '' || typed === value) { onCancel(); return; }
    if (creatable) { onCommit(typed); return; }
    const match = base.find((o) => o.toLowerCase() === typed.toLowerCase());
    if (match) onCommit(match); else onCancel();
  };

  return (
    // displayValue → '' so the input starts EMPTY (the typed query drives
    // filtering); the current value is marked with a ✓ on its option, not
    // pre-filled — pre-filling would append to it on type.
    <Combobox immediate value={value} onChange={handleChange} onClose={handleClose}>
      <HuiComboboxInput
        autoFocus
        aria-label={ariaLabel}
        placeholder={placeholder ?? value}
        displayValue={() => ''}
        onChange={(e) => setQuery(e.target.value)}
        className="input py-1 px-1.5 text-xs w-full"
      />
      <ComboboxOptions
        anchor="bottom start"
        className="z-50 max-h-56 w-[max(var(--input-width),13rem)] rounded-lg border border-gray-200 bg-white py-1 text-xs shadow-lg empty:invisible focus:outline-none dark:border-gray-700 dark:bg-gray-800 [--anchor-gap:4px]"
      >
        {filtered.map((o) => (
          <ComboboxOption key={o} value={o} className={OPTION_CLASS}>
            {o === value && <Check className="w-3 h-3 shrink-0 text-accent-600 dark:text-accent-400" />}
            <span className={`truncate ${o === value ? 'font-medium' : ''} text-gray-800 dark:text-gray-100`}>{o}</span>
          </ComboboxOption>
        ))}
        {createValue && (
          <ComboboxOption value={createValue} className={OPTION_CLASS}>
            <Plus className="w-3 h-3 shrink-0 text-accent-600 dark:text-accent-400" />
            <span className="truncate text-gray-700 dark:text-gray-200">
              {t('paperlessAudit.review.createValue', { value: createValue })}
            </span>
          </ComboboxOption>
        )}
      </ComboboxOptions>
    </Combobox>
  );
}

// ---------------------------------------------------------------------------
// Calendar popover — react-day-picker in a portal, anchored to the field.
// ---------------------------------------------------------------------------

interface AnchorPos { left: number; width: number; top?: number; bottom?: number; }

/** Fixed coords anchored below (flipped above when tight) + clamped to the
 *  viewport, synced on scroll/resize. Positions the calendar portal. */
function useAnchoredPosition(anchorRef: RefObject<HTMLElement | null>, width: number): AnchorPos | null {
  const [pos, setPos] = useState<AnchorPos | null>(null);
  useLayoutEffect(() => {
    const el = anchorRef.current;
    if (!el) return;
    const update = () => {
      const r = el.getBoundingClientRect();
      const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
      const below = window.innerHeight - r.bottom - 8;
      const above = r.top - 8;
      setPos(below >= 320 || below >= above
        ? { left, width, top: r.bottom + 4 }
        : { left, width, bottom: window.innerHeight - r.top + 4 });
    };
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [anchorRef, width]);
  return pos;
}

function useOutside(refs: RefObject<HTMLElement | null>[], onOutside: () => void) {
  const cb = useRef(onOutside);
  cb.current = onOutside;
  useEffect(() => {
    const handler = (e: PointerEvent) => {
      const t = e.target as Node;
      if (refs.every((r) => !r.current || !r.current.contains(t))) cb.current();
    };
    document.addEventListener('pointerdown', handler, true);
    return () => document.removeEventListener('pointerdown', handler, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

interface CalendarPopoverProps {
  value: string;               // 'YYYY-MM-DD' or ''
  ariaLabel: string;
  locale: string;
  placeholder?: string;
  t: TFunction;
  onCommit: (iso: string) => void;   // '' clears the override (revert to suggestion)
  onCancel: () => void;
}

export function CalendarPopover({ value, ariaLabel, locale, placeholder, t, onCommit, onCancel }: CalendarPopoverProps) {
  const CAL_WIDTH = 260;
  const anchorRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const pos = useAnchoredPosition(anchorRef, CAL_WIDTH);
  const valid = value && ISO_DATE.test(value) ? value : '';
  const selected = valid ? parseIso(valid) : undefined;
  const [month, setMonth] = useState<Date>(selected ?? new Date());
  useOutside([anchorRef, popRef], onCancel);

  const style: CSSProperties = {
    position: 'fixed',
    left: pos?.left,
    top: pos?.top,
    bottom: pos?.bottom,
    width: CAL_WIDTH,
    // react-day-picker theming → DESIGN.md turquoise accent.
    ['--rdp-accent-color' as string]: 'var(--color-accent-500)',
    ['--rdp-accent-background-color' as string]: 'color-mix(in srgb, var(--color-accent-500) 16%, transparent)',
    ['--rdp-today-color' as string]: 'var(--color-accent-600)',
  };

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-label={ariaLabel}
        onKeyDown={(e) => { if (e.key === 'Escape') { e.preventDefault(); onCancel(); } }}
        className="input flex w-full items-center py-1 px-1.5 text-xs text-left"
      >
        <span className={valid ? 'text-gray-900 dark:text-gray-100' : 'italic text-gray-400'}>
          {valid || placeholder || '—'}
        </span>
      </button>
      {pos && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label={ariaLabel}
          style={style}
          className="paperless-rdp z-50 rounded-lg border border-gray-200 bg-white p-2 text-gray-800 shadow-lg dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
        >
          <DayPicker
            mode="single"
            autoFocus
            selected={selected}
            month={month}
            onMonthChange={setMonth}
            onSelect={(d) => { if (d) onCommit(iso(d)); }}
            locale={locale.startsWith('de') ? de : enUS}
            weekStartsOn={1}
          />
          <div className="mt-1 flex justify-end border-t border-gray-100 pt-1.5 dark:border-gray-700">
            <button
              type="button"
              onClick={() => onCommit('')}
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            >
              <RotateCcw className="w-3 h-3" /> {t('paperlessAudit.review.resetField')}
            </button>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
