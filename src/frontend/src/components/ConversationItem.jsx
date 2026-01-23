import React from 'react';
import { MessageSquare, Trash2 } from 'lucide-react';

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
  const handleDelete = (e) => {
    e.stopPropagation();
    onDelete();
  };

  return (
    <div
      className={`group flex items-center px-4 py-3 cursor-pointer transition-colors ${
        isActive
          ? 'bg-gray-700'
          : 'hover:bg-gray-700/50'
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
      aria-label={`Konversation: ${conversation.preview || 'Neue Konversation'}`}
      aria-current={isActive ? 'true' : undefined}
    >
      <MessageSquare
        className="w-4 h-4 text-gray-400 mr-3 flex-shrink-0"
        aria-hidden="true"
      />

      <div className="flex-1 min-w-0">
        <p className="text-sm text-gray-200 truncate">
          {conversation.preview || 'Neue Konversation'}
        </p>
        <p className="text-xs text-gray-500">
          {conversation.message_count} {conversation.message_count === 1 ? 'Nachricht' : 'Nachrichten'}
        </p>
      </div>

      <button
        onClick={handleDelete}
        className="opacity-0 group-hover:opacity-100 p-1.5 text-gray-400 hover:text-red-400 rounded transition-all focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-red-500/50"
        aria-label={`Konversation löschen: ${conversation.preview || 'Neue Konversation'}`}
        title="Konversation löschen"
      >
        <Trash2 className="w-4 h-4" aria-hidden="true" />
      </button>
    </div>
  );
}
