/**
 * `<cite>` markup in an ASSISTANT bubble renders as a CitationChip.
 *
 * Since #1530 a check_output handler runs on single-agent web turns, and
 * Reva's wraps entity names in `<cite entity=… type=…>Label</cite>`. Card text
 * parsed that tag; the bubble printed it raw (21 literal tags in one turn).
 */
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

import ChatMessages from '../../../../src/frontend/src/pages/ChatPage/ChatMessages';
import { WissensbasisProvider } from '../../../../src/frontend/src/context/WissensbasisContext';
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
  return { ...actual, useChatContext: vi.fn<() => ChatContextValue>() };
});

type ChatMessage = ChatContextValue['messages'][number];

const ANSWER =
  'Offen sind <cite entity="JIRA-1" type="ticket">Login kaputt</cite> und ' +
  '<cite entity="../x" type="ticket">Export hängt</cite>.';

function render(messages: ChatMessage[], speakText = vi.fn(async () => undefined)) {
  vi.mocked(useChatContext).mockReturnValue(buildChatContextValue({ messages, speakText }));
  const view = renderWithRouter(
    <WissensbasisProvider>
      <ChatMessages />
    </WissensbasisProvider>,
  );
  return { ...view, speakText };
}

describe('ChatMessages — citation markup', () => {
  it('turns a cite tag into a chip and never prints the markup', () => {
    const { container } = render([
      { role: 'user', content: 'Zeige mir offene Jira-Tickets' },
      { role: 'assistant', content: ANSWER },
    ]);
    expect(screen.getByRole('button', { name: /Login kaputt/ })).toBeInTheDocument();
    expect(container.textContent).not.toContain('<cite');
    expect(container.textContent).not.toContain('</cite>');
  });

  it('renders an invalid entity id as its plain label, not a chip', () => {
    const { container } = render([{ role: 'assistant', content: ANSWER }]);
    expect(container.textContent).toContain('Export hängt');
    expect(container.textContent).not.toContain('../x');
    expect(screen.queryByRole('button', { name: /Export hängt/ })).not.toBeInTheDocument();
  });

  it('reads the labels aloud, not the markup', () => {
    const { speakText } = render([{ role: 'assistant', content: ANSWER }]);
    fireEvent.click(screen.getByRole('button', { name: /vorlesen/i }));
    expect(speakText).toHaveBeenCalledWith('Offen sind Login kaputt und Export hängt.');
  });

  it('leaves a user bubble untouched (users do not send citation markup)', () => {
    render([{ role: 'user', content: 'Was ist <cite entity="X">das</cite>?' }]);
    expect(screen.getByText('Was ist <cite entity="X">das</cite>?')).toBeInTheDocument();
  });
});
