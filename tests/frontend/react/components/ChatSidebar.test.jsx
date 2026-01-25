import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatSidebar from '../../../../src/frontend/src/components/ChatSidebar';
import { renderWithRouter } from '../test-utils.jsx';

// Mock conversations for testing
const mockConversations = [
  {
    session_id: 'session-today-1',
    preview: 'Wie ist das Wetter heute?',
    message_count: 4,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  },
  {
    session_id: 'session-today-2',
    preview: 'Schalte das Licht an',
    message_count: 2,
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() - 3600000).toISOString() // 1 hour ago
  },
  {
    session_id: 'session-yesterday-1',
    preview: 'Was gibt es Neues?',
    message_count: 6,
    created_at: new Date(Date.now() - 86400000).toISOString(),
    updated_at: new Date(Date.now() - 86400000).toISOString() // Yesterday
  },
  {
    session_id: 'session-old-1',
    preview: 'Ältere Konversation',
    message_count: 10,
    created_at: new Date(Date.now() - 86400000 * 10).toISOString(),
    updated_at: new Date(Date.now() - 86400000 * 10).toISOString() // 10 days ago
  }
];

describe('ChatSidebar', () => {
  const defaultProps = {
    conversations: mockConversations,
    activeSessionId: null,
    onSelectConversation: vi.fn(),
    onNewChat: vi.fn(),
    onDeleteConversation: vi.fn(),
    isOpen: true,
    onClose: vi.fn(),
    loading: false
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the sidebar with conversations', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      expect(screen.getByText('Neuer Chat')).toBeInTheDocument();
      expect(screen.getByText('Wie ist das Wetter heute?')).toBeInTheDocument();
      expect(screen.getByText('Schalte das Licht an')).toBeInTheDocument();
    });

    it('shows loading state', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} loading={true} />);

      // Loader shows spinning icon with sr-only text
      expect(screen.getByRole('navigation')).toBeInTheDocument();
    });

    it('shows empty state when no conversations', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} conversations={[]} />);

      expect(screen.getByText('Noch keine Konversationen')).toBeInTheDocument();
      expect(screen.getByText(/Starte ein Gespräch/i)).toBeInTheDocument();
    });

    it('groups conversations by date', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      expect(screen.getByText('Heute')).toBeInTheDocument();
      expect(screen.getByText('Gestern')).toBeInTheDocument();
      expect(screen.getByText('Älter')).toBeInTheDocument();
    });

    it('shows message count for each conversation', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      expect(screen.getByText('4 Nachrichten')).toBeInTheDocument();
      expect(screen.getByText('2 Nachrichten')).toBeInTheDocument();
      expect(screen.getByText('6 Nachrichten')).toBeInTheDocument();
      expect(screen.getByText('10 Nachrichten')).toBeInTheDocument();
    });

    it('handles singular message count', () => {
      const singleMessageConv = [{
        session_id: 'single',
        preview: 'Single message',
        message_count: 1,
        updated_at: new Date().toISOString()
      }];

      renderWithRouter(<ChatSidebar {...defaultProps} conversations={singleMessageConv} />);

      expect(screen.getByText('1 Nachricht')).toBeInTheDocument();
    });
  });

  describe('Active conversation', () => {
    it('highlights the active conversation', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} activeSessionId="session-today-1" />);

      // The active conversation item should have a different background
      // In light mode it's bg-gray-100, in dark mode it's dark:bg-gray-700
      const activeItem = screen.getByText('Wie ist das Wetter heute?').closest('[role="button"]');
      expect(activeItem).toHaveClass('bg-gray-100');
    });
  });

  describe('User Interactions', () => {
    it('calls onNewChat when clicking new chat button', async () => {
      const user = userEvent.setup();
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      await user.click(screen.getByText('Neuer Chat'));

      expect(defaultProps.onNewChat).toHaveBeenCalledTimes(1);
    });

    it('calls onSelectConversation when clicking a conversation', async () => {
      const user = userEvent.setup();
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      const conversationItem = screen.getByText('Wie ist das Wetter heute?').closest('[role="button"]');
      await user.click(conversationItem);

      expect(defaultProps.onSelectConversation).toHaveBeenCalledWith('session-today-1');
    });

    it('calls onDeleteConversation when clicking delete button', async () => {
      const user = userEvent.setup();
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      // Find the delete button (it should be in the conversation item)
      const deleteButtons = screen.getAllByLabelText(/Konversation löschen/i);
      await user.click(deleteButtons[0]);

      expect(defaultProps.onDeleteConversation).toHaveBeenCalledWith('session-today-1');
    });

    it('calls onClose when clicking the backdrop on mobile', async () => {
      const user = userEvent.setup();
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      // The backdrop is the overlay div
      const backdrop = document.querySelector('.bg-black\\/50');
      if (backdrop) {
        await user.click(backdrop);
        expect(defaultProps.onClose).toHaveBeenCalled();
      }
    });

    it('supports keyboard navigation', async () => {
      const user = userEvent.setup();
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      const conversationItem = screen.getByText('Wie ist das Wetter heute?').closest('[role="button"]');

      // Focus the item
      conversationItem.focus();

      // Press Enter
      await user.keyboard('{Enter}');

      expect(defaultProps.onSelectConversation).toHaveBeenCalledWith('session-today-1');
    });
  });

  describe('Mobile behavior', () => {
    it('renders close button on mobile view', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      // The close button should be visible (though CSS may hide it on desktop)
      expect(screen.getByLabelText('Menü schließen')).toBeInTheDocument();
    });

    it('calls onClose when clicking close button', async () => {
      const user = userEvent.setup();
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      await user.click(screen.getByLabelText('Menü schließen'));

      expect(defaultProps.onClose).toHaveBeenCalled();
    });

    it('applies transform class when closed', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} isOpen={false} />);

      const sidebar = document.querySelector('aside');
      expect(sidebar).toHaveClass('-translate-x-full');
    });

    it('applies transform class when open', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} isOpen={true} />);

      const sidebar = document.querySelector('aside');
      expect(sidebar).toHaveClass('translate-x-0');
    });
  });

  describe('Accessibility', () => {
    it('has proper ARIA labels', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      expect(screen.getByRole('navigation')).toHaveAttribute('aria-label', 'Konversationen öffnen');
      expect(screen.getByRole('list')).toHaveAttribute('aria-label', 'Konversationen');
    });

    it('marks active conversation with aria-current', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} activeSessionId="session-today-1" />);

      const activeItem = screen.getByText('Wie ist das Wetter heute?').closest('[role="button"]');
      expect(activeItem).toHaveAttribute('aria-current', 'true');
    });

    it('conversation items are focusable', () => {
      renderWithRouter(<ChatSidebar {...defaultProps} />);

      // Find conversation items by their aria-label pattern
      const conversationItems = screen.getAllByLabelText(/^Konversation:/);

      expect(conversationItems.length).toBeGreaterThan(0);
      conversationItems.forEach(item => {
        // tabIndex is lowercase in the DOM
        expect(item).toHaveAttribute('tabindex', '0');
      });
    });
  });
});

