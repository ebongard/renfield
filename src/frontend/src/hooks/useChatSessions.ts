import type { Conversation, GroupedConversations } from '../types/chat';
import { toLocalDate } from '../utils/datetime';

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
    // Naive UTC from the backend: `new Date()` would read it as local time and
    // put a chat from just after local midnight into the wrong group.
    const convDate = toLocalDate(conv.updated_at || conv.created_at);

    if (!convDate) {
      groups.older.push(conv);
    } else if (convDate >= today) {
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
