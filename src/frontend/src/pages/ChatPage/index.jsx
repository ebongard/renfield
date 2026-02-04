import React from 'react';
import { useTranslation } from 'react-i18next';
import { Menu } from 'lucide-react';
import ChatSidebar from '../../components/ChatSidebar';

import ChatHeader from './ChatHeader';
import ChatMessages from './ChatMessages';
import ChatInput from './ChatInput';
import { ChatProvider, useChatContext } from './context/ChatContext';

function ChatPageLayout() {
  const { t } = useTranslation();
  const {
    sidebarOpen, setSidebarOpen,
    conversations, conversationsLoading,
    sessionId, switchConversation, startNewChat, handleDeleteConversation,
  } = useChatContext();

  return (
    <div className="h-[calc(100vh-8rem)] flex">
      {/* Mobile Sidebar Toggle Button */}
      <button
        onClick={() => setSidebarOpen(true)}
        className="fixed bottom-24 left-4 z-10 md:hidden p-3 bg-primary-600 hover:bg-primary-700 text-white rounded-full shadow-lg transition-colors"
        aria-label={t('chat.openConversations')}
      >
        <Menu className="w-5 h-5" aria-hidden="true" />
      </button>

      {/* Sidebar */}
      <ChatSidebar
        conversations={conversations}
        activeSessionId={sessionId}
        onSelectConversation={switchConversation}
        onNewChat={startNewChat}
        onDeleteConversation={handleDeleteConversation}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        loading={conversationsLoading}
      />

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col min-w-0">
        <ChatHeader />
        <ChatMessages />
        <ChatInput />
      </div>
    </div>
  );
}

export default function ChatPage() {
  return (
    <ChatProvider>
      <ChatPageLayout />
    </ChatProvider>
  );
}
