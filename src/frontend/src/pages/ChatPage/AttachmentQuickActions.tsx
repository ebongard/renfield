import { useState, useRef, useEffect } from 'react';
import type { MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { MoreVertical, BookOpen, Send, FileSearch, Mail, Loader, Layers, Landmark } from 'lucide-react';
import apiClient from '../../utils/axios';
import type { MessageAttachment } from './context/ChatContext';

interface KnowledgeBase {
  id: string | number;
  name: string;
}

// Which submenu the user is currently viewing inside the popover.
// `null` = top-level menu; 'kb'/'both' show a KB picker; 'simba'/'simba_both'
// show the Simba category → type picker.
type SubMenu = null | 'kb' | 'both' | 'simba' | 'simba_both';

interface AttachmentQuickActionsProps {
  attachment: MessageAttachment;
  onIndexToKb: (attachmentId: string, kbId: string | number) => void;
  onSendToPaperless: (attachmentId: string) => void;
  onSendToBoth: (attachmentId: string, kbId: string | number) => void;
  onSendToSimba?: (attachmentId: string, category: string, type: string) => void;
  onSendToPaperlessAndSimba?: (attachmentId: string, category: string, type: string) => void;
  onSendViaEmail?: (attachmentId: string) => void;
  onSummarize: (attachmentId: string) => void;
  actionLoading?: Record<string, string>;
}

export default function AttachmentQuickActions({
  attachment,
  onIndexToKb,
  onSendToPaperless,
  onSendToBoth,
  onSendToSimba,
  onSendToPaperlessAndSimba,
  onSendViaEmail,
  onSummarize,
  actionLoading,
}: AttachmentQuickActionsProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  // Which submenu (KB picker) is shown inside the popover. The picker is
  // shared between "Add to KB" and "Send to Paperless + KB"; the active
  // submenu remembers which top-level action selected it so picking a
  // KB dispatches the right call.
  const [submenu, setSubmenu] = useState<SubMenu>(null);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [kbLoading, setKbLoading] = useState(false);
  // Simba (tax portal) — categories→types, availability, and the drilled-in
  // category. Only rendered when the simba MCP is reachable (xidra).
  const [simbaCategories, setSimbaCategories] = useState<Record<string, string[]>>({});
  const [simbaCategory, setSimbaCategory] = useState<string | null>(null);
  const [simbaLoading, setSimbaLoading] = useState(false);
  const [simbaAvailable, setSimbaAvailable] = useState<boolean | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const isLoading = actionLoading?.[attachment.id];
  const isDisabled = attachment.status !== 'completed' || !!isLoading;

  useEffect(() => {
    if (!open) return;
    const handleMouseDown = (e: globalThis.MouseEvent) => {
      const target = e.target as Node | null;
      if (menuRef.current && target && !menuRef.current.contains(target)) {
        setOpen(false);
        setSubmenu(null);
        setSimbaCategory(null);
      }
    };
    document.addEventListener('mousedown', handleMouseDown);
    return () => document.removeEventListener('mousedown', handleMouseDown);
  }, [open]);

  const handleToggle = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    if (isDisabled) return;
    const willOpen = !open;
    setOpen(willOpen);
    setSubmenu(null);
    setSimbaCategory(null);
    // Probe Simba availability on open so the menu items only appear on
    // instances where the simba MCP is reachable (xidra), not the household.
    if (willOpen) void ensureSimbaCategories();
  };

  // Lazy-load the Simba category→type map the first time the menu opens.
  // A 503 (simba MCP absent) sets simbaAvailable=false → the items stay hidden.
  const ensureSimbaCategories = async () => {
    if (Object.keys(simbaCategories).length > 0 || simbaAvailable === false) return;
    setSimbaLoading(true);
    try {
      const response = await apiClient.get<{ categories: Record<string, string[]> }>(
        '/api/chat/upload/simba/categories',
      );
      setSimbaCategories(response.data?.categories || {});
      setSimbaAvailable(true);
    } catch {
      setSimbaCategories({});
      setSimbaAvailable(false);
    } finally {
      setSimbaLoading(false);
    }
  };

  const handleSimba = async (e: MouseEvent<HTMLButtonElement>, both: boolean) => {
    e.stopPropagation();
    const target: SubMenu = both ? 'simba_both' : 'simba';
    if (submenu === target) {
      setSubmenu(null);
      return;
    }
    setSimbaCategory(null);
    await ensureSimbaCategories();
    setSubmenu(target);
  };

  const handleSelectSimbaType = (e: MouseEvent<HTMLButtonElement>, type: string) => {
    e.stopPropagation();
    const category = simbaCategory;
    const both = submenu === 'simba_both';
    if (!category) return;
    // Irreversible transfer to the tax accountant → explicit confirm.
    const question = both
      ? t('chat.confirmSimbaBoth', { category, type })
      : t('chat.confirmSimba', { category, type });
    // eslint-disable-next-line no-alert
    if (!window.confirm(question)) return;
    setOpen(false);
    setSubmenu(null);
    setSimbaCategory(null);
    if (both) {
      onSendToPaperlessAndSimba?.(attachment.id, category, type);
    } else {
      onSendToSimba?.(attachment.id, category, type);
    }
  };

  // Lazy-load the KB list the first time any submenu opens, then reuse
  // the cached list while the popover is open. Clearing happens on
  // popover close (via useEffect below).
  const ensureKbList = async () => {
    if (knowledgeBases.length > 0) return;
    setKbLoading(true);
    try {
      const response = await apiClient.get<KnowledgeBase[]>('/api/knowledge/bases');
      setKnowledgeBases(response.data || []);
    } catch {
      setKnowledgeBases([]);
    } finally {
      setKbLoading(false);
    }
  };

  const handleAddToKb = async (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    if (submenu === 'kb') {
      setSubmenu(null);
      return;
    }
    await ensureKbList();
    setSubmenu('kb');
  };

  const handleSendToBoth = async (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    if (submenu === 'both') {
      setSubmenu(null);
      return;
    }
    await ensureKbList();
    setSubmenu('both');
  };

  const handleSelectKb = (e: MouseEvent<HTMLButtonElement>, kbId: string | number) => {
    e.stopPropagation();
    const dispatchTarget = submenu;
    setOpen(false);
    setSubmenu(null);
    if (dispatchTarget === 'both') {
      onSendToBoth(attachment.id, kbId);
    } else {
      onIndexToKb(attachment.id, kbId);
    }
  };

  const handlePaperless = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    setOpen(false);
    onSendToPaperless(attachment.id);
  };

  const handleEmail = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    setOpen(false);
    onSendViaEmail?.(attachment.id);
  };

  const handleSummarize = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    setOpen(false);
    onSummarize(attachment.id);
  };

  if (isDisabled && !isLoading) return null;

  return (
    <div className="relative inline-flex" ref={menuRef}>
      {isLoading ? (
        <Loader className="w-3 h-3 animate-spin" aria-hidden="true" />
      ) : (
        <button
          onClick={handleToggle}
          className="p-0.5 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
          aria-label={t('chat.quickActions')}
        >
          <MoreVertical className="w-3 h-3" aria-hidden="true" />
        </button>
      )}

      {open && (
        <div className="absolute top-full right-0 mt-1 w-56 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 py-1">
          {/* Send to Paperless + KB — primary combo. Hidden when already
              indexed since the index half would no-op. */}
          {!attachment.indexed && (
            <button
              onClick={handleSendToBoth}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              <Layers className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
              {t('chat.sendToPaperlessAndKb')}
            </button>
          )}

          {/* Add to KB — hide when already indexed */}
          {!attachment.indexed && (
            <button
              onClick={handleAddToKb}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              <BookOpen className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
              {t('chat.addToKb')}
            </button>
          )}

          {/* KB sub-list, shared between 'kb' and 'both' submenus —
              `submenu` remembers which top-level action opened it so
              `handleSelectKb` can route the click to the right dispatcher. */}
          {submenu !== null && !attachment.indexed && (
            <div className="border-t border-gray-100 dark:border-gray-700 max-h-32 overflow-y-auto">
              {kbLoading ? (
                <div className="px-3 py-1.5 text-xs text-gray-400">
                  <Loader className="w-3 h-3 animate-spin inline mr-1" aria-hidden="true" />
                  {t('common.loading')}
                </div>
              ) : knowledgeBases.length === 0 ? (
                <div className="px-3 py-1.5 text-xs text-gray-400">
                  {t('common.noResults')}
                </div>
              ) : (
                knowledgeBases.map(kb => (
                  <button
                    key={kb.id}
                    onClick={(e) => handleSelectKb(e, kb.id)}
                    className="w-full text-left px-5 py-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors truncate"
                  >
                    {kb.name}
                  </button>
                ))
              )}
            </div>
          )}

          {/* Send to Paperless */}
          <button
            onClick={handlePaperless}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            <Send className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
            {t('chat.sendToPaperless')}
          </button>

          {/* Send to Simba (tax portal) — only where the simba MCP is reachable.
              Needs a category → type pick (Simba metadata) and is irreversible,
              so it drills into a picker + confirms before uploading. */}
          {simbaAvailable && onSendToSimba && (
            <button
              onClick={(e) => handleSimba(e, false)}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              <Landmark className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
              {t('chat.sendToSimba')}
            </button>
          )}
          {simbaAvailable && onSendToPaperlessAndSimba && (
            <button
              onClick={(e) => handleSimba(e, true)}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              <Layers className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
              {t('chat.sendToPaperlessAndSimba')}
            </button>
          )}

          {/* Simba category → type picker, shared by 'simba' and 'simba_both'. */}
          {(submenu === 'simba' || submenu === 'simba_both') && (
            <div className="border-t border-gray-100 dark:border-gray-700 max-h-40 overflow-y-auto">
              {simbaLoading ? (
                <div className="px-3 py-1.5 text-xs text-gray-400">
                  <Loader className="w-3 h-3 animate-spin inline mr-1" aria-hidden="true" />
                  {t('common.loading')}
                </div>
              ) : Object.keys(simbaCategories).length === 0 ? (
                <div className="px-3 py-1.5 text-xs text-gray-400">{t('common.noResults')}</div>
              ) : simbaCategory === null ? (
                Object.keys(simbaCategories).map((cat) => (
                  <button
                    key={cat}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSimbaCategory(cat);
                    }}
                    className="w-full text-left px-5 py-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors truncate flex items-center justify-between"
                  >
                    <span className="truncate">{cat}</span>
                    <span className="text-gray-400">›</span>
                  </button>
                ))
              ) : (
                <>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setSimbaCategory(null);
                    }}
                    className="w-full text-left px-3 py-1.5 text-xs text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors truncate"
                  >
                    ‹ {simbaCategory}
                  </button>
                  {(simbaCategories[simbaCategory] || []).map((tp) => (
                    <button
                      key={tp}
                      onClick={(e) => handleSelectSimbaType(e, tp)}
                      className="w-full text-left px-6 py-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors truncate"
                    >
                      {tp}
                    </button>
                  ))}
                </>
              )}
            </div>
          )}

          {/* Send via Email */}
          <button
            onClick={handleEmail}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            <Mail className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
            {t('chat.sendViaEmail')}
          </button>

          {/* Summarize */}
          <button
            onClick={handleSummarize}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            <FileSearch className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
            {t('chat.summarizeDocument')}
          </button>
        </div>
      )}
    </div>
  );
}
