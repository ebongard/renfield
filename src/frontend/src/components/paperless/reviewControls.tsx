/**
 * Custom, fully-styled edit controls for the Paperless-audit review row.
 *
 * The review table lives inside an `overflow-x-auto` container, so an absolutely
 * positioned dropdown would clip. Both controls render their overlay through a
 * portal to <body> with fixed positioning anchored to the field (recomputed on
 * scroll/resize, flipped/clamped to the viewport). Keyboard operation, focus,
 * outside-dismiss and ARIA are handled explicitly.
 */
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import type { TFunction } from 'i18next';
import { Check, Plus, ChevronLeft, ChevronRight, RotateCcw } from 'lucide-react';

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

interface AnchorPos {
  left: number;
  width: number;
  top?: number;
  bottom?: number;
  maxHeight: number;
}

/** Fixed-position coords anchored to `anchorRef`, flipped above when there's no
 *  room below and clamped horizontally to the viewport. Kept in sync on
 *  scroll/resize. `desiredWidth` is the min overlay width for the clamp. */
function useAnchoredPosition(anchorRef: RefObject<HTMLElement | null>, desiredWidth: number): AnchorPos | null {
  const [pos, setPos] = useState<AnchorPos | null>(null);
  useLayoutEffect(() => {
    const el = anchorRef.current;
    if (!el) return;
    const update = () => {
      const r = el.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const width = Math.max(r.width, desiredWidth);
      const left = Math.max(8, Math.min(r.left, vw - width - 8));
      const below = vh - r.bottom - 8;
      const above = r.top - 8;
      if (below >= 200 || below >= above) {
        setPos({ left, width, top: r.bottom + 4, maxHeight: Math.max(140, below) });
      } else {
        setPos({ left, width, bottom: vh - r.top + 4, maxHeight: Math.max(140, above) });
      }
    };
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [anchorRef, desiredWidth]);
  return pos;
}

/** Fire `onOutside` on a pointerdown outside every non-null ref. (A null ref —
 *  e.g. the listbox not rendered — must NOT block dismissal.) */
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

const OVERLAY_CLASS =
  'z-50 rounded-lg border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-800';

function overlayStyle(pos: AnchorPos, width?: number): React.CSSProperties {
  return {
    position: 'fixed',
    left: pos.left,
    width: width ?? pos.width,
    top: pos.top,
    bottom: pos.bottom,
    maxHeight: pos.maxHeight,
  };
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
  /** Discard an in-progress entry on blur/outside instead of committing it —
   *  used for the tag add-input so a half-typed fragment can't be saved. */
  discardOnBlur?: boolean;
  onCommit: (value: string) => void;
  onCancel: () => void;
}

export function ComboboxInput({ value, options, creatable, placeholder, ariaLabel, t, discardOnBlur = false, onCommit, onCancel }: ComboboxInputProps) {
  const [query, setQuery] = useState(value);
  const [active, setActive] = useState(-1); // -1 = the typed value is authoritative
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const popRef = useRef<HTMLUListElement>(null);
  const pos = useAnchoredPosition(wrapRef, 208);
  const listId = useId();

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const q = query.trim().toLowerCase();
  // Existing-only: keep the current value reachable even if absent from the list.
  const base = !creatable && value && !options.includes(value) ? [value, ...options] : options;
  const filtered = base.filter((o) => o.toLowerCase().includes(q));
  const exact = base.some((o) => o.toLowerCase() === q);
  const createValue = creatable && query.trim() !== '' && !exact ? query.trim() : null;
  const rowCount = filtered.length + (createValue ? 1 : 0);
  const clampedActive = active >= 0 && active < rowCount ? active : -1;

  const pickAt = (i: number) => {
    if (i < 0 || i >= rowCount) return;
    if (createValue && i === filtered.length) onCommit(createValue);
    else onCommit(filtered[i]);
  };

  // Blur/outside: creatable commits the typed text (visible, not a hidden
  // fragment); existing-only commits only an exact match, else reverts.
  const commitLoose = () => {
    const typed = query.trim();
    if (typed === value) { onCancel(); return; }
    if (creatable) { typed ? onCommit(typed) : onCancel(); return; }
    const match = base.find((o) => o.toLowerCase() === typed.toLowerCase());
    if (match) onCommit(match); else onCancel();
  };
  const onBlurBehaviour = discardOnBlur ? onCancel : commitLoose;
  useOutside([wrapRef, popRef], onBlurBehaviour);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, rowCount - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, -1)); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (clampedActive >= 0) pickAt(clampedActive);   // explicit highlight wins
      else if (createValue) onCommit(createValue);      // else commit the typed value
      else onBlurBehaviour();
    } else if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
    else if (e.key === 'Tab') { onBlurBehaviour(); }
  };

  const optionId = (i: number) => `${listId}-opt-${i}`;

  return (
    <div ref={wrapRef} className="min-w-0">
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded={rowCount > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={clampedActive >= 0 ? optionId(clampedActive) : undefined}
        aria-label={ariaLabel}
        value={query}
        placeholder={placeholder}
        onChange={(e) => { setQuery(e.target.value); setActive(-1); }}
        onKeyDown={onKeyDown}
        className="input py-1 px-1.5 text-xs w-full"
      />
      {pos && rowCount > 0 && createPortal(
        <ul
          ref={popRef}
          id={listId}
          role="listbox"
          className={`overflow-auto py-1 text-xs ${OVERLAY_CLASS}`}
          style={overlayStyle(pos)}
        >
          {filtered.map((o, i) => (
            <li key={o} id={optionId(i)} role="option" aria-selected={i === clampedActive}>
              <button
                type="button"
                tabIndex={-1}
                onMouseDown={(e) => { e.preventDefault(); onCommit(o); }}
                onMouseEnter={() => setActive(i)}
                className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left ${i === clampedActive ? 'bg-gray-100 dark:bg-gray-700' : ''}`}
              >
                {o === value && <Check className="w-3 h-3 shrink-0 text-accent-600 dark:text-accent-400" />}
                <span className={`truncate ${o === value ? 'font-medium' : ''} text-gray-800 dark:text-gray-100`}>{o}</span>
              </button>
            </li>
          ))}
          {createValue && (
            <li id={optionId(filtered.length)} role="option" aria-selected={clampedActive === filtered.length}>
              <button
                type="button"
                tabIndex={-1}
                onMouseDown={(e) => { e.preventDefault(); onCommit(createValue); }}
                onMouseEnter={() => setActive(filtered.length)}
                className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left ${clampedActive === filtered.length ? 'bg-gray-100 dark:bg-gray-700' : ''}`}
              >
                <Plus className="w-3 h-3 shrink-0 text-accent-600 dark:text-accent-400" />
                <span className="truncate text-gray-700 dark:text-gray-200">
                  {t('paperlessAudit.review.createValue', { value: createValue })}
                </span>
              </button>
            </li>
          )}
        </ul>,
        document.body,
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Calendar popover — month grid, locale weekday/month names, keyboard nav.
// ---------------------------------------------------------------------------

interface CalendarPopoverProps {
  value: string;               // 'YYYY-MM-DD' or ''
  ariaLabel: string;
  locale: string;
  placeholder?: string;
  t: TFunction;
  onCommit: (iso: string) => void;   // '' clears the override (revert to suggestion)
  onCancel: () => void;
}

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function parseIso(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d); // local midnight — no TZ shift
}

export function CalendarPopover({ value, ariaLabel, locale, placeholder, t, onCommit, onCancel }: CalendarPopoverProps) {
  const anchorRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const pos = useAnchoredPosition(anchorRef, 240);
  const valid = value && ISO_DATE.test(value) ? value : '';
  const [focused, setFocused] = useState<Date>(valid ? parseIso(valid) : new Date());

  useOutside([anchorRef, popRef], onCancel);
  // Roving focus: focus the focused day whenever it moves (and on open).
  useEffect(() => {
    gridRef.current?.querySelector<HTMLButtonElement>(`[data-iso="${iso(focused)}"]`)?.focus();
  }, [focused]);

  const y = focused.getFullYear();
  const m = focused.getMonth();
  const today = iso(new Date());
  const monthLabel = new Intl.DateTimeFormat(locale, { month: 'long', year: 'numeric' }).format(new Date(y, m, 1));
  // Monday-first weekday initials (2024-01-01 is a Monday).
  const weekdays = Array.from({ length: 7 }, (_, i) =>
    new Intl.DateTimeFormat(locale, { weekday: 'short' }).format(new Date(2024, 0, 1 + i)));

  const startOffset = (new Date(y, m, 1).getDay() + 6) % 7; // 0 = Monday
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const cells: (Date | null)[] = [];
  for (let i = 0; i < startOffset; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(new Date(y, m, d));

  const shiftMonth = (delta: number) => setFocused((f) => new Date(f.getFullYear(), f.getMonth() + delta, Math.min(f.getDate(), 28)));
  const move = (deltaDays: number) => setFocused((f) => { const d = new Date(f); d.setDate(d.getDate() + deltaDays); return d; });

  const onGridKey = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case 'ArrowLeft': e.preventDefault(); move(-1); break;
      case 'ArrowRight': e.preventDefault(); move(1); break;
      case 'ArrowUp': e.preventDefault(); move(-7); break;
      case 'ArrowDown': e.preventDefault(); move(7); break;
      case 'PageUp': e.preventDefault(); shiftMonth(-1); break;
      case 'PageDown': e.preventDefault(); shiftMonth(1); break;
      case 'Enter': case ' ': e.preventDefault(); onCommit(iso(focused)); break;
      case 'Escape': e.preventDefault(); onCancel(); break;
      default: break;
    }
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
        <div ref={popRef} role="dialog" aria-label={ariaLabel} className={`p-2 ${OVERLAY_CLASS}`} style={overlayStyle(pos, 240)}>
          <div className="mb-1 flex items-center justify-between px-1">
            <button type="button" onClick={() => shiftMonth(-1)} className="btn-icon btn-icon-ghost" aria-label={t('common.previous')}>
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-xs font-medium capitalize text-gray-800 dark:text-gray-100">{monthLabel}</span>
            <button type="button" onClick={() => shiftMonth(1)} className="btn-icon btn-icon-ghost" aria-label={t('common.next')}>
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
          <div ref={gridRef} role="grid" onKeyDown={onGridKey} className="grid grid-cols-7 gap-0.5">
            {weekdays.map((w) => (
              <span key={w} role="columnheader" className="py-1 text-center text-[10px] font-medium text-gray-400">{w}</span>
            ))}
            {cells.map((d, i) => {
              if (!d) return <span key={`e${i}`} />;
              const di = iso(d);
              const isSel = di === valid;
              const isFocused = di === iso(focused);
              const isToday = di === today;
              return (
                <button
                  key={di}
                  type="button"
                  role="gridcell"
                  data-iso={di}
                  aria-selected={isSel}
                  tabIndex={isFocused ? 0 : -1}
                  onClick={() => onCommit(di)}
                  className={`h-7 rounded text-xs tabular-nums transition-colors focus:outline-none focus:ring-2 focus:ring-accent-500
                    ${isSel ? 'bg-accent-500 text-white font-semibold'
                      : isToday ? 'font-semibold text-accent-600 dark:text-accent-400 hover:bg-gray-100 dark:hover:bg-gray-700'
                      : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700'}`}
                >
                  {d.getDate()}
                </button>
              );
            })}
          </div>
          <div className="mt-1.5 flex justify-end border-t border-gray-100 pt-1.5 dark:border-gray-700">
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
