import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL } from '../mocks/handlers.js';
import SpeakersPage from '../../../../src/frontend/src/pages/SpeakersPage';
import { renderWithProviders } from '../test-utils.jsx';

// Mock data
const mockSpeakers = [
  {
    id: 1,
    name: 'Max Mustermann',
    alias: 'max',
    is_admin: true,
    embedding_count: 5
  },
  {
    id: 2,
    name: 'Anna Schmidt',
    alias: 'anna',
    is_admin: false,
    embedding_count: 2
  },
  {
    id: 3,
    name: 'Test User',
    alias: 'test',
    is_admin: false,
    embedding_count: 0
  }
];

const mockServiceStatus = {
  available: true,
  message: 'SpeechBrain ECAPA-TDNN Model geladen'
};

// Mock ConfirmDialog
vi.mock('../../../../src/frontend/src/components/ConfirmDialog', () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn().mockResolvedValue(true),
    ConfirmDialogComponent: null
  })
}));

// Mock Modal component
vi.mock('../../../../src/frontend/src/components/Modal', () => ({
  default: ({ isOpen, onClose, title, children }) => {
    if (!isOpen) return null;
    return (
      <div data-testid="modal">
        <h2>{title}</h2>
        <button onClick={onClose}>Schließen</button>
        {children}
      </div>
    );
  }
}));

describe('SpeakersPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    // Set up default handlers
    server.use(
      http.get(`${BASE_URL}/api/speakers/status`, () => {
        return HttpResponse.json(mockServiceStatus);
      }),
      http.get(`${BASE_URL}/api/speakers`, () => {
        return HttpResponse.json(mockSpeakers);
      })
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<SpeakersPage />);

      expect(screen.getByText('Sprechererkennung')).toBeInTheDocument();
      expect(screen.getByText('Verwalte Sprecher und Voice Samples')).toBeInTheDocument();
    });

    it('shows loading state initially', () => {
      renderWithProviders(<SpeakersPage />);

      expect(screen.getByText('Lade Sprecher...')).toBeInTheDocument();
    });

    it('shows service status when available', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Speaker Recognition aktiv')).toBeInTheDocument();
      });
    });

    it('shows service unavailable status', async () => {
      server.use(
        http.get(`${BASE_URL}/api/speakers/status`, () => {
          return HttpResponse.json({
            available: false,
            message: 'Model nicht geladen'
          });
        })
      );

      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Speaker Recognition nicht verfügbar')).toBeInTheDocument();
      });
    });

    it('displays speakers after loading', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      expect(screen.getByText('Anna Schmidt')).toBeInTheDocument();
      expect(screen.getByText('Test User')).toBeInTheDocument();
    });

    it('shows speaker count in heading', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Registrierte Sprecher (3)')).toBeInTheDocument();
      });
    });

    it('shows admin badge for admin speakers', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      expect(screen.getByText('Admin')).toBeInTheDocument();
    });

    it('shows voice sample count for speakers', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      expect(screen.getByText(/5 \(gut\)/)).toBeInTheDocument();
      expect(screen.getByText(/2 \(mehr empfohlen\)/)).toBeInTheDocument();
      expect(screen.getByText(/0 \(keine\)/)).toBeInTheDocument();
    });
  });

  describe('Empty State', () => {
    it('shows empty state when no speakers exist', async () => {
      server.use(
        http.get(`${BASE_URL}/api/speakers`, () => {
          return HttpResponse.json([]);
        })
      );

      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Noch keine Sprecher registriert')).toBeInTheDocument();
      });

      expect(screen.getByText('Ersten Sprecher anlegen')).toBeInTheDocument();
    });
  });

  describe('Create Speaker', () => {
    it('opens create modal when clicking new speaker button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Neuer Sprecher'));

      expect(screen.getByText('Neuen Sprecher anlegen')).toBeInTheDocument();
    });

    it('creates speaker with form data', async () => {
      const user = userEvent.setup();
      let createdSpeaker = null;

      server.use(
        http.post(`${BASE_URL}/api/speakers`, async ({ request }) => {
          createdSpeaker = await request.json();
          return HttpResponse.json({
            id: 4,
            ...createdSpeaker,
            embedding_count: 0
          }, { status: 201 });
        })
      );

      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Neuer Sprecher'));

      // Fill in form
      await user.type(screen.getByPlaceholderText('Max Mustermann'), 'New Speaker');
      await user.type(screen.getByPlaceholderText('max'), 'newspeaker');

      // Submit
      await user.click(screen.getByText('Erstellen'));

      await waitFor(() => {
        expect(createdSpeaker).not.toBeNull();
      });

      expect(createdSpeaker.name).toBe('New Speaker');
      expect(createdSpeaker.alias).toBe('newspeaker');
    });
  });

  describe('Delete Speaker', () => {
    it('deletes speaker when clicking delete button', async () => {
      const user = userEvent.setup();
      let deletedId = null;

      server.use(
        http.delete(`${BASE_URL}/api/speakers/:id`, ({ params }) => {
          deletedId = params.id;
          return HttpResponse.json({ message: 'Speaker deleted' });
        })
      );

      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      // Find and click delete button for first speaker
      const deleteButtons = screen.getAllByLabelText(/löschen/i);
      await user.click(deleteButtons[0]);

      await waitFor(() => {
        expect(deletedId).not.toBeNull();
      });
    });
  });

  describe('Action Buttons', () => {
    it('shows refresh button', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      expect(screen.getByLabelText('Sprecherliste aktualisieren')).toBeInTheDocument();
    });

    it('shows identify speaker button', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      expect(screen.getByText('Sprecher identifizieren')).toBeInTheDocument();
    });

    it('disables identify button when no speakers exist', async () => {
      server.use(
        http.get(`${BASE_URL}/api/speakers`, () => {
          return HttpResponse.json([]);
        })
      );

      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Noch keine Sprecher registriert')).toBeInTheDocument();
      });

      // The text is inside a span, so we need to find the button containing it
      const identifyButton = screen.getByText('Sprecher identifizieren').closest('button');
      expect(identifyButton).toBeDisabled();
    });
  });

  describe('Error Handling', () => {
    it('shows error when loading fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/speakers`, () => {
          return HttpResponse.json(
            { detail: 'Failed to load speakers' },
            { status: 500 }
          );
        })
      );

      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText(/Sprecher konnten nicht geladen werden/i)).toBeInTheDocument();
      });
    });
  });

  describe('Speaker Actions', () => {
    it('shows enroll button for each speaker', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      const enrollButtons = screen.getAllByText('Aufnehmen');
      expect(enrollButtons.length).toBe(3);
    });

    it('shows edit button for each speaker', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      const editButtons = screen.getAllByLabelText(/bearbeiten/i);
      expect(editButtons.length).toBe(3);
    });

    it('shows merge button for each speaker', async () => {
      renderWithProviders(<SpeakersPage />);

      await waitFor(() => {
        expect(screen.getByText('Max Mustermann')).toBeInTheDocument();
      });

      const mergeButtons = screen.getAllByLabelText(/zusammenführen/i);
      expect(mergeButtons.length).toBe(3);
    });
  });
});
