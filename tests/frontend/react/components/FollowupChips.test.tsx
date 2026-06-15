/**
 * FollowupChips — ephemeral follow-up suggestion chips. Tapping fills the
 * composer (setInput), never auto-sends. German is the test default.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FollowupChips from '../../../../src/frontend/src/components/chat/FollowupChips';
import {
  type ChatContextValue,
} from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import { chatContextWithSpies } from '../test-chat-mock';
import { renderWithRouter } from '../test-utils';

const ctx = chatContextWithSpies();

vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual<
    typeof import('../../../../src/frontend/src/pages/ChatPage/context/ChatContext')
  >('../../../../src/frontend/src/pages/ChatPage/context/ChatContext');
  return { ...actual, useChatContext: (): ChatContextValue => ctx };
});

describe('FollowupChips', () => {
  it('renders nothing for empty or undefined', () => {
    const { container, rerender } = renderWithRouter(<FollowupChips followups={[]} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<FollowupChips followups={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a button per suggestion', () => {
    renderWithRouter(<FollowupChips followups={['Wie viel kostet das?', 'Wann ist es fällig?']} />);
    expect(screen.getByRole('button', { name: /Wie viel kostet das\?/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Wann ist es fällig\?/ })).toBeInTheDocument();
  });

  it('tapping a chip fills the composer (setInput) and does NOT send', async () => {
    renderWithRouter(<FollowupChips followups={['Und morgen?']} />);
    await userEvent.click(screen.getByRole('button', { name: /Und morgen\?/ }));
    expect(ctx.setInput).toHaveBeenCalledWith('Und morgen?');
    expect(ctx.sendMessage).not.toHaveBeenCalled();
  });

  it('shows the full suggestion text — long chips wrap, never clip', () => {
    // Regression guard: a fixed `truncate max-w-[260px]` clipped long
    // suggestions mid-word (e.g. "…noch nicht im Syst"). The text span must
    // wrap, not truncate, and the full text stays available (button name + title).
    const long = 'Welche Geräte sind noch nicht im System registriert worden?';
    renderWithRouter(<FollowupChips followups={[long]} />);
    const btn = screen.getByRole('button', { name: long });
    expect(btn).toHaveAttribute('title', long);
    const span = btn.querySelector('span');
    expect(span).not.toBeNull();
    expect(span?.className).toMatch(/whitespace-normal/);
    expect(span?.className).not.toMatch(/truncate/);
  });
});
