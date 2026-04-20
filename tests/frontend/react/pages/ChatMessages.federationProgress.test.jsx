/**
 * F4c — federation progress rendering inside ChatMessages.
 *
 * Rather than drive the full ChatContext + WebSocket stack, we render
 * ChatMessages directly with a messages array that already contains
 * the `federationProgress` map. This isolates the test to the
 * render-layer contract: one status line per peer, keyed by pubkey,
 * labeled by the locked vocabulary, disappearing on terminal chunks.
 */
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { screen } from '@testing-library/react';

// jsdom doesn't implement scrollIntoView; ChatMessages auto-scrolls on
// every render via effect. Stub once for the whole file.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
import ChatMessages from '../../../../src/frontend/src/pages/ChatPage/ChatMessages';
import { renderWithRouter } from '../test-utils.jsx';

// ChatMessages pulls a bundle of things from ChatContext (messages list,
// loading flag, derived selectors, handlers for card / TTS / attachments).
// Mock the context to a minimal shape so we can drive just the one message
// we care about.
vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual(
    '../../../../src/frontend/src/pages/ChatPage/context/ChatContext',
  );
  return {
    ...actual,
    useChatContext: vi.fn(),
  };
});

import { useChatContext } from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';

function driveContext(messages) {
  useChatContext.mockReturnValue({
    messages,
    loading: false,
    ttsPlaying: null,
    playTTS: vi.fn(),
    stopTTS: vi.fn(),
    handleQuickAction: vi.fn(),
    openEmailDialog: vi.fn(),
    closeEmailDialog: vi.fn(),
    emailDialogOpen: false,
    emailDialogContext: null,
    hideSteps: false,
    hideActions: false,
  });
}

const MOM_PUBKEY = 'm'.repeat(64);
const DAD_PUBKEY = 'd'.repeat(64);

describe('ChatMessages — federation progress', () => {
  it('does not render a progress list when federationProgress is absent', () => {
    driveContext([
      { role: 'user', content: 'Was hat Mom zur Hochzeit gesagt?' },
      { role: 'assistant', content: '', streaming: true },
    ]);
    renderWithRouter(<ChatMessages />);
    // Nothing matching the per-peer live vocabulary should be on the page
    expect(screen.queryByText(/sucht Wissen/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/formuliert Antwort/i)).not.toBeInTheDocument();
  });

  it('renders one line per peer when federationProgress has entries', () => {
    driveContext([
      { role: 'user', content: 'Ask both' },
      {
        role: 'assistant',
        content: '',
        streaming: true,
        federationProgress: {
          [MOM_PUBKEY]: { peer_display_name: 'Mom', label: 'retrieving', sequence: 2 },
          [DAD_PUBKEY]: { peer_display_name: 'Dad', label: 'synthesizing', sequence: 3 },
        },
      },
    ]);
    renderWithRouter(<ChatMessages />);
    expect(screen.getByText(/Moms Renfield sucht Wissen/i)).toBeInTheDocument();
    expect(screen.getByText(/Dads Renfield formuliert eine Antwort/i)).toBeInTheDocument();
  });

  it('renders an aria-live region for screen readers', () => {
    driveContext([
      {
        role: 'assistant',
        content: '',
        streaming: true,
        federationProgress: {
          [MOM_PUBKEY]: { peer_display_name: 'Mom', label: 'retrieving', sequence: 1 },
        },
      },
    ]);
    const { container } = renderWithRouter(<ChatMessages />);
    const live = container.querySelector('[aria-live="polite"]');
    expect(live).not.toBeNull();
    expect(live.textContent).toMatch(/Moms Renfield sucht Wissen/i);
  });

  it('falls back to a generic label for unknown progress labels', () => {
    driveContext([
      {
        role: 'assistant',
        content: '',
        streaming: true,
        federationProgress: {
          [MOM_PUBKEY]: { peer_display_name: 'Mom', label: 'future_unknown_label', sequence: 1 },
        },
      },
    ]);
    renderWithRouter(<ChatMessages />);
    // Fallback copy in DE: "Frage Moms Renfield..."
    expect(screen.getByText(/Frage Moms Renfield/i)).toBeInTheDocument();
  });

  it('waking_up, retrieving, synthesizing all render distinct localized copy', () => {
    driveContext([
      {
        role: 'assistant',
        content: '',
        streaming: true,
        federationProgress: {
          aaa: { peer_display_name: 'A', label: 'waking_up', sequence: 1 },
          bbb: { peer_display_name: 'B', label: 'retrieving', sequence: 1 },
          ccc: { peer_display_name: 'C', label: 'synthesizing', sequence: 1 },
        },
      },
    ]);
    renderWithRouter(<ChatMessages />);
    expect(screen.getByText(/Verbinde mit As Renfield/i)).toBeInTheDocument();
    expect(screen.getByText(/Bs Renfield sucht Wissen/i)).toBeInTheDocument();
    expect(screen.getByText(/Cs Renfield formuliert eine Antwort/i)).toBeInTheDocument();
  });
});
