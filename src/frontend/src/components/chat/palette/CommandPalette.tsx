import { useEffect, useRef, useState, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Search as SearchIcon } from 'lucide-react';
import { useChatContext } from '../../../pages/ChatPage/context/ChatContext';
import { usePaletteActions, type ResolvedPaletteAction } from './usePaletteActions';

/**
 * Chat command palette overlay (chat-ui roadmap item 4). Opened from the chat
 * composer (`/`-key when empty, or the touch button). Renders nothing unless
 * `paletteOpen`. Execution per category:
 *  - navigate → client route jump
 *  - tool     → stage the command into the composer (user reviews + sends)
 *  - set-role → stage a next-turn agent-role hint
 *
 * a11y: role=dialog/listbox, arrow/enter/esc keyboard nav, focus trap to the
 * search field, focus restored to the opener on close.
 */
export default function CommandPalette() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { paletteOpen, closePalette, setInput, setRoleHint } = useChatContext();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const { filtered } = usePaletteActions(query);
  const inputRef = useRef<HTMLInputElement>(null);
  const openerRef = useRef<Element | null>(null);

  // Capture the opener for focus restore; focus the search field on open.
  useEffect(() => {
    if (paletteOpen) {
      openerRef.current = document.activeElement;
      setQuery('');
      setActive(0);
      // focus after paint
      requestAnimationFrame(() => inputRef.current?.focus());
    } else if (openerRef.current instanceof HTMLElement) {
      openerRef.current.focus();
    }
  }, [paletteOpen]);

  // Keep the active index in range as the filtered list shrinks.
  useEffect(() => { setActive((a) => Math.min(a, Math.max(0, filtered.length - 1))); }, [filtered.length]);

  // Escape closes from anywhere while open (robust to where focus currently is),
  // as a modal should — not dependent on focus being inside the dialog.
  useEffect(() => {
    if (!paletteOpen) return;
    const onDocKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
    };
    document.addEventListener('keydown', onDocKey);
    return () => document.removeEventListener('keydown', onDocKey);
  }, [paletteOpen, closePalette]);

  const execute = useMemo(() => (action: ResolvedPaletteAction) => {
    closePalette();
    if (action.category === 'navigate' && action.to) {
      navigate(action.to);
    } else if (action.category === 'tool' && action.toolCommand !== undefined) {
      setInput(action.toolCommand);          // stage into composer (no auto-send)
    } else if (action.category === 'set-role' && action.roleId) {
      setRoleHint(action.roleId);            // next-turn hint (also closes)
    }
  }, [closePalette, navigate, setInput, setRoleHint]);

  if (!paletteOpen) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    // Escape is handled document-level (see effect above).
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, filtered.length - 1)); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); return; }
    if (e.key === 'Enter') { e.preventDefault(); if (filtered[active]) execute(filtered[active]); }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-24 px-4 bg-black/40"
      onMouseDown={(e) => { if (e.target === e.currentTarget) closePalette(); }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t('chat.palette.title')}
        className="w-full max-w-lg rounded-xl bg-white dark:bg-gray-800 shadow-xl ring-1 ring-black/5 dark:ring-white/10 overflow-hidden"
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2 px-3 border-b border-gray-200 dark:border-gray-700">
          <SearchIcon className="w-4 h-4 text-gray-400 flex-shrink-0" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-listbox"
            aria-activedescendant={filtered[active] ? `palette-opt-${filtered[active].id}` : undefined}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            placeholder={t('chat.palette.search')}
            className="flex-1 py-3 bg-transparent text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:outline-none"
          />
        </div>
        <ul id="palette-listbox" role="listbox" className="max-h-80 overflow-y-auto py-1">
          {filtered.length === 0 && (
            <li className="px-4 py-6 text-center text-sm text-gray-500 dark:text-gray-400">
              {t('chat.palette.noResults')}
            </li>
          )}
          {filtered.map((a, i) => {
            const Icon = a.icon;
            return (
              <li key={a.id} role="option" id={`palette-opt-${a.id}`} aria-selected={i === active}>
                <button
                  type="button"
                  onMouseEnter={() => setActive(i)}
                  onClick={() => execute(a)}
                  className={`w-full flex items-center gap-3 min-h-[44px] px-4 py-2 text-left text-sm ${
                    i === active
                      ? 'bg-primary-50 text-primary-800 dark:bg-primary-900/40 dark:text-primary-100'
                      : 'text-gray-700 dark:text-gray-200'
                  }`}
                >
                  <Icon className="w-4 h-4 flex-shrink-0 opacity-70" aria-hidden="true" />
                  <span className="truncate">{a.label}</span>
                  <span className="ml-auto text-[10px] uppercase tracking-wide text-gray-400">
                    {t(`chat.palette.categories.${a.category === 'set-role' ? 'setRole' : a.category}`)}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>,
    document.body,
  );
}
