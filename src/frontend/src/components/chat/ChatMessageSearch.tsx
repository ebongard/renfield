import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, X, Loader, MessageSquare } from 'lucide-react';

import { useMessageSearch, type MessageSearchHit } from '../../api/resources/chatSessions';

// Highlight sentinels emitted by ts_headline on the backend (STX / ETX control
// chars — see backend conversation_service._HL_START / _HL_END). The snippet is
// rendered by SPLITTING on these and wrapping matches in a real <mark> element,
// so the server text is never interpreted as HTML — no XSS surface, no
// sanitizer dependency.
const HL_START = '\u0002';
const HL_END = '\u0003';

/** Render a sentinel-delimited snippet as React nodes with <mark> highlights. */
function renderSnippet(snippet: string): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = snippet;
  let key = 0;
  while (rest.length > 0) {
    const start = rest.indexOf(HL_START);
    if (start === -1) {
      out.push(rest);
      break;
    }
    if (start > 0) out.push(rest.slice(0, start));
    const end = rest.indexOf(HL_END, start + 1);
    if (end === -1) {
      // Unbalanced sentinel — render the remainder as plain text.
      out.push(rest.slice(start + 1));
      break;
    }
    out.push(
      <mark
        key={`hl-${key++}`}
        className="bg-accent-200 dark:bg-accent-700 text-gray-900 dark:text-white rounded-sm px-0.5"
      >
        {rest.slice(start + 1, end)}
      </mark>,
    );
    rest = rest.slice(end + 1);
  }
  return out;
}

interface ChatMessageSearchProps {
  /** When set, search is restricted to this conversation; null = global. */
  scopeSessionId: string | null;
  /** Switch to a conversation + scroll to the matched message. */
  onJumpToMessage: (sessionId: string, messageIndex: number) => void;
}

const DEBOUNCE_MS = 250;

/**
 * Chat message search (chat-ui roadmap item 3).
 *
 * Debounced full-text search over the asker's own chat messages. Global
 * (cross-conversation) by default; the parent may scope it to one
 * conversation. Renders a search field, a ranked results list with
 * `<mark>`-highlighted snippets, and the required interaction states:
 * loading, a WARM empty state (not a bare "no results"), and a distinct
 * no-permission state when the search route is unauthorized. Results are
 * keyboard-navigable (↑/↓/Enter/Esc); clicking or Entering a result jumps to
 * the message and returns focus to the thread.
 */
