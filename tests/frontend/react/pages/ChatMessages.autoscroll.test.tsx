/**
 * Regression: ChatMessages must auto-scroll its OWN container to the bottom on
 * new messages — never via Element.scrollIntoView(). In Safari, scrollIntoView()
 * scrolls every scrollable ancestor including the window, which scrolled the whole
 * page on each sent message and pushed the chat + input out of view. Scrolling the
 * container's scrollTop can only move that region, never the window.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import ChatMessages from '../../../../src/frontend/src/pages/ChatPage/ChatMessages';
import { renderWithRouter } from '../test-utils';
import {
  useChatContext,
  type ChatContextValue,
} from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import { buildChatContextValue } from '../test-chat-mock';

vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual<
    typeof import('../../../../src/frontend/src/pages/ChatPage/context/ChatContext')
  >('../../../../src/frontend/src/pages/ChatPage/context/ChatContext');
  return {
    ...actual,
    useChatContext: vi.fn<() => ChatContextValue>(),
  };
});

describe('ChatMessages — auto-scroll (Safari window-scroll regression)', () => {
  beforeEach(() => {
    vi.mocked(useChatContext).mockReturnValue(
      buildChatContextValue({
        messages: [
          { role: 'user', content: 'hi' },
          { role: 'assistant', content: 'hello' },
        ],
      }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('scrolls its own container, not the window via scrollIntoView', () => {
    const scrollTo = vi.spyOn(Element.prototype, 'scrollTo');
    const scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView');

    renderWithRouter(<ChatMessages />);

    // The auto-scroll happened on the container...
    expect(scrollTo).toHaveBeenCalled();
    // ...and NOT via the window-scrolling scrollIntoView (the Safari bug).
    expect(scrollIntoView).not.toHaveBeenCalled();

    // It scrolls to the bottom (top === the element's scrollHeight).
    const [arg] = scrollTo.mock.calls[0];
    expect(arg).toMatchObject({ behavior: 'smooth' });
  });
});
