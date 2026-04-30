import type { Conversation, GroupedConversations } from '../types/chat';

export { useChatSessions } from '../api/resources/chatSessions';
export { useChatSessions as default } from '../api/resources/chatSessions';

/**
 * Group conversations by date periods
 */
export function groupConversationsByDate(conversations: Conversation[]): GroupedConversations {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const lastWeek = new Date(today);
  lastWeek.setDate(today.getDate() - 7);

  const groups: GroupedConversations = {
    today: [],
    yesterday: [],
    lastWeek: [],
    older: [],
  };

  conversations.forEach((conv) => {
    const convDate = new Date(conv.updated_at || conv.created_at);

    if (convDate >= today) {
      groups.today.push(conv);
    } else if (convDate >= yesterday) {
      groups.yesterday.push(conv);
    } else if (convDate >= lastWeek) {
      groups.lastWeek.push(conv);
    } else {
      groups.older.push(conv);
    }
  });

  return groups;
}