export default function ChatMessageSearch({
  scopeSessionId,
  onJumpToMessage,
}: ChatMessageSearchProps) {
  const { t } = useTranslation();
  const [raw, setRaw] = useState('');
  const [debounced, setDebounced] = useState('');
  const [activeIndex, setActiveIndex] = useState(-1);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  // Debounce the query so we fire at most once per settled keystroke burst.
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(raw), DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [raw]);

  const trimmed = debounced.trim();
  const enabled = trimmed.length >= 2;
  const search = useMessageSearch(trimmed, scopeSessionId, enabled);

  const results: MessageSearchHit[] = useMemo(
    () => search.data?.results ?? [],
    [search.data?.results],
  );

  // Reset highlight when the result set changes.
  useEffect(() => {
    setActiveIndex(results.length > 0 ? 0 : -1);
  }, [results]);

  const status = search.error?.response?.status;
  const noPermission = status === 401 || status === 403;
  const isSearching = enabled && (search.isLoading || search.isFetching);
  const showResults = enabled && !isSearching && !search.isError && results.length > 0;
  const showEmpty = enabled && !isSearching && !search.isError && results.length === 0;

  const jump = (hit: MessageSearchHit) => {
    onJumpToMessage(hit.session_id, hit.message_index);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      if (raw) {
        setRaw('');
        setDebounced('');
      }
      return;
    }
    if (!showResults) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const hit = results[activeIndex] ?? results[0];
      if (hit) jump(hit);
    }
  };

  // Keep the highlighted row scrolled into view during keyboard nav.
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    const el = listRef.current.querySelectorAll('[role="option"]')[activeIndex] as
      | HTMLElement
      | undefined;
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  return (
    <div className="border-b border-gray-200 dark:border-gray-700 shrink-0">
      <div className="p-3">
        <label htmlFor="chat-message-search" className="sr-only">
          {scopeSessionId
            ? t('chat.search.placeholderInConversation')
            : t('chat.search.placeholderGlobal')}
        </label>
        <div className="relative">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none"
            aria-hidden="true"
          />
          <input
            id="chat-message-search"
            ref={inputRef}
            type="search"
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              scopeSessionId
                ? t('chat.search.placeholderInConversation')
                : t('chat.search.placeholderGlobal')
            }
            className="input w-full pl-9 pr-9 text-sm"
            role="combobox"
            aria-expanded={showResults}
            aria-controls="chat-message-search-results"
            aria-autocomplete="list"
            autoComplete="off"
          />
          {raw && (
            <button
              type="button"
              onClick={() => {
                setRaw('');
                setDebounced('');
                inputRef.current?.focus();
              }}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 rounded-sm"
              aria-label={t('chat.search.clear')}
            >
              <X className="w-4 h-4" aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      {/* Results / states — only rendered once a real query is active. */}
      {enabled && (
        <div
          id="chat-message-search-results"
          className="max-h-72 overflow-y-auto px-3 pb-3"
          aria-live="polite"
        >
          {/* Searching / loading */}
          {isSearching && (
            <div className="flex items-center justify-center gap-2 py-6 text-sm text-gray-500 dark:text-gray-400">
              <Loader className="w-4 h-4 animate-spin" aria-hidden="true" />
              <span>{t('chat.search.searching')}</span>
            </div>
          )}

          {/* No permission (auth required / forbidden) — distinct from no-match */}
          {!isSearching && search.isError && noPermission && (
            <div className="py-6 text-center">
              <p className="text-sm text-gray-600 dark:text-gray-300">
                {t('chat.search.noPermissionTitle')}
              </p>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                {t('chat.search.noPermissionHint')}
              </p>
            </div>
          )}

          {/* Generic error */}
          {!isSearching && search.isError && !noPermission && (
            <div className="py-6 text-center">
              <p className="text-sm text-red-600 dark:text-red-400">
                {search.errorMessage ?? t('chat.search.error')}
              </p>
            </div>
          )}

          {/* Warm zero-results state (NOT a bare "no results") */}
          {showEmpty && (
            <div className="py-8 text-center">
              <MessageSquare
                className="w-8 h-8 mx-auto mb-2 text-gray-300 dark:text-gray-600"
                aria-hidden="true"
              />
              <p className="text-sm text-gray-600 dark:text-gray-300">
                {t('chat.search.emptyTitle', { query: trimmed })}
              </p>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                {scopeSessionId
                  ? t('chat.search.emptyHintInConversation')
                  : t('chat.search.emptyHintGlobal')}
              </p>
            </div>
          )}

          {/* Results */}
          {showResults && (
            <ul
              ref={listRef}
              role="listbox"
              aria-label={t('chat.search.resultsLabel')}
              className="space-y-1"
            >
              {results.map((hit, i) => (
                <li key={`${hit.session_id}-${hit.message_index}`} role="presentation">
                  <button
                    type="button"
                    role="option"
                    aria-selected={i === activeIndex}
                    onClick={() => jump(hit)}
                    onMouseEnter={() => setActiveIndex(i)}
                    className={`w-full text-left px-2.5 py-2 rounded-lg transition-colors ${
                      i === activeIndex
                        ? 'bg-accent-50 dark:bg-accent-900/20'
                        : 'hover:bg-gray-100 dark:hover:bg-gray-700/50'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 mb-0.5">
                      <span className="text-[11px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
                        {hit.role === 'user'
                          ? t('chat.search.roleUser')
                          : t('chat.search.roleAssistant')}
                      </span>
                      {hit.timestamp && (
                        <span className="text-[11px] text-gray-400 dark:text-gray-500">
                          {new Date(hit.timestamp).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                    {/* Snippet rendered by splitting on the backend's STX/ETX
                        sentinels — never as HTML, so no XSS surface. */}
                    <p className="text-sm text-gray-700 dark:text-gray-200 line-clamp-2">
                      {renderSnippet(hit.snippet)}
                    </p>
                  </button>
                </li>
              ))}
              {search.data?.has_more && (
                <li
                  role="presentation"
                  className="px-2.5 py-2 text-center text-xs text-gray-400 dark:text-gray-500"
                >
                  {t('chat.search.moreResults')}
                </li>
              )}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
