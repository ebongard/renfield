import React from 'react';
import { MessageSquare, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

/**
 * Single conversation item in the sidebar.
 * Shows preview text, message count, and delete button on hover.
 */
export default function ConversationItem({
  conversation,
  isActive,
  onClick,
  onDelete
}) {
  const { t } = useTranslation();

  const handleDelete = (e) => {
    e.stopPropagation();
    onDelete();
  };

  const preview = conversation.preview || t('chat.newConversation');

  return (
    <div
      className={`group flex items-center px-4 py-3 cursor-pointer transition-colors ${
        isActive
          ? 'bg-gray-100 dark:bg-gray-700'
          : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
      }`}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick();
        }
      }}
      aria-label={`${t('chat.conversationLabel')}: ${preview}`}
      aria-current={isActive ? 'true' : undefined}
    >
      <MessageSquare
        className="w-4 h-4 text-gray-400 mr-3 shrink-0"
        aria-hidden="true"
      />

      <div className="flex-1 min-w-0">
        <p className="text-sm text-gray-700 dark:text-gray-200 truncate">
          {preview}
        </p>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {t('chat.messageCount', { count: conversation.message_count })}
        </p>
      </div>

      <button
        onClick={handleDelete}
        className="opacity-0 group-hover:opacity-100 p-1.5 text-gray-400 hover:text-red-500 dark:hover:text-red-400 rounded-sm transition-all focus:opacity-100 focus:outline-hidden focus:ring-2 focus:ring-red-500/50"
        aria-label={`${t('chat.deleteConversationLabel')}: ${preview}`}
        title={t('chat.deleteConversationLabel')}
      >
        <Trash2 className="w-4 h-4" aria-hidden="true" />
      </button>
    </div>
  );
}
