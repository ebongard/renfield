import { useRef, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { ReactElement } from 'react';
import { useTranslation } from 'react-i18next';
import { Volume2, Loader, FileText, AlertCircle, CheckCircle, Search, CheckCircle2, XCircle, ChevronRight, ChevronLeft, Radio, Pencil, RotateCcw, Trash2 } from 'lucide-react';
import AdaptiveCardRenderer from '../../components/AdaptiveCardRenderer';
import IntentCorrectionButton from '../../components/IntentCorrectionButton';
import AttachmentQuickActions from './AttachmentQuickActions';
import EmailForwardDialog from './EmailForwardDialog';
import PaperlessConfirmCard from './PaperlessConfirmCard';
import SourceChips from '../../components/chat/SourceChips';
import FollowupChips from '../../components/chat/FollowupChips';
import MediaHandoffIndicator from '../../components/chat/MediaHandoffIndicator';
import AgentRoleBadge from '../../components/chat/AgentRoleBadge';
import ArtifactRenderer from '../../components/chat/artifacts/ArtifactRenderer';
import { useFeatureFlags } from '../../api/resources/brain';
import { useChatContext } from './context/ChatContext';
import { CitationChip } from '../../components/wissensbasis/CitationChip';
import { useTraceQuery, type TraceEntity } from '../../api/resources/wissensbasis';

const IMAGE_URL_RE = /https?:\/\/[^\s)]+?\/Items\/[^\s)]+?\/Images\/[^\s)]+|https?:\/\/[^\s)]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s)]*)?/i;

function isImageUrl(url: string): boolean {
  return IMAGE_URL_RE.test(url);
}

type ContentPart =
  | { type: 'text'; content: string }
  | { type: 'link'; label: string; url: string }
  | { type: 'image'; url: string };

function renderTextWithChips(text: string, entities: TraceEntity[], keyPrefix: string): ReactNode[] {
  if (!text || entities.length === 0) {
    return [text];
  }
  const candidates = entities
    .filter((e) => {
      const d = (e.display_name || '').trim();
      const id = (e.entity_id || '').trim();
      if (!d || !id || d.length < 3) return false;
      if (d === id && /^\d+$/.test(d)) return false;
      return true;
    })
    .sort((a, b) => b.display_name.length - a.display_name.length);
  if (candidates.length === 0) return [text];

  const escaped = candidates.map((c) => c.display_name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const pattern = new RegExp('(?<![A-Za-z0-9_])(' + escaped.join('|') + ')(?![A-Za-z0-9_])', 'g');
  const byDisplay = new Map(candidates.map((c) => [c.display_name, c]));

  const out: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      out.push(text.slice(lastIndex, match.index));
    }
    const ent = byDisplay.get(match[1]);
    if (ent) {
      out.push(
        <CitationChip
          key={`${keyPrefix}-chip-${key++}`}
          entity={ent.entity_id}
          label={ent.display_name}
          entityType={ent.entity_type}
        />,
      );
    } else {
      out.push(match[0]);
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    out.push(text.slice(lastIndex));
  }
  return out;
}

function renderMessageContent(text: string, imageAlt: string, entities: TraceEntity[] = [], msgKey = 'm'): ReactElement {
  // Combined pattern: markdown links [text](url), image URLs, or plain URLs
  const pattern = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|https?:\/\/[^\s)]+?\/Items\/[^\s)]+?\/Images\/[^\s)]+|https?:\/\/[^\s)]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s)]*)?|https?:\/\/[^\s)]+/gi;

  const parts: ContentPart[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: 'text', content: text.slice(lastIndex, match.index) });
    }
    if (match[1] && match[2]) {
      // Markdown link [text](url)
      parts.push({ type: 'link', label: match[1], url: match[2] });
    } else if (isImageUrl(match[0])) {
      parts.push({ type: 'image', url: match[0] });
    } else {
      // Plain URL
      parts.push({ type: 'link', label: match[0], url: match[0] });
    }
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push({ type: 'text', content: text.slice(lastIndex) });
  }

  if (parts.length === 1 && parts[0].type === 'text') {
    return (
      <p className="whitespace-pre-wrap">
        {renderTextWithChips(text, entities, msgKey)}
      </p>
    );
  }

  return (
    <div className="whitespace-pre-wrap">
      {parts.map((part, i) =>
        part.type === 'image' ? (
          <img
            key={i}
            src={part.url}
            alt={imageAlt}
            className="rounded-lg max-w-[200px] max-h-[200px] my-2 shadow-md"
            loading="lazy"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
          />
        ) : part.type === 'link' ? (
          <a
            key={i}
            href={part.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary-600 dark:text-primary-400 underline hover:text-primary-800 dark:hover:text-primary-300"
          >{part.label}</a>
        ) : (
          <span key={i}>{renderTextWithChips(part.content, entities, `${msgKey}-${i}`)}</span>
        )
      )}
    </div>
  );
}

