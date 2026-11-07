/**
 * What stays on screen when the server replaces this tab's session id.
 *
 * The server swaps the id (`session_replaced`) when the conversation the tab
 * names is not the caller's, or was DELETED elsewhere (#1485: another tab,
 * another device, the admin cleanup). In the deleted case the tab still shows
 * the old transcript, but none of it exists any more and the new conversation
 * starts empty — leaving it on screen would suggest the next turn continues it.
 *
 * - A turn in flight (the swap arrives at its start, before any answer): keep
 *   that turn — the last user message and everything after it. It is the first
 *   turn of the new conversation.
 * - No turn in flight (the swap arrives on the socket's `register` frame):
 *   nothing on screen belongs to the new conversation.
 *
 * For a foreign or ownerless id the history was never loaded, so the screen
 * already holds only this turn and both rules leave it as it is.
 */
export function messagesAfterSessionReplaced<T extends { role: string }>(
  messages: T[],
  turnInFlight: boolean,
): T[] {
  if (!turnInFlight) return [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'user') return messages.slice(i);
  }
  return [];
}
