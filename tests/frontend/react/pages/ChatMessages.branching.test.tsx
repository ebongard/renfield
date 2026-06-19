/**
 * Chat branching (edit-and-fork, Phase 1) — ChatMessages render contract.
 *
 * Like the federationProgress test, we render ChatMessages directly with a
 * crafted messages array and a stubbed ChatContext, and drive the
 * chat_branching_enabled flag via an MSW override on /api/config/features.
 *
 * Verifies:
 *  - flag OFF → no edit/regenerate affordances render.
 *  - flag ON → the LATEST user message shows Edit; clicking → inline editor;
 *    submitting calls editAndResubmit(index, newText).
 *  - flag ON → the LATEST assistant turn shows Regenerate; clicking calls
 *    regenerateTurn(assistantIndex).
 *  - only the LATEST user/assistant turns get the affordances (Phase 1 gate).
 */
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
  // jsdom: focus() exists; nothing else needed.
});

import ChatMessages from '../../../../src/frontend/src/pages/ChatPage/ChatMessages';
import { renderWithRouter } from '../test-utils';
import {
  useChatContext,
  type ChatContextValue,
} from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import { buildChatContextValue } from '../test-chat-mock';
import { server } from '../mocks/server';

const BASE_URL = 'http://localhost:8000';

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

function enableBranchingFlag(): void {
  server.use(
    http.get(`${BASE_URL}/api/config/features`, () =>
      HttpResponse.json({
        schicht_a_extraction_enabled: false,
        wissen_workspace_enabled: false,
        command_palette_enabled: false,
        role_surfacing_enabled: false,
        message_search_enabled: false,
        artifacts_typed_enabled: false,
        room_handoff_enabled: false,
        chat_branching_enabled: true,
      }),
    ),
  );
}

function driveContext(
  messages: ChatMessage[],
  overrides: Partial<ChatContextValue> = {},
): void {
  vi.mocked(useChatContext).mockReturnValue(
    buildChatContextValue({ messages, ...overrides }),
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

const LINEAR: ChatMessage[] = [
  { id: 1, role: 'user', content: 'Wie ist das Wetter?' },
  { id: 2, role: 'assistant', content: 'Sonnig.' },
];

describe('ChatMessages — chat branching (Phase 1)', () => {
  it('renders no edit/regenerate affordances when the flag is OFF', async () => {
    // default MSW handler → chat_branching_enabled: false
    driveContext(LINEAR);
    renderWithRouter(<ChatMessages />);
    // Give the feature-flag query a tick to resolve to false.
    await waitFor(() =>
      expect(screen.getByText('Sonnig.')).toBeInTheDocument(),
    );
    expect(screen.queryByLabelText(/Nachricht bearbeiten|Edit message/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Neu generieren|Regenerate/i)).not.toBeInTheDocument();
  });

  it('shows Edit on the latest user message and fires editAndResubmit with the right index', async () => {
    enableBranchingFlag();
    const editAndResubmit = vi.fn();
    driveContext(LINEAR, { editAndResubmit });
    renderWithRouter(<ChatMessages />);

    const editBtn = await screen.findByLabelText(/Nachricht bearbeiten|Edit message/i);
    await userEvent.click(editBtn);

    const textarea = await screen.findByLabelText(/Nachricht bearbeiten|Edit message/i);
    // Clicking Edit swaps the label onto the textarea editor; type a new value.
    fireEvent.change(textarea, { target: { value: 'Und morgen?' } });

    const submit = screen.getByText(/Erneut senden|Resubmit/i);
    await userEvent.click(submit);

    // The edited user message is at index 0 in LINEAR.
    expect(editAndResubmit).toHaveBeenCalledWith(0, 'Und morgen?');
  });

  it('shows Regenerate on the latest assistant turn and fires regenerateTurn with the right index', async () => {
    enableBranchingFlag();
    const regenerateTurn = vi.fn();
    driveContext(LINEAR, { regenerateTurn });
    renderWithRouter(<ChatMessages />);

    const regenBtn = await screen.findByLabelText(/Neu generieren|Regenerate/i);
    await userEvent.click(regenBtn);

    // The assistant turn is at index 1 in LINEAR.
    expect(regenerateTurn).toHaveBeenCalledWith(1);
  });

  it('ALL user/assistant turns are editable/regenerable (Phase 2 fork-from-any)', async () => {
    enableBranchingFlag();
    const TWO_TURNS: ChatMessage[] = [
      { id: 1, role: 'user', content: 'erste Frage' },
      { id: 2, role: 'assistant', content: 'erste Antwort' },
      { id: 3, role: 'user', content: 'zweite Frage' },
      { id: 4, role: 'assistant', content: 'zweite Antwort' },
    ];
    driveContext(TWO_TURNS);
    renderWithRouter(<ChatMessages />);

    await waitFor(() =>
      expect(screen.getByText('zweite Antwort')).toBeInTheDocument(),
    );
    // Phase 2: BOTH user messages get Edit, BOTH assistant turns get Regenerate.
    const editBtns = await screen.findAllByLabelText(/Nachricht bearbeiten|Edit message/i);
    expect(editBtns).toHaveLength(2);
    const regenBtns = screen.getAllByLabelText(/Neu generieren|Regenerate/i);
    expect(regenBtns).toHaveLength(2);
  });
});

describe('ChatMessages — branch switcher (Phase 2)', () => {
  // An assistant turn that is branch 2 of 2 (siblings 41 and 42).
  const FORKED: ChatMessage[] = [
    { id: 1, role: 'user', content: 'Frage' },
    {
      id: 42,
      role: 'assistant',
      content: 'Antwort Variante B',
      branch: { index: 1, count: 2, sibling_ids: [41, 42] },
    },
  ];

  it('renders the ‹n/m› switcher and fires switchBranch with the previous sibling', async () => {
    enableBranchingFlag();
    const switchBranch = vi.fn().mockResolvedValue(undefined);
    driveContext(FORKED, { switchBranch });
    renderWithRouter(<ChatMessages />);

    // Position indicator 2/2 is shown.
    await waitFor(() => expect(screen.getByText('2/2')).toBeInTheDocument());

    // ◂ navigates to the previous sibling (id 41); ▸ is disabled (already last).
    const prev = screen.getByLabelText(/Vorheriger Branch|Previous branch/i);
    const next = screen.getByLabelText(/Nächster Branch|Next branch/i);
    expect(next).toBeDisabled();
    await userEvent.click(prev);
    expect(switchBranch).toHaveBeenCalledWith(41);
  });

  it('fires deleteBranch (switching to the sibling first) on confirm', async () => {
    enableBranchingFlag();
    const deleteBranch = vi.fn().mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    driveContext(FORKED, { deleteBranch });
    renderWithRouter(<ChatMessages />);

    const del = await screen.findByLabelText(/Diesen Branch löschen|Delete this branch/i);
    await userEvent.click(del);
    // deletes current (42), switching to neighbor sibling (41).
    expect(deleteBranch).toHaveBeenCalledWith(42, 41);
    confirmSpy.mockRestore();
  });

  it('no switcher when a message has no siblings', async () => {
    enableBranchingFlag();
    driveContext(LINEAR);
    renderWithRouter(<ChatMessages />);
    await waitFor(() => expect(screen.getByText('Sonnig.')).toBeInTheDocument());
    expect(screen.queryByLabelText(/Vorheriger Branch|Previous branch/i)).not.toBeInTheDocument();
  });
});
