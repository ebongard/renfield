/**
 * Custom, fully-styled edit controls for the Paperless-audit review row.
 *
 * The review table lives inside an `overflow-x-auto` container, so an
 * absolutely-positioned dropdown would clip. Both controls render their
 * overlay through a portal to <body> with fixed positioning anchored to the
 * field (recomputed on scroll/resize), so they escape the clip while staying
 * visually attached. Keyboard + outside-click behaviour is handled explicitly.
 */
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import type { TFunction } from 'i18next';
import { Check, Plus, ChevronLeft, ChevronRight } from 'lucide-react';

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

interface AnchorPos {
  top: number;
  left: number;
  width: number;
}

/** Fixed-position coordinates just below an anchor element, kept in sync while
 *  the overlay is open (scroll/resize). */
function useAnchoredPosition(anchorRef: RefObject<HTMLElement | null>): AnchorPos | null {
  const [pos, setPos] = useState<AnchorPos | null>(null);
  useLayoutEffect(() => {
    const el = anchorRef.current;
    if (!el) return;
    const update = () => {
      const r = el.getBoundingClientRect();
      setPos({ top: r.bottom + 4, left: r.left, width: Math.max(r.width, 208) });
    };
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [anchorRef]);
  return pos;
}

/** Fire `onOutside` on a pointerdown outside every provided ref. */
function useOutside(refs: RefObject<HTMLElement | null>[], onOutside: () => void) {
  useEffect(() => {
    const handler = (e: PointerEvent) => {
      const t = e.target as Node;
      if (refs.every((r) => r.current && !r.current.contains(t))) onOutside();
    };
    document.addEventListener('pointerdown', handler, true);
    return () => document.removeEventListener('pointerdown', handler, true);
  }, [refs, onOutside]);
}

const OVERLAY_CLASS =
  'z-50 rounded-lg border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-800';

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
  const [active, setActive] = useState(0);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const popRef = useRef<HTMLUListElement>(null);
  const pos = useAnchoredPosition(wrapRef);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const q = query.trim().toLowerCase();
  const filtered = options.filter((o) => o.toLowerCase().includes(q));
  const exact = options.some((o) => o.toLowerCase() === q);
  const createValue = creatable && query.trim() !== '' && !exact ? query.trim() : null;
  // rows: existing options first, then an optional "create" row
  const rowCount = filtered.length + (createValue ? 1 : 0);
  const clampedActive = rowCount === 0 ? -1 : Math.min(active, rowCount - 1);

  const pickAt = (i: number) => {
    if (i < 0 || i >= rowCount) return;
    if (createValue && i === rowCount - 1) onCommit(createValue);
    else onCommit(filtered[i]);
  };

  // Outside / blur: for a creatable field commit the typed text (visible, not a
  // hidden fragment); for existing-only, commit only an exact match else cancel.
  const commitLoose = () => {
    const typed = query.trim();
    if (typed === value) { onCancel(); return; }
    if (creatable) { typed ? onCommit(typed) : onCancel(); return; }
    const match = options.find((o) => o.toLowerCase() === typed.toLowerCase());
    if (match) onCommit(match);
    else onCancel();
  };

  const onBlurBehaviour = discardOnBlur ? onCancel : commitLoose;
  useOutside([wrapRef, popRef], onBlurBehaviour);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min((a < 0 ? -1 : a) + 1, rowCount - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (clampedActive >= 0) pickAt(clampedActive);
      else if (createValue) onCommit(createValue);
      else onBlurBehaviour();
    } else if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
    else if (e.key === 'Tab') { onBlurBehaviour(); }
  };

  return (
    <div ref={wrapRef} className="min-w-0">
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded="true"
        aria-label={ariaLabel}
        value={query}
        placeholder={placeholder}
        onChange={(e) => { setQuery(e.target.value); setActive(0); }}
        onKeyDown={onKeyDown}
        className="input py-1 px-1.5 text-xs w-full"
      />
      {pos && rowCount > 0 && createPortal(
        <ul
          ref={popRef}
          role="listbox"
          className={`fixed max-h-56 overflow-auto py-1 text-xs ${OVERLAY_CLASS}`}
          style={{ top: pos.top, left: pos.left, width: pos.width }}
        >
          {filtered.map((o, i) => (
            <li key={o} role="option" aria-selected={i === clampedActive}>
              <button
                type="button"
                onMouseDown={(e) => { e.preventDefault(); onCommit(o); }}
                onMouseEnter={() => setActive(i)}
                className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left ${i === clampedActive ? 'bg-gray-100 dark:bg-gray-700' : ''}`}
              >
                {o === value && <Check className="w-3 h-3 shrink-0 text-emerald-500" />}
                <span className={`truncate ${o === value ? 'font-medium' : ''} text-gray-800 dark:text-gray-100`}>{o}</span>
              </button>
            </li>
          ))}
          {createValue && (
            <li role="option" aria-selected={clampedActive === rowCount - 1}>
              <button
                type="button"
                onMouseDown={(e) => { e.preventDefault(); onCommit(createValue); }}
                onMouseEnter={() => setActive(rowCount - 1)}
                className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left ${clampedActive === rowCount - 1 ? 'bg-gray-100 dark:bg-gray-700' : ''}`}
              >
                <Plus className="w-3 h-3 shrink-0 text-emerald-500" />
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
  onCommit: (iso: string) => void;
  onCancel: () => void;
}

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function CalendarPopover({ value, ariaLabel, locale, placeholder, onCommit, onCancel }: CalendarPopoverProps) {
  const anchorRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const pos = useAnchoredPosition(anchorRef);
  const valid = value && ISO_DATE.test(value) ? value : '';
  const base = valid ? new Date(valid + 'T00:00:00') : new Date();
  const [view, setView] = useState({ y: base.getFullYear(), m: base.getMonth() });

  useEffect(() => { anchorRef.current?.focus(); }, []);
  useOutside([anchorRef, popRef], onCancel);

  const today = iso(new Date());
  const monthLabel = new Intl.DateTimeFormat(locale, { month: 'long', year: 'numeric' })
    .format(new Date(view.y, view.m, 1));
  // Monday-first weekday initials
  const weekdays = Array.from({ length: 7 }, (_, i) =>
    new Intl.DateTimeFormat(locale, { weekday: 'short' }).format(new Date(2024, 0, 1 + i)), // Mon 2024-01-01
  );

  const first = new Date(view.y, view.m, 1);
  const startOffset = (first.getDay() + 6) % 7; // 0 = Monday
  const daysInMonth = new Date(view.y, view.m + 1, 0).getDate();
  const cells: (Date | null)[] = [];
  for (let i = 0; i < startOffset; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(new Date(view.y, view.m, d));

  const shiftMonth = (delta: number) => setView((v) => {
    const d = new Date(v.y, v.m + delta, 1);
    return { y: d.getFullYear(), m: d.getMonth() };
  });

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
          className={`fixed p-2 ${OVERLAY_CLASS}`}
          style={{ top: pos.top, left: pos.left, width: 232 }}
        >
          <div className="mb-1 flex items-center justify-between px-1">
            <button type="button" onClick={() => shiftMonth(-1)} className="btn-icon btn-icon-ghost" aria-label="◀">
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-xs font-medium capitalize text-gray-800 dark:text-gray-100">{monthLabel}</span>
            <button type="button" onClick={() => shiftMonth(1)} className="btn-icon btn-icon-ghost" aria-label="▶">
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
          <div className="grid grid-cols-7 gap-0.5">
            {weekdays.map((w) => (
              <span key={w} className="py-1 text-center text-[10px] font-medium text-gray-400">{w}</span>
            ))}
            {cells.map((d, i) => {
              if (!d) return <span key={`e${i}`} />;
              const di = iso(d);
              const isSel = di === valid;
              const isToday = di === today;
              return (
                <button
                  key={di}
                  type="button"
                  onClick={() => onCommit(di)}
                  className={`h-7 rounded text-xs tabular-nums transition-colors
                    ${isSel ? 'bg-emerald-500 text-white font-semibold'
                      : isToday ? 'font-semibold text-emerald-600 dark:text-emerald-400 hover:bg-gray-100 dark:hover:bg-gray-700'
                      : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700'}`}
                >
                  {d.getDate()}
                </button>
              );
            })}
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
