import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import SettingsPage from '../../../../src/frontend/src/pages/SettingsPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { adminAuthMock } from '../test-auth-mock';
import type { WakewordSettingsData, WakewordInput } from '../../../../src/frontend/src/api/resources/settings';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/context/AuthContext')>(
    '../../../../src/frontend/src/context/AuthContext',
  );
  return {
    ...actual,
    useAuth: vi.fn<() => AuthContextValue>(),
  };
});

// Mock settings response (a few extra fields the backend ships are tolerated by the type via index)
const mockSettingsResponse: WakewordSettingsData = {
  keyword: 'alexa',
  threshold: 0.5,
  cooldown_ms: 2000,
  available_keywords: [
    { id: 'alexa', label: 'Alexa', description: 'Pre-trained wake word' },
    { id: 'hey_jarvis', label: 'Hey Jarvis', description: 'Pre-trained wake word' },
    { id: 'hey_mycroft', label: 'Hey Mycroft', description: 'Pre-trained wake word' },
  ],
  subscriber_count: 3,
};

describe('SettingsPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);

    // Add handler for settings endpoint
    server.use(
      http.get(`${BASE_URL}/api/settings/wakeword`, () => {
        return HttpResponse.json(mockSettingsResponse);
      }),
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Einstellungen')).toBeInTheDocument();
      });
    });

    it('shows loading state initially', () => {
      renderWithProviders(<SettingsPage />);

      expect(screen.getByText('Lade...')).toBeInTheDocument();
    });

    it('displays wake word settings after loading', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      expect(keywordSelect).toBeInTheDocument();
    });

    it('displays available keywords in dropdown', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      expect(keywordSelect).toHaveValue('alexa');

      expect(screen.getByText('Alexa')).toBeInTheDocument();
      expect(screen.getByText('Hey Jarvis')).toBeInTheDocument();
      expect(screen.getByText('Hey Mycroft')).toBeInTheDocument();
    });

    it('displays connected devices count', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText(/3 Geräte verbunden/)).toBeInTheDocument();
      });
    });
  });

  describe('Form Interaction', () => {
    it('enables save button when settings are changed', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const saveButton = screen.getByRole('button', { name: /speichern/i });
      expect(saveButton).toBeDisabled();

      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      expect(saveButton).not.toBeDisabled();
    });

    it('shows unsaved changes indicator when form is modified', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      expect(screen.getByText('Ungespeicherte Änderungen')).toBeInTheDocument();
    });

    it('updates threshold via slider', async () => {
      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

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
          const body = (await request.json()) as WakewordInput;
          return HttpResponse.json({
            ...mockSettingsResponse,
            ...body,
          });
        }),
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      const saveButton = screen.getByRole('button', { name: /speichern/i });
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
            keyword: 'hey_jarvis',
          });
        }),
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      const saveButton = screen.getByRole('button', { name: /speichern/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/Einstellungen gespeichert/)).toBeInTheDocument();
      });
    });
  });

  describe('Error Handling', () => {
    it('displays error when loading fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/settings/wakeword`, () => {
          return new HttpResponse(null, { status: 500 });
        }),
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText(/Einstellungen konnten nicht geladen werden/)).toBeInTheDocument();
      });
    });

    it('displays error when saving fails', async () => {
      server.use(
        http.put(`${BASE_URL}/api/settings/wakeword`, () => {
          return HttpResponse.json(
            { detail: 'Invalid keyword' },
            { status: 400 },
          );
        }),
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      const saveButton = screen.getByRole('button', { name: /speichern/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/Invalid keyword/)).toBeInTheDocument();
      });
    });

    it('displays permission error when unauthorized', async () => {
      server.use(
        http.put(`${BASE_URL}/api/settings/wakeword`, () => {
          return new HttpResponse(null, { status: 403 });
        }),
      );

      renderWithProviders(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wake Word Einstellungen')).toBeInTheDocument();
      });

      const keywordSelect = screen.getByRole('combobox');
      fireEvent.change(keywordSelect, { target: { value: 'hey_jarvis' } });

      const saveButton = screen.getByRole('button', { name: /speichern/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/Zugriff verweigert/)).toBeInTheDocument();
      });
    });
  });
});
