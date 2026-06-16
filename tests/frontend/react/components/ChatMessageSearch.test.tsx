/**
 * ChatMessageSearch — chat message search (chat-UI roadmap item 3).
 *
 * Covers the required interaction states: a real search renders ranked
 * results, the WARM zero-results empty state (not a bare "no results"), and
 * clicking a result calls onJumpToMessage(session_id, message_index). Search
 * goes through React Query → MSW. German is the test default.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import ChatMessageSearch from '../../../../src/frontend/src/components/chat/ChatMessageSearch';
import { renderWithRouter } from '../test-utils';
import { server } from '../mocks/server';

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const SEARCH_URL = `${BASE_URL}/api/chat/messages/search`;

const HL_START = '\u0002';
const HL_END = '\u0003';

function mockSearch(results: unknown[], hasMore = false) {
  server.use(
    http.get(SEARCH_URL, ({ request }) => {
      const q = new URL(request.url).searchParams.get('q') ?? '';
      return HttpResponse.json({ query: q, results, count: results.length, has_more: hasMore });
    }),
  );
}

describe('ChatMessageSearch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the search field (no query → no results region)', () => {
    renderWithRouter(<ChatMessageSearch scopeSessionId={null} onJumpToMessage={vi.fn()} />);
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    // Results region only renders once a real (≥2 char) query is active.
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('renders ranked results with the matched snippet', async () => {
    mockSearch([
      {
        session_id: 's-abc',
        message_index: 2,
        role: 'user',
        content: 'Schalte das Licht ein',
        snippet: `Schalte das ${HL_START}Licht${HL_END} ein`,
        timestamp: '2026-06-15T10:00:00Z',
        rank: 0.5,
      },
    ]);
    renderWithRouter(<ChatMessageSearch scopeSessionId={null} onJumpToMessage={vi.fn()} />);

    await userEvent.type(screen.getByRole('combobox'), 'Licht');

    const options = await screen.findAllByRole('option');
    expect(options).toHaveLength(1);
    // The highlighted term renders inside a <mark>, the sentinels are stripped.
    const mark = options[0].querySelector('mark');
    expect(mark).not.toBeNull();
    expect(mark?.textContent).toBe('Licht');
    expect(options[0].textContent).not.toContain(HL_START);
  });

  it('shows a WARM zero-results state (not a bare "no results")', async () => {
    mockSearch([]);
    renderWithRouter(<ChatMessageSearch scopeSessionId={null} onJumpToMessage={vi.fn()} />);

    await userEvent.type(screen.getByRole('combobox'), 'zzznope');

    // Warm copy references the query + offers a constructive hint, and there
    // is no results listbox.
    await waitFor(() => {
      expect(screen.getByText(/zzznope/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Schreibweise/)).toBeInTheDocument();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('clicking a result calls onJumpToMessage with session + index', async () => {
    const onJump = vi.fn();
    mockSearch([
      {
        session_id: 's-jump',
        message_index: 7,
        role: 'assistant',
        content: 'Hier ist die Rechnung',
        snippet: `Hier ist die ${HL_START}Rechnung${HL_END}`,
        timestamp: null,
        rank: 0.9,
      },
    ]);
    renderWithRouter(<ChatMessageSearch scopeSessionId={null} onJumpToMessage={onJump} />);

    await userEvent.type(screen.getByRole('combobox'), 'Rechnung');
    const option = await screen.findByRole('option');
    await userEvent.click(option);

    expect(onJump).toHaveBeenCalledWith('s-jump', 7);
  });

  it('keyboard Enter jumps to the highlighted result', async () => {
    const onJump = vi.fn();
    mockSearch([
      {
        session_id: 's-kbd',
        message_index: 0,
        role: 'user',
        content: 'Termin Finanzamt',
        snippet: `${HL_START}Termin${HL_END} Finanzamt`,
        timestamp: null,
        rank: 1.0,
      },
    ]);
    renderWithRouter(<ChatMessageSearch scopeSessionId={null} onJumpToMessage={onJump} />);

    const input = screen.getByRole('combobox');
    await userEvent.type(input, 'Termin');
    await screen.findByRole('option');
    await userEvent.type(input, '{Enter}');

    expect(onJump).toHaveBeenCalledWith('s-kbd', 0);
  });
});
