/**
 * Document cards in the chat — which rendering a turn's sources get.
 *
 * `chat_document_links_enabled` (from /api/config/features) is the backend
 * flag that also makes the server write the card fields. On: one card per
 * document. Off, or a backend that does not know the flag: the chip row as
 * before. The history path (`historyToUiMessage`) feeds the same component.
 */
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

import ChatMessages from '../../../../src/frontend/src/pages/ChatPage/ChatMessages';
import { renderWithRouter } from '../test-utils';
import {
  historyToUiMessage,
  useChatContext,
  type ChatContextValue,
} from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import { buildChatContextValue } from '../test-chat-mock';
import { server } from '../mocks/server';

const BASE_URL = 'http://localhost:8000';
const PAPERLESS = 'https://paperless.example.org';

vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual<
    typeof import('../../../../src/frontend/src/pages/ChatPage/context/ChatContext')
  >('../../../../src/frontend/src/pages/ChatPage/context/ChatContext');
  return {
    ...actual,
    useChatContext: vi.fn<() => ChatContextValue>(),
  };
});

type ChatMessage = ChatContextValue['messages'][number];

const FLAGS = {
  schicht_a_extraction_enabled: false,
  wissen_workspace_enabled: false,
  chat_starters: [],
  command_palette_enabled: false,
  role_surfacing_enabled: false,
  message_search_enabled: false,
  artifacts_typed_enabled: false,
  room_handoff_enabled: false,
  chat_branching_enabled: false,
};

function serveFlags(extra: Record<string, boolean>): { served: () => boolean } {
  let hit = false;
  server.use(
    http.get(`${BASE_URL}/api/config/features`, () => {
      hit = true;
      return HttpResponse.json({ ...FLAGS, ...extra });
    }),
  );
  return { served: () => hit };
}

const SOURCES: NonNullable<ChatMessage['sources']> = [
  {
    document_id: 7, filename: 'bescheid.pdf', title: 'Steuerbescheid 2024', tier: 0,
    document_date: '2025-03-14', page_count: 3,
    paperless_url: `${PAPERLESS}/documents/70/details`,
  },
  {
    kind: 'paperless', paperless_document_id: 11, title: 'Rechnung Stadtwerke',
    created: '2025-02-02', document_type: 'Rechnung', correspondent: 'Stadtwerke',
    paperless_url: `${PAPERLESS}/documents/11/details`,
  },
];

const TURN: ChatMessage[] = [
  { id: 1, role: 'user', content: 'Wann ist die Zahlung fällig?' },
  { id: 2, role: 'assistant', content: 'Am 12. Mai.', sources: SOURCES },
];

const metaLines = () =>
  Array.from(document.querySelectorAll('[data-card-meta]')).map((el) => el.textContent);

function drive(messages: ChatMessage[]): void {
  vi.mocked(useChatContext).mockReturnValue(buildChatContextValue({ messages }));
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('ChatMessages — document cards', () => {
  it('renders one card per document when the flag is on', async () => {
    serveFlags({ chat_document_links_enabled: true });
    drive(TURN);
    renderWithRouter(<ChatMessages />);

    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(2));
    expect(metaLines()).toEqual(['14.03.2025 · 3 S.', '02.02.2025 · Rechnung · Stadtwerke']);
    expect(screen.getByRole('link', { name: 'Steuerbescheid 2024 öffnen' })).toHaveAttribute(
      'href', '/knowledge?doc=7',
    );
    // The chip row is gone: its "Quellen:" lead-in is not rendered.
    expect(screen.queryByText('Quellen:')).not.toBeInTheDocument();
  });

  it('renders the chip row as before when the flag is off', async () => {
    const flags = serveFlags({ chat_document_links_enabled: false });
    drive(TURN);
    renderWithRouter(<ChatMessages />);

    await waitFor(() => expect(flags.served()).toBe(true));
    expect(screen.getByText('Quellen:')).toBeInTheDocument();
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
    // The chip (and its Paperless segment) shows the name and no card content.
    expect(screen.getAllByRole('link', { name: /Steuerbescheid 2024/ })[0]).toHaveAttribute(
      'href', '/knowledge?doc=7',
    );
    expect(screen.queryByText(/14\.03\.2025/)).not.toBeInTheDocument();
    expect(screen.queryByText('Öffnen')).not.toBeInTheDocument();
  });

  it('renders the chip row for a backend that does not send the flag', async () => {
    const flags = serveFlags({});
    drive(TURN);
    renderWithRouter(<ChatMessages />);

    await waitFor(() => expect(flags.served()).toBe(true));
    expect(screen.getByText('Quellen:')).toBeInTheDocument();
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  it('renders no card while the answer is still streaming', async () => {
    const flags = serveFlags({ chat_document_links_enabled: true });
    drive([TURN[0], { ...TURN[1], streaming: true }]);
    renderWithRouter(<ChatMessages />);

    await waitFor(() => expect(flags.served()).toBe(true));
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  it('renders the same cards from a reloaded history', async () => {
    serveFlags({ chat_document_links_enabled: true });
    // What GET /api/chat/history serves: the sources inside `metadata`.
    const reloaded = historyToUiMessage({
      id: 2, role: 'assistant', content: 'Am 12. Mai.',
      metadata: { agent_role: 'documents', sources: SOURCES },
    });
    expect(reloaded.sources).toEqual(SOURCES);
    drive([TURN[0], reloaded]);
    renderWithRouter(<ChatMessages />);

    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(2));
    expect(metaLines()[0]).toBe('14.03.2025 · 3 S.');
    expect(
      screen.getByRole('link', { name: 'Rechnung Stadtwerke – in Paperless öffnen (neuer Tab)' }),
    ).toHaveAttribute('href', `${PAPERLESS}/documents/11/details`);
  });
});
