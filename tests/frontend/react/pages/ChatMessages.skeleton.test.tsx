/**
 * Chat history skeleton (#125).
 *
 * While a conversation's history loads, the message list shows a skeleton of
 * the thread — alternating user/assistant bubbles — instead of a spinner over
 * an empty (or, on a switch, the PREVIOUS conversation's) list. It announces
 * itself once as a status, is decorative otherwise, carries dark variants and
 * honours reduced motion, and disappears when the messages are there.
 */
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';
import { screen, within } from '@testing-library/react';

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
  Element.prototype.scrollTo = vi.fn();
});

import ChatMessages from '../../../../src/frontend/src/pages/ChatPage/ChatMessages';
import { renderWithRouter } from '../test-utils';
import {
  useChatContext,
  type ChatContextValue,
} from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import type { ChatUiMessage } from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import { buildChatContextValue } from '../test-chat-mock';
import i18n from '../../../../src/frontend/src/i18n';

vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual<
    typeof import('../../../../src/frontend/src/pages/ChatPage/context/ChatContext')
  >('../../../../src/frontend/src/pages/ChatPage/context/ChatContext');
  return { ...actual, useChatContext: vi.fn<() => ChatContextValue>() };
});

const OLD_THREAD: ChatUiMessage[] = [
  { role: 'user', content: 'Alte Frage aus dem vorigen Gespräch' },
  { role: 'assistant', content: 'Alte Antwort aus dem vorigen Gespräch' },
];

function withContext(overrides: Partial<ChatContextValue>): void {
  vi.mocked(useChatContext).mockReturnValue(buildChatContextValue(overrides));
}

afterEach(() => vi.clearAllMocks());

describe('ChatMessages — history skeleton (#125)', () => {
  it('shows bubble placeholders on both sides while the history loads', () => {
    withContext({ messages: [], historyLoading: true });
    renderWithRouter(<ChatMessages />);

    const skeleton = screen.getByTestId('chat-skeleton');
    const bubbles = skeleton.querySelectorAll('[data-side]');
    expect(bubbles.length).toBeGreaterThanOrEqual(3);
    const sides = Array.from(bubbles).map((b) => b.getAttribute('data-side'));
    // Alternating, like a real thread — not a column of bars.
    expect(sides).toContain('user');
    expect(sides).toContain('assistant');
    for (let i = 1; i < sides.length; i++) expect(sides[i]).not.toBe(sides[i - 1]);
    // User right, assistant left — the thread's own alignment.
    for (const b of Array.from(bubbles)) {
      expect(b.className).toContain(
        b.getAttribute('data-side') === 'user' ? 'justify-end' : 'justify-start',
      );
    }
  });

  it('announces itself once, translated, and hides the decorative bubbles', () => {
    withContext({ messages: [], historyLoading: true });
    renderWithRouter(<ChatMessages />);

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent(i18n.t('chat.loadingConversation'));
    for (const b of Array.from(status.querySelectorAll('[data-side]'))) {
      expect(b).toHaveAttribute('aria-hidden', 'true');
    }
  });

  it('pulses with dark variants and stops under reduced motion', () => {
    withContext({ messages: [], historyLoading: true });
    renderWithRouter(<ChatMessages />);

    const pulsing = screen.getByTestId('chat-skeleton').querySelectorAll('.animate-pulse');
    expect(pulsing.length).toBeGreaterThan(0);
    for (const el of Array.from(pulsing)) {
      expect(el.className).toContain('motion-reduce:animate-none');
      expect(el.className).toMatch(/\bdark:bg-/);
    }
  });

  it('stands in for the previous conversation during a switch', () => {
    withContext({ messages: OLD_THREAD, historyLoading: true });
    renderWithRouter(<ChatMessages />);

    expect(screen.getByTestId('chat-skeleton')).toBeInTheDocument();
    expect(screen.queryByText('Alte Frage aus dem vorigen Gespräch')).not.toBeInTheDocument();
    // No empty-state starters either: the conversation is not empty, it is loading.
    expect(screen.queryByText(i18n.t('chat.startConversation'))).not.toBeInTheDocument();
  });

  it('disappears once the messages are loaded', () => {
    withContext({ messages: OLD_THREAD, historyLoading: false });
    renderWithRouter(<ChatMessages />);

    expect(screen.queryByTestId('chat-skeleton')).not.toBeInTheDocument();
    const log = screen.getByRole('log');
    expect(within(log).getByText('Alte Antwort aus dem vorigen Gespräch')).toBeInTheDocument();
  });
});