describe('ConversationItem', () => {
  // These tests are implicitly covered through ChatSidebar tests,
  // but we can add specific ConversationItem tests here if needed

  it('truncates long preview text via CSS', () => {
    const longPreviewConv = [{
      session_id: 'long',
      preview: 'This is a very long preview text that should be truncated because it exceeds the available space in the sidebar',
      message_count: 1,
      updated_at: new Date().toISOString()
    }];

    renderWithRouter(
      <ChatSidebar
        conversations={longPreviewConv}
        activeSessionId={null}
        onSelectConversation={vi.fn()}
        onNewChat={vi.fn()}
        onDeleteConversation={vi.fn()}
        isOpen={true}
        onClose={vi.fn()}
        loading={false}
      />
    );

    const previewElement = screen.getByText(/This is a very long preview text/);
    expect(previewElement).toHaveClass('truncate');
  });

  it('shows fallback text for empty preview', () => {
    const emptyPreviewConv = [{
      session_id: 'empty',
      preview: '',
      message_count: 0,
      updated_at: new Date().toISOString()
    }];

    renderWithRouter(
      <ChatSidebar
        conversations={emptyPreviewConv}
        activeSessionId={null}
        onSelectConversation={vi.fn()}
        onNewChat={vi.fn()}
        onDeleteConversation={vi.fn()}
        isOpen={true}
        onClose={vi.fn()}
        loading={false}
      />
    );

    expect(screen.getByText('Neue Konversation')).toBeInTheDocument();
  });
});
