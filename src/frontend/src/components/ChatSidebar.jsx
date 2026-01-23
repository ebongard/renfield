import React from 'react';
import { Plus, X, Loader } from 'lucide-react';
import ConversationItem from './ConversationItem';
import { groupConversationsByDate } from '../hooks/useChatSessions';

/**
 * Sidebar component displaying conversation history.
 * Supports mobile overlay and desktop persistent modes.
 */
export default function ChatSidebar({
  conversations,
  activeSessionId,
  onSelectConversation,
  onNewChat,
  onDeleteConversation,
  isOpen,
  onClose,
  loading
}) {
  const grouped = groupConversationsByDate(conversations);

  const periodLabels = {
    today: 'Heute',
    yesterday: 'Gestern',
    lastWeek: 'Letzte 7 Tage',
    older: 'Älter'
  };

  return (
    <>
      {/* Mobile Overlay Backdrop */}
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-20 md:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      {/* Sidebar */}
      <aside
        className={`
          fixed md:relative z-30
          w-72 h-full bg-gray-800 border-r border-gray-700
          flex flex-col
          transform transition-transform duration-300 ease-in-out
          ${isOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}
        `}
        role="navigation"
        aria-label="Konversationshistorie"
      >
        {/* Header with New Chat Button */}
        <div className="p-4 border-b border-gray-700 flex-shrink-0">
          <div className="flex items-center justify-between mb-4 md:mb-0">
            <h2 className="text-lg font-semibold text-white md:hidden">
              Konversationen
            </h2>
            <button
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-white rounded md:hidden"
              aria-label="Sidebar schließen"
            >
              <X className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>

          <button
            onClick={onNewChat}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-primary-600 hover:bg-primary-700 text-white rounded-lg transition-colors font-medium"
            aria-label="Neuen Chat starten"
          >
            <Plus className="w-4 h-4" aria-hidden="true" />
            <span>Neuer Chat</span>
          </button>
        </div>

        {/* Conversation List */}
        <div className="flex-1 overflow-y-auto" role="list" aria-label="Konversationsliste">
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <Loader className="w-6 h-6 text-gray-400 animate-spin" aria-hidden="true" />
              <span className="sr-only">Lade Konversationen...</span>
            </div>
          ) : conversations.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-gray-500">
                Keine Konversationen vorhanden
              </p>
              <p className="text-xs text-gray-600 mt-1">
                Starte einen neuen Chat
              </p>
            </div>
          ) : (
            Object.entries(grouped).map(([period, convs]) =>
              convs.length > 0 && (
                <div key={period} role="group" aria-label={periodLabels[period]}>
                  <div className="px-4 py-2 text-xs text-gray-500 uppercase tracking-wider font-medium sticky top-0 bg-gray-800">
                    {periodLabels[period]}
                  </div>
                  {convs.map(conv => (
                    <ConversationItem
                      key={conv.session_id}
                      conversation={conv}
                      isActive={conv.session_id === activeSessionId}
                      onClick={() => onSelectConversation(conv.session_id)}
                      onDelete={() => onDeleteConversation(conv.session_id)}
                    />
                  ))}
                </div>
              )
            )
          )}
        </div>
      </aside>
    </>
  );
}
