/**
 * AgentRoleBadge — shows which agent role answered + pins it for the next turn
 * (chat-ui roadmap item 6). Tapping calls setRoleHint (reuses the role_hint path),
 * never sends. German is the test default.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AgentRoleBadge, { roleLabel } from '../../../../src/frontend/src/components/chat/AgentRoleBadge';
import { type ChatContextValue } from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import { buildChatContextValue } from '../test-chat-mock';
import { renderWithRouter } from '../test-utils';

let ctx: ChatContextValue = buildChatContextValue();

vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual<
    typeof import('../../../../src/frontend/src/pages/ChatPage/context/ChatContext')
  >('../../../../src/frontend/src/pages/ChatPage/context/ChatContext');
  return { ...actual, useChatContext: (): ChatContextValue => ctx };
});

describe('AgentRoleBadge', () => {
  it('renders nothing without a role', () => {
    ctx = buildChatContextValue();
    const { container } = renderWithRouter(<AgentRoleBadge role={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders the localized role label', () => {
    ctx = buildChatContextValue();
    renderWithRouter(<AgentRoleBadge role="smart_home" />);
    // de.json chat.roles.smart_home = "Smart Home"
    expect(screen.getByRole('button', { name: /Smart Home/ })).toBeInTheDocument();
  });

  it('tapping pins the role for the next turn (setRoleHint), does not send', async () => {
    const setRoleHint = vi.fn();
    ctx = buildChatContextValue({ setRoleHint, sendMessage: vi.fn(async () => {}) });
    renderWithRouter(<AgentRoleBadge role="media" />);
    await userEvent.click(screen.getByRole('button', { name: /Medien/ }));
    expect(setRoleHint).toHaveBeenCalledWith('media');
    expect(ctx.sendMessage).not.toHaveBeenCalled();
  });

  it('shows a pinned state when this role is the active hint', () => {
    ctx = buildChatContextValue({ pendingRoleHint: 'documents' });
    renderWithRouter(<AgentRoleBadge role="documents" />);
    // aria-label switches to the "pinned" string for the active role
    expect(screen.getByRole('button', { name: /angeheftet/ })).toBeInTheDocument();
  });

  it('roleLabel falls back to the raw id for an unknown role', () => {
    const t = (k: string) => k; // identity translator
    expect(roleLabel(t, 'totally_new_role')).toBe('totally_new_role');
    expect(roleLabel(t, 'media')).toBe('chat.roles.media');
  });
});
