import { useMemo, useRef, useState, type KeyboardEvent } from 'react';

/**
 * A markdown textarea with `[[ ]]` link typeahead. When the caret sits inside an
 * unclosed `[[…`, a dropdown of matching existing note titles appears; picking
 * one (click / ↑↓+Enter) inserts `Title]]`. Purely local — suggestions come from
 * the `titles` the page already loaded. See docs/design/notes-atom.md (4B.3).
 */
export default function NoteBodyEditor({
  value,
  onChange,
  titles,
  placeholder,
  ariaLabel,
  rows = 4,
  className = 'input font-mono text-sm',
}: {
  value: string;
  onChange: (v: string) => void;
  titles: string[];
  placeholder?: string;
  ariaLabel?: string;
  rows?: number;
  className?: string;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  // `query` = the partial text after the nearest unclosed `[[` at the caret, or
  // null when the caret isn't inside a wikilink.
  const [query, setQuery] = useState<string | null>(null);
  const [active, setActive] = useState(0);

  const suggestions = useMemo(() => {
    if (query === null) return [];
    const q = query.trim().toLowerCase();
    return titles
      .filter((t) => t.toLowerCase().includes(q))
      .slice(0, 6);
  }, [query, titles]);

  // Read from the LIVE DOM value (el.value), not the closure `value`, which lags
  // a keystroke behind during rapid typing (the change handler's closure is from
  // the prior render) and would misplace the query window.
  const refreshQuery = (el: HTMLTextAreaElement) => {
    const before = el.value.slice(0, el.selectionStart);
    const m = before.match(/\[\[([^\][]*)$/);
    setQuery(m ? m[1] : null);
    setActive(0);
  };

  const insert = (title: string) => {
    const el = ref.current;
    if (!el) return;
    const live = el.value;
    const pos = el.selectionStart;
    const m = live.slice(0, pos).match(/\[\[([^\][]*)$/);
    if (!m) return;
    const start = pos - m[1].length;
    // Don't double the closing `]]` if the caret already sits before one
    // (e.g. picking a title inside a pre-closed `[[Ro]]`).
    const closer = live.slice(pos, pos + 2) === ']]' ? '' : ']]';
    const next = live.slice(0, start) + title + closer + live.slice(pos);
    onChange(next);
    setQuery(null);
    // Caret lands just past the closing `]]` either way (inserted, or the
    // pre-existing one we reused).
    const caret = start + title.length + 2;
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(caret, caret);
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (query === null || suggestions.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => (a + 1) % suggestions.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => (a - 1 + suggestions.length) % suggestions.length);
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      insert(suggestions[active]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      setQuery(null);
    }
  };

  return (
    <div className="relative">
      <textarea
        ref={ref}
        className={className}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          refreshQuery(e.target);
        }}
        onKeyDown={onKeyDown}
        onClick={(e) => refreshQuery(e.currentTarget)}
        onKeyUp={(e) => refreshQuery(e.currentTarget)}
        onBlur={() => window.setTimeout(() => setQuery(null), 150)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        rows={rows}
      />
      {suggestions.length > 0 && (
        <ul
          className="absolute z-20 mt-1 max-h-48 w-64 overflow-auto rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-lg text-sm"
          role="listbox"
        >
          {suggestions.map((s, i) => (
            <li key={s}>
              <button
                type="button"
                role="option"
                aria-selected={i === active}
                className={`block w-full truncate text-left px-3 py-1.5 ${
                  i === active
                    ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300'
                    : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                }`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  insert(s);
                }}
                onMouseEnter={() => setActive(i)}
              >
                {s}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
