import React, { useState, useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { MoreVertical, BookOpen, Send, FileSearch, Mail, Loader } from 'lucide-react';
import apiClient from '../../utils/axios';

export default function AttachmentQuickActions({
  attachment,
  onIndexToKb,
  onSendToPaperless,
  onSendViaEmail,
  onSummarize,
  actionLoading,
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [showKbList, setShowKbList] = useState(false);
  const [knowledgeBases, setKnowledgeBases] = useState([]);
  const [kbLoading, setKbLoading] = useState(false);
  const menuRef = useRef(null);

  const isLoading = actionLoading?.[attachment.id];
  const isDisabled = attachment.status !== 'completed' || !!isLoading;

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handleMouseDown = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setOpen(false);
        setShowKbList(false);
      }
    };
    document.addEventListener('mousedown', handleMouseDown);
    return () => document.removeEventListener('mousedown', handleMouseDown);
  }, [open]);

  const handleToggle = (e) => {
    e.stopPropagation();
    if (isDisabled) return;
    setOpen(prev => !prev);
    setShowKbList(false);
  };

  const handleAddToKb = async (e) => {
    e.stopPropagation();
    if (showKbList) {
      setShowKbList(false);
      return;
    }
    setKbLoading(true);
    try {
      const response = await apiClient.get('/api/knowledge/bases');
      setKnowledgeBases(response.data || []);
    } catch {
      setKnowledgeBases([]);
    } finally {
      setKbLoading(false);
    }
    setShowKbList(true);
  };

  const handleSelectKb = (e, kbId) => {
    e.stopPropagation();
    setOpen(false);
    setShowKbList(false);
    onIndexToKb(attachment.id, kbId);
  };

  const handlePaperless = (e) => {
    e.stopPropagation();
    setOpen(false);
    onSendToPaperless(attachment.id);
  };

  const handleEmail = (e) => {
    e.stopPropagation();
    setOpen(false);
    onSendViaEmail?.(attachment.id);
  };

  const handleSummarize = (e) => {
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
        <div className="absolute top-full right-0 mt-1 w-48 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 py-1">
          {/* Add to KB */}
          <button
            onClick={handleAddToKb}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            <BookOpen className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
            {t('chat.addToKb')}
          </button>

          {/* KB sub-list */}
          {showKbList && (
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