export default function ChatMessages() {
  const { t } = useTranslation();
  const {
    messages, loading, historyLoading, speakText, handleFeedbackSubmit,
    regenerateWithCorrectedIntent,
    actionLoading, actionResult, indexToKb, sendToPaperless, sendToBoth, handleSummarize,
    handleSendViaEmail, emailDialog, confirmSendViaEmail, cancelEmailDialog,
    sendMessage, sessionId, submitPaperlessConfirm,
    pendingScrollIndex, clearPendingScroll,
    editAndResubmit, regenerateTurn, switchBranch, deleteBranch, sendDeviceAction,
  } = useChatContext();
  const { data: features } = useFeatureFlags();
  const roleSurfacingEnabled = features?.role_surfacing_enabled ?? false;
  const artifactsEnabled = features?.artifacts_typed_enabled ?? false;
  // Chat branching (Phase 1): edit/regenerate fork affordances. Dark by default.
  const branchingEnabled = features?.chat_branching_enabled ?? false;
  // Index of the user message currently being edited inline (null = none).
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState('');

  // Phase 2: fork-from-ANY message — any user turn is editable, any finished
  // assistant turn is regenerable (the per-message ‹n/m› switcher below lets the
  // user navigate the resulting sibling branches).
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  // Per-message element refs so a search jump can scroll/focus a specific turn.
  const messageRefs = useRef<Array<HTMLDivElement | null>>([]);
  // Briefly highlighted message index (the jump target's ring), cleared on a timer.
  const [flashIndex, setFlashIndex] = useState<number | null>(null);

  // Fetch the wissensbasis reasoning trace for this session so we can
  // wrap entity mentions in the assistant prose with CitationChips.
  // The trace is rebuilt server-side on every agent turn (drained from
  // the wb_annotations accumulator) so the entity list stays current.
  // useTraceQuery gates on sessionId — null/missing returns empty data.
  const traceQ = useTraceQuery(sessionId);
  const chipEntities = useMemo(
    () => traceQ.data?.trace?.entities ?? [],
    [traceQ.data?.trace?.entities],
  );

  // Auto-scroll to bottom when messages change. Scroll the CONTAINER directly
  // rather than calling scrollIntoView() on a sentinel: in Safari, scrollIntoView
  // walks up and scrolls every scrollable ancestor — including the window — so on
  // each new message it scrolled the whole page and pushed the chat + input out of
  // view. Scrolling the container's scrollTop can only move this region, never the
  // window, so it fixes Safari and behaves identically elsewhere.
  useEffect(() => {
    const el = scrollContainerRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  // Jump-to-message (chat-ui item 3): when a search result arms
  // pendingScrollIndex AND the target message is now loaded, scroll it into
  // view, focus it (so keyboard focus returns to the thread, not stranded in
  // the sidebar search field), flash a highlight ring, then clear the pending
  // index so a later auto-scroll on new messages isn't fought. Out-of-range
  // indices (history shorter than expected) are cleared without scrolling.
  useEffect(() => {
    if (pendingScrollIndex === null) return;
    if (historyLoading) return; // wait until the switched history has loaded
    const target = messageRefs.current[pendingScrollIndex];
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.focus({ preventScroll: true });
      setFlashIndex(pendingScrollIndex);
      const tid = window.setTimeout(() => setFlashIndex(null), 2000);
      clearPendingScroll();
      return () => window.clearTimeout(tid);
    }
    // Index not present (stale/short history) — give up gracefully.
    clearPendingScroll();
  }, [pendingScrollIndex, historyLoading, messages, clearPendingScroll]);

  return (
    <div
      ref={scrollContainerRef}
      className="flex-1 overflow-y-auto card space-y-4 mb-4 mx-4 md:mx-0"
      role="log"
      aria-live="polite"
      aria-label={t('chat.conversations')}
      aria-relevant="additions"
    >
      {/* History Loading State */}
      {historyLoading && (
        <div className="flex items-center justify-center py-8">
          <Loader className="w-6 h-6 text-gray-500 dark:text-gray-400 animate-spin mr-2" aria-hidden="true" />
          <span className="text-gray-500 dark:text-gray-400">{t('chat.loadingConversation')}</span>
        </div>
      )}

      {/* Empty State */}
      {!historyLoading && messages.length === 0 && (
        <div className="text-center py-16">
          <img src="/logo-icon.svg" alt="" className="w-20 h-20 mx-auto mb-6 opacity-30" aria-hidden="true" />
          <h2 className="font-display text-2xl text-gray-400 dark:text-gray-500 mb-2">{t('chat.startConversation')}</h2>
          <p className="text-sm text-gray-400 dark:text-gray-500 mb-6">
            {t('chat.useTextOrMic')}
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            {(((): string[] => {
              try {
                const custom = import.meta.env.VITE_CHAT_STARTERS;
                if (custom) return JSON.parse(custom) as string[];
              } catch { /* fall through */ }
              return [t('chat.exampleWeather'), t('chat.exampleLight'), t('chat.exampleMusic')];
            })()).map((example: string) => (
              <button
                key={example}
                onClick={() => sendMessage?.(example, false)}
                className="px-4 py-2 rounded-full text-sm border border-gray-300 dark:border-gray-600 text-gray-500 dark:text-gray-400 hover:border-accent-400 hover:text-accent-600 dark:hover:border-accent-500 dark:hover:text-accent-400 transition-colors"
              >
                {example}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Messages */}
      {messages.map((message, index) => (
        <div
          key={index}
          ref={(el) => { messageRefs.current[index] = el; }}
          tabIndex={-1}
          className={`flex outline-none scroll-mt-4 ${message.role === 'user' ? 'justify-end' : 'justify-start'} ${
            flashIndex === index
              ? 'rounded-lg ring-2 ring-accent-400 dark:ring-accent-500 ring-offset-2 ring-offset-white dark:ring-offset-gray-800 transition-shadow'
              : ''
          }`}
          role="article"
          aria-label={message.role === 'user' ? t('chat.yourMessage') : t('chat.assistantResponse')}
        >
          <div
            className={`max-w-[70%] px-4 py-2 rounded-lg ${
              message.role === 'user'
                ? 'bg-primary-600 text-white'
                : 'bg-gray-100 text-gray-900 dark:bg-gray-700 dark:text-gray-100'
            }`}
          >
            {/* Agent Steps (collapsible) */}
            {message.agentSteps && message.agentSteps.length > 0 && (() => {
              // Bind to a local so the narrowing survives the IIFE boundary —
              // TypeScript loses the `agentSteps` non-null narrowing across the
              // arrow function call inside `.some()`.
              const agentSteps = message.agentSteps;
              const toolCalls = agentSteps.filter(s => s.type === 'tool_call');
              const results = agentSteps.filter(s => s.type === 'tool_result');
              const hasError = results.some(s => !s.success);
              const isStillRunning = toolCalls.some(
                tc => !agentSteps.find(s => s.type === 'tool_result' && s.step === tc.step)
              );

              return (
                <details className="mb-2 group" open={isStillRunning || undefined}>
                  <summary className="flex items-center gap-1.5 text-sm cursor-pointer select-none list-none text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
                    <ChevronRight className="w-4 h-4 flex-shrink-0 transition-transform group-open:rotate-90" aria-hidden="true" />
                    {isStillRunning ? (
                      <>
                        <Loader className="w-4 h-4 animate-spin text-accent-500 dark:text-accent-400" aria-hidden="true" />
                        <span>{t('chat.agentThinking')}</span>
                      </>
                    ) : (
                      <>
                        {hasError
                          ? <XCircle className="w-4 h-4 flex-shrink-0 text-red-500 dark:text-red-400" aria-hidden="true" />
                          : <CheckCircle2 className="w-4 h-4 flex-shrink-0 text-green-500 dark:text-green-400" aria-hidden="true" />
                        }
                        <span>{t('chat.agentStepsCount', { count: toolCalls.length })}</span>
                      </>
                    )}
                  </summary>
                  <div className="mt-1.5 ml-5 space-y-1 border-l-2 border-accent-400 dark:border-accent-600 pl-2.5 bg-gray-100/50 dark:bg-gray-800/50 rounded-lg p-2.5">
                    {message.agentSteps.map((step, stepIdx) => (
                      <div key={stepIdx} className="flex items-start gap-1.5 text-sm">
                        {step.type === 'tool_call' && (
                          <>
                            <Search className="w-4 h-4 mt-0.5 flex-shrink-0 text-accent-500 dark:text-accent-400" aria-hidden="true" />
                            <span className="text-gray-600 dark:text-gray-300">
                              <span className="font-medium">{step.tool?.split('.').pop()}</span>
                              {step.reason && <span className="ml-1 text-gray-400 dark:text-gray-500">— {step.reason}</span>}
                            </span>
                            {!agentSteps.find(s => s.type === 'tool_result' && s.step === step.step) && (
                              <Loader className="w-4 h-4 mt-0.5 animate-spin text-accent-500 dark:text-accent-400" aria-hidden="true" />
                            )}
                          </>
                        )}
                        {step.type === 'tool_result' && (
                          <>
                            {step.success
                              ? <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0 text-green-500 dark:text-green-400" aria-hidden="true" />
                              : <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0 text-red-500 dark:text-red-400" aria-hidden="true" />
                            }
                            <span className={`${step.success ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                              {step.tool?.split('.').pop()}{step.success ? '' : ` — ${step.message || 'failed'}`}
                            </span>
                          </>
                        )}
                      </div>
                    ))}
                  </div>
                </details>
              );
            })()}

            {/* F4c — federation progress: one status line per remote peer */}
            {message.federationProgress && Object.keys(message.federationProgress).length > 0 && (
              <ul className="mb-2 space-y-1" aria-live="polite">
                {Object.entries(message.federationProgress).map(([pubkey, entry]) => (
                  <li
                    key={pubkey}
                    className="flex items-center gap-1.5 text-sm text-gray-600 dark:text-gray-300"
                  >
                    <Radio className="w-4 h-4 flex-shrink-0 text-accent-500 dark:text-accent-400 animate-pulse" aria-hidden="true" />
                    <span>
                      {t(`chat.federationProgress.${entry.label}`, {
                        name: entry.peer_display_name,
                        defaultValue: t('chat.federationProgress.fallback', { name: entry.peer_display_name }),
                      })}
                    </span>
                  </li>
                ))}
              </ul>
            )}

            {message.role === 'assistant'
              ? renderMessageContent(message.content, t('chat.albumArt'), message.entities ?? chipEntities, `msg-${index}`)
              : editingIndex === index ? (
                /* Inline edit-and-resubmit editor (chat branching, Phase 1). */
                <div className="flex flex-col gap-2">
                  <textarea
                    className="input w-full text-gray-900 dark:text-gray-100"
                    rows={Math.min(6, Math.max(2, editDraft.split('\n').length))}
                    value={editDraft}
                    autoFocus
                    aria-label={t('chat.editMessage')}
                    onChange={(e) => setEditDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        if (editDraft.trim()) {
                          editAndResubmit(index, editDraft);
                          setEditingIndex(null);
                        }
                      } else if (e.key === 'Escape') {
                        e.preventDefault();
                        setEditingIndex(null);
                      }
                    }}
                  />
                  <div className="flex gap-2 justify-end">
                    <button
                      type="button"
                      className="btn-secondary text-xs px-3 py-1"
                      onClick={() => setEditingIndex(null)}
                    >
                      {t('common.cancel')}
                    </button>
                    <button
                      type="button"
                      className="btn-primary text-xs px-3 py-1"
                      disabled={!editDraft.trim()}
                      onClick={() => {
                        if (editDraft.trim()) {
                          editAndResubmit(index, editDraft);
                          setEditingIndex(null);
                        }
                      }}
                    >
                      {t('chat.resubmit')}
                    </button>
                  </div>
                </div>
              ) : (
                <p className="whitespace-pre-wrap">{message.content}</p>
              )}

            {/* Branch switcher (chat branching, Phase 2): ◂ n/m ▸ + delete, on
                any message that has sibling branches. Keyboard-reachable;
                role-aware colors so contrast holds on the user bubble too. */}
            {branchingEnabled && message.branch && message.branch.count > 1
              && typeof message.id === 'number' && !loading && (() => {
              const onUserBubble = message.role === 'user';
              const navClass = onUserBubble
                ? 'text-white/80 hover:bg-white/20 focus:ring-white/50'
                : 'text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700 focus:ring-accent-400';
              const br = message.branch;
              const msgId = message.id;
              return (
                <div
                  className={`mt-2 inline-flex items-center gap-1 text-xs ${onUserBubble ? 'text-white/80' : 'text-gray-500 dark:text-gray-400'}`}
                  role="group"
                  aria-label={t('chat.branch.group')}
                >
                  <button
                    type="button"
                    onClick={() => { const p = br.sibling_ids[br.index - 1]; if (typeof p === 'number') void switchBranch(p); }}
                    disabled={br.index === 0}
                    className={`p-1.5 rounded disabled:opacity-30 disabled:cursor-not-allowed focus:outline-none focus:ring-2 ${navClass}`}
                    aria-label={t('chat.branch.previous')}
                  >
                    <ChevronLeft className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <span className="tabular-nums px-0.5" aria-live="polite">
                    {t('chat.branch.position', { current: br.index + 1, total: br.count })}
                  </span>
                  <button
                    type="button"
                    onClick={() => { const n = br.sibling_ids[br.index + 1]; if (typeof n === 'number') void switchBranch(n); }}
                    disabled={br.index === br.count - 1}
                    className={`p-1.5 rounded disabled:opacity-30 disabled:cursor-not-allowed focus:outline-none focus:ring-2 ${navClass}`}
                    aria-label={t('chat.branch.next')}
                  >
                    <ChevronRight className="w-4 h-4" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const neighbor = br.sibling_ids[br.index - 1] ?? br.sibling_ids[br.index + 1];
                      if (typeof neighbor === 'number' && window.confirm(t('chat.branch.deleteConfirm'))) {
                        void deleteBranch(msgId, neighbor);
                      }
                    }}
                    className={`p-1.5 ml-1 rounded focus:outline-none focus:ring-2 ${onUserBubble ? 'text-white/80 hover:bg-white/20 focus:ring-white/50' : 'text-gray-400 hover:text-red-500 dark:hover:text-red-400 hover:bg-gray-200 dark:hover:bg-gray-700 focus:ring-red-400'}`}
                    aria-label={t('chat.branch.delete')}
                  >
                    <Trash2 className="w-3.5 h-3.5" aria-hidden="true" />
                  </button>
                </div>
              );
            })()}

            {/* Edit affordance (chat branching): any user message (Phase 2 —
                fork-from-any), dark behind chat_branching_enabled. Keyboard-
                reachable (a real focusable button, not hover-only). */}
            {branchingEnabled && message.role === 'user'
              && editingIndex !== index && !loading && (
              <button
                type="button"
                onClick={() => { setEditingIndex(index); setEditDraft(message.content); }}
                className="mt-2 text-xs text-white/80 hover:text-white flex items-center gap-1 focus:outline-none focus:ring-2 focus:ring-white/50 rounded"
                aria-label={t('chat.editMessage')}
              >
                <Pencil className="w-3 h-3" aria-hidden="true" />
                <span>{t('chat.edit')}</span>
              </button>
            )}

            {/* Provenance source chips — KB documents a knowledge-backed answer
                used. Renders nothing when the turn had no sources. */}
            {message.role === 'assistant' && !message.streaming && (
              <SourceChips sources={message.sources} />
            )}

            {/* Follow-up suggestion chips — only under the LAST finished
                assistant turn (ephemeral; tapping fills the composer). */}
            {message.role === 'assistant' && !message.streaming && index === messages.length - 1 && (
              <FollowupChips followups={message.suggestedFollowups} />
            )}

            {/* Adaptive Card (from WebSocket card message) */}
            {message.card && (
              <div className="mt-2">
                <AdaptiveCardRenderer card={message.card} />
              </div>
            )}

            {/* Typed artifacts (Lane A: table/list/keyvalue/chart). Inert when
                the feature flag is off. Each renders in arrival order. `loading`
                = the artifact is still streaming (partial); `finalized` = the
                turn finished, so a stuck-partial resolves to the fallback. */}
            {artifactsEnabled && message.role === 'assistant' && message.artifacts?.map((artifact) => (
              <ArtifactRenderer
                key={artifact.id}
                artifact={artifact}
                loading={artifact.partial === true && message.streaming === true}
                finalized={message.streaming !== true}
                onDeviceAction={sendDeviceAction}
              />
            ))}

            {/* Interactive Paperless cold-start confirm card */}
            {message.paperlessConfirm && (
              <PaperlessConfirmCard
                key={message.paperlessConfirm.confirmToken}
                confirmToken={message.paperlessConfirm.confirmToken}
                filename={message.paperlessConfirm.filename}
                summary={message.paperlessConfirm.summary}
                fields={message.paperlessConfirm.fields}
                status={message.paperlessConfirm.status}
                onSubmit={(token, decisions) => submitPaperlessConfirm(token, { decisions })}
                onAbort={(token) => submitPaperlessConfirm(token, { abort: true })}
              />
            )}

            {/* Attachment chips */}
            {message.attachments && message.attachments.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {message.attachments.map(att => (
                  <div
                    key={att.id}
                    className={`flex items-center space-x-1 px-2 py-1 rounded text-xs ${
                      att.status === 'completed'
                        ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                        : 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
                    }`}
                  >
                    <FileText className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
                    <span className="truncate max-w-[140px]">{att.filename}</span>
                    {att.file_size && (
                      <span className="text-[10px] opacity-70">
                        ({att.file_size < 1024 * 1024
                          ? `${Math.round(att.file_size / 1024)} KB`
                          : `${(att.file_size / (1024 * 1024)).toFixed(1)} MB`
                        })
                      </span>
                    )}
                    {att.indexing
                      ? <span title={t('chat.documentIndexing')} className="inline-flex"><Loader className="w-3 h-3 flex-shrink-0 animate-spin" aria-hidden="true" /></span>
                      : att.indexed
                        ? <span className="inline-flex items-center px-1 rounded text-[9px] font-semibold bg-green-200 text-green-900 dark:bg-green-800 dark:text-green-100" title={t('chat.documentIndexed')}>KB</span>
                        : att.status === 'completed'
                          ? <CheckCircle className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
                          : <AlertCircle className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
                    }
                    <AttachmentQuickActions
                      attachment={att}
                      onIndexToKb={indexToKb}
                      onSendToPaperless={sendToPaperless}
                      onSendToBoth={sendToBoth}
                      onSendViaEmail={handleSendViaEmail}
                      onSummarize={handleSummarize}
                      actionLoading={actionLoading}
                    />
                  </div>
                ))}
              </div>
            )}

            {/* TTS Button for assistant messages */}
            {message.role === 'assistant' && !message.streaming && speakText && (
              <button
                onClick={() => speakText(message.content)}
                className="mt-2 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-white flex items-center space-x-1"
                aria-label={t('chat.readAloud')}
              >
                <Volume2 className="w-3 h-3" aria-hidden="true" />
                <span>{t('chat.readAloud')}</span>
              </button>
            )}

            {/* Regenerate affordance (chat branching): any finished assistant
                turn (Phase 2 — fork-from-any), dark behind chat_branching_enabled.
                Keyboard-reachable. Re-runs the same user query → new sibling. */}
            {branchingEnabled && message.role === 'assistant' && !message.streaming
              && !loading && (
              <button
                type="button"
                onClick={() => regenerateTurn(index)}
                className="mt-2 ml-3 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-white inline-flex items-center gap-1 focus:outline-none focus:ring-2 focus:ring-accent-400 rounded"
                aria-label={t('chat.regenerate')}
              >
                <RotateCcw className="w-3 h-3" aria-hidden="true" />
                <span>{t('chat.regenerate')}</span>
              </button>
            )}

            {/* Agent-role badge (item 6) — which role answered; tap to pin next turn. */}
            {message.role === 'assistant' && !message.streaming && roleSurfacingEnabled && message.agentRole && (
              <div className="mt-2">
                <AgentRoleBadge role={message.agentRole} />
              </div>
            )}

            {/* Intent info + Correction Button */}
            {message.role === 'assistant' && !message.streaming && message.intentInfo && (
              <IntentCorrectionButton
                messageText={message.userQuery || ''}
                detectedIntent={message.intentInfo.intent}
                detectedConfidence={message.intentInfo.confidence}
                feedbackType="intent"
                onCorrect={handleFeedbackSubmit}
                onRegenerate={regenerateWithCorrectedIntent}
                proactive={message.feedbackRequested === true}
              />
            )}
          </div>
        </div>
      ))}

      {/* Room-handoff affordance (item 8): a quiet, transient inline meta line
          when Media Follow moves the user's playback to the room they entered.
          Self-gated on the room_handoff_enabled feature flag; renders nothing
          when off or idle. Sits in the thread flow, not as a floating overlay. */}
      <MediaHandoffIndicator />

      {/* Loading Indicator */}
      {loading && (
        <div className="flex justify-start" role="status" aria-label="Renfield denkt nach">
          <div className="bg-gray-200 dark:bg-gray-700 px-4 py-3 rounded-lg flex items-center space-x-1.5">
            <span className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 animate-typing-dot" />
            <span className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 animate-typing-dot" />
            <span className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 animate-typing-dot" />
            <span className="sr-only">{t('chat.thinkingStatus')}</span>
          </div>
        </div>
      )}

      {/* Quick action result toast */}
      {actionResult && (
        <div
          className={`mx-auto px-3 py-1.5 rounded text-xs font-medium ${
            actionResult.success
              ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
              : 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
          }`}
          role="status"
        >
          {actionResult.success
            ? (actionResult.type === 'indexing' ? t('chat.indexingSuccess')
              : actionResult.type === 'email' ? t('chat.emailSuccess')
              : actionResult.type === 'both' ? t('chat.sendToPaperlessAndKbSuccess')
              : t('chat.paperlessSuccess'))
            : (actionResult.type === 'indexing' ? t('chat.indexingFailed')
              : actionResult.type === 'email' ? t('chat.emailFailed')
              : actionResult.type === 'both' ? t('chat.sendToPaperlessAndKbFailed', { detail: actionResult.message })
              : t('chat.paperlessFailed'))
          }
        </div>
      )}

      {/* Email Forward Dialog */}
      {emailDialog && (
        <EmailForwardDialog
          open={!!emailDialog}
          filename={emailDialog.filename}
          onConfirm={confirmSendViaEmail}
          onCancel={cancelEmailDialog}
        />
      )}
    </div>
  );
}
