import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL } from '../mocks/handlers.js';
import SettingsPage from '../../../../src/frontend/src/pages/SettingsPage';
import { renderWithProviders } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

// Default mock values for admin user
const adminAuthMock = {
  getAccessToken: () => Promise.resolve('mock-token'),
  hasPermission: () => true,
  hasAnyPermission: () => true,
  isAuthenticated: true,
  authEnabled: true,
  loading: false,
  user: { username: 'admin', role: 'Admin', permissions: ['admin', 'settings.manage'] }
};

// Mock settings response
const mockSettingsResponse = {
  enabled: true,
  keyword: 'alexa',
  threshold: 0.5,
  cooldown_ms: 2000,
  available_keywords: [
    { id: 'alexa', label: 'Alexa', description: 'Pre-trained wake word' },
    { id: 'hey_jarvis', label: 'Hey Jarvis', description: 'Pre-trained wake word' },
    { id: 'hey_mycroft', label: 'Hey Mycroft', description: 'Pre-trained wake word' }
  ],
  server_fallback_available: true,
  subscriber_count: 3
};

describe('SettingsPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);

    // Add handler for settings endpoint
    server.use(
      http.get(`${BASE_URL}/api/settings/wakeword`, () => {
        return HttpResponse.json(mockSettingsResponse);
      })
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Settings')).toBeInTheDocument();
      });
    });

    it('shows loading state initially', () => {
      renderWithProviders(<SettingsPage />);

      expect(screen.getByText('Loading...')).toBeInTheDocument();
    });

    it('displays wake word settings after loading', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      // Check for keyword dropdown
      const keywordSelect = screen.getByRole('combobox');
      expect(keywordSelect).toBeInTheDocument();
    });

    it('displays available keywords in dropdown', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      expect(keywordSelect).toHaveValue('alexa');

      // Check that options are present
      expect(screen.getByText('Alexa')).toBeInTheDocument();
      expect(screen.getByText('Hey Jarvis')).toBeInTheDocument();
      expect(screen.getByText('Hey Mycroft')).toBeInTheDocument();
    });

    it('displays connected devices count', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText(/3 devices connected/)).toBeInTheDocument();
      });
    });
  });

  describe('Form Interaction', () => {
    it('enables save button when settings are changed', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      const saveButton = screen.getByRole('button', { name: /save/i });
      expect(saveButton).toBeDisabled();

      // Change keyword
      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      // Save button should now be enabled
      expect(saveButton).not.toBeDisabled();
    });

    it('shows unsaved changes indicator when form is modified', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      // Change keyword
      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
    });

    it('updates threshold via slider', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      // Find threshold slider
      const thresholdSlider = screen.getAllByRole('slider')[0];
      expect(thresholdSlider).toBeInTheDocument();
    });
  });

  describe('Save Functionality', () => {
    it('calls API when save button is clicked', async () => {
      let saveCalled = false;

      server.use(
        http.put(`${BASE_URL}/api/settings/wakeword`, async ({ request }) => {
          saveCalled = true;
          const body = await request.json();
          return HttpResponse.json({
            ...mockSettingsResponse,
            ...body
          });
        })
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      // Change keyword
      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      // Click save
      const saveButton = screen.getByRole('button', { name: /save/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(saveCalled).toBe(true);
      });
    });

    it('shows success message after saving', async () => {
      server.use(
        http.put(`${BASE_URL}/api/settings/wakeword`, () => {
          return HttpResponse.json({
            ...mockSettingsResponse,
            keyword: 'hey_jarvis'
          });
        })
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      // Change keyword
      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      // Click save
      const saveButton = screen.getByRole('button', { name: /save/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/Settings saved/)).toBeInTheDocument();
      });
    });
  });

  describe('Error Handling', () => {
    it('displays error when loading fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/settings/wakeword`, () => {
          return new HttpResponse(null, { status: 500 });
        })
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText(/Failed to load settings/)).toBeInTheDocument();
      });
    });

    it('displays error when saving fails', async () => {
      server.use(
        http.put(`${BASE_URL}/api/settings/wakeword`, () => {
          return HttpResponse.json(
            { detail: 'Invalid keyword' },
            { status: 400 }
          );
        })
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      // Change keyword
      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      // Click save
      const saveButton = screen.getByRole('button', { name: /save/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/Invalid keyword/)).toBeInTheDocument();
      });
    });

    it('displays permission error when unauthorized', async () => {
      server.use(
        http.put(`${BASE_URL}/api/settings/wakeword`, () => {
          return new HttpResponse(null, { status: 403 });
        })
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Settings')).toBeInTheDocument();
      });

      // Change keyword
      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      // Click save
      const saveButton = screen.getByRole('button', { name: /save/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/Access denied/)).toBeInTheDocument();
      });
    });
  });
});
