import { useTranslation } from 'react-i18next';
import { Menu } from 'lucide-react';
import ChatSidebar from '../../components/ChatSidebar';
import { WissensbasisSidePanel } from '../../components/wissensbasis/WissensbasisSidePanel';
import { WissensbasisProvider } from '../../context/WissensbasisContext';
import { useWissensbasisAvailable } from '../../api/resources/wissensbasis';

import ChatHeader from './ChatHeader';
import ChatMessages from './ChatMessages';
import ChatInput from './ChatInput';
import CommandPalette from '../../components/chat/palette/CommandPalette';
import { ChatProvider, useChatContext } from './context/ChatContext';

function ChatPageLayout() {
  const { t } = useTranslation();
  const {
    sidebarOpen, setSidebarOpen,
    conversations, conversationsLoading,
    sessionId, switchConversation, startNewChat, handleDeleteConversation,
    jumpToMessage,
  } = useChatContext();
  // Hide the side panel + FAB when the Reva backend has
  // REVA_WISSENSBASIS_ENABLED=false. Otherwise users see permanently
  // empty placeholders. ChatMessages still imports the trace query,
  // but useTraceQuery's `enabled` flag gates it on sessionId being
  // present and the route returning a real result; when the route
  // 404s, useTraceQuery falls through to empty data harmlessly.
  const wissensbasisAvailable = useWissensbasisAvailable();

  return (
    <div className="h-[calc(100vh-8rem)] flex">
      {/* Mobile Sidebar Toggle Button */}
      <button
        onClick={() => setSidebarOpen(true)}
        className="fixed bottom-36 left-4 z-10 md:hidden p-3 bg-primary-600 hover:bg-primary-700 text-white rounded-full shadow-lg transition-colors"
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
        onJumpToMessage={jumpToMessage}
      />

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col min-w-0">
        <ChatHeader />
        <ChatMessages />
        <ChatInput />
      </div>

      {/* Command palette overlay (portals to body; null unless opened). */}
      <CommandPalette />

      {/* Wissensbasis side panel — A-LANDING composed view (A2 reasoning + A4 focus).
          Mounted only when the backend route is reachable (probed once via
          useWissensbasisAvailable). When REVA_WISSENSBASIS_ENABLED=false in
          prod, the routes 404 and the panel is omitted entirely — no empty
          placeholders, no FAB clutter. */}
      {wissensbasisAvailable === true && (
        <WissensbasisSidePanel sessionId={sessionId} role={null} />
      )}
    </div>
  );
}

export default function ChatPage() {
  return (
    // Provider wraps BOTH the chat area (whose AdaptiveCardRenderer emits
    // CitationChip components consuming useWissensbasis) and the side panel
    // (which reads the same focus state). syncWithUrl=false on this surface
    // because chat-page URLs already encode the conversation; we don't want
    // every chip click pushing a `?focus=` param into the chat URL bar.
    // The standalone /wissensbasis page sets syncWithUrl=true.
    <ChatProvider>
      <WissensbasisProvider syncWithUrl={false} defaultCollapsed>
        <ChatPageLayout />
      </WissensbasisProvider>
    </ChatProvider>
  );
}
