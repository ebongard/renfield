/**
 * CommandPalette — overlay open/close, keyboard, and per-category execution.
 * Tool actions STAGE into the composer (setInput), never auto-send.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithRouter } from '../test-utils';

const navigateSpy = vi.fn();
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => navigateSpy };
});

const ctx = {
  paletteOpen: true,
  closePalette: vi.fn(),
  setInput: vi.fn(),
  setRoleHint: vi.fn(),
  sendMessage: vi.fn(),
};
vi.mock('../../../../src/frontend/src/pages/ChatPage/context/ChatContext', async () => {
  const actual = await vi.importActual<
    typeof import('../../../../src/frontend/src/pages/ChatPage/context/ChatContext')
  >('../../../../src/frontend/src/pages/ChatPage/context/ChatContext');
  return { ...actual, useChatContext: () => ctx };
});

const FIXED = [
  { id: 'nav.brain', category: 'navigate', label: 'Open knowledge', icon: () => null, to: '/brain' },
  { id: 'tool.bt_scan', category: 'tool', label: 'Scan Bluetooth devices', icon: () => null, toolCommand: 'Scanne alle Bluetooth-Geräte' },
  { id: 'role.media', category: 'set-role', label: 'Role: Media', icon: () => null, roleId: 'media' },
];
vi.mock('../../../../src/frontend/src/components/chat/palette/usePaletteActions', () => ({
  usePaletteActions: (query: string) => {
    const filtered = query ? FIXED.filter((a) => a.label.toLowerCase().includes(query.toLowerCase())) : FIXED;
    return { visible: FIXED, filtered };
  },
}));

import CommandPalette from '../../../../src/frontend/src/components/chat/palette/CommandPalette';

describe('CommandPalette', () => {
  beforeEach(() => { ctx.paletteOpen = true; vi.clearAllMocks(); });

  it('renders nothing when closed', () => {
    ctx.paletteOpen = false;
    const { container } = renderWithRouter(<CommandPalette />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders a dialog with all actions when open', () => {
    renderWithRouter(<CommandPalette />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Open knowledge/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Scan Bluetooth/ })).toBeInTheDocument();
  });

  it('navigate action calls navigate() and closes', async () => {
    renderWithRouter(<CommandPalette />);
    await userEvent.click(screen.getByText('Open knowledge'));
    expect(navigateSpy).toHaveBeenCalledWith('/brain');
    expect(ctx.closePalette).toHaveBeenCalled();
  });

  it('tool action STAGES into the composer (setInput), never sends', async () => {
    renderWithRouter(<CommandPalette />);
    await userEvent.click(screen.getByText('Scan Bluetooth devices'));
    expect(ctx.setInput).toHaveBeenCalledWith('Scanne alle Bluetooth-Geräte');
    expect(ctx.sendMessage).not.toHaveBeenCalled();
  });

  it('set-role action sets the role hint', async () => {
    renderWithRouter(<CommandPalette />);
    await userEvent.click(screen.getByText('Role: Media'));
    expect(ctx.setRoleHint).toHaveBeenCalledWith('media');
  });

  it('Escape closes the palette', async () => {
    renderWithRouter(<CommandPalette />);
    await userEvent.keyboard('{Escape}');
    expect(ctx.closePalette).toHaveBeenCalled();
  });

  it('typing filters the list', async () => {
    renderWithRouter(<CommandPalette />);
    await userEvent.type(screen.getByRole('combobox'), 'bluetooth');
    expect(screen.getByText('Scan Bluetooth devices')).toBeInTheDocument();
    expect(screen.queryByText('Open knowledge')).not.toBeInTheDocument();
  });
});
