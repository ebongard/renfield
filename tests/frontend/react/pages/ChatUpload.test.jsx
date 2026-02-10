import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL } from '../mocks/handlers.js';
import ChatPage from '../../../../src/frontend/src/pages/ChatPage';
import { renderWithProviders } from '../test-utils.jsx';

// Mock useWakeWord hook
vi.mock('../../../../src/frontend/src/hooks/useWakeWord', () => ({
  useWakeWord: () => ({
    isEnabled: false,
    isListening: false,
    isLoading: false,
    isReady: false,
    isAvailable: false,
    lastDetection: null,
    error: null,
    settings: { keyword: 'hey_jarvis', threshold: 0.5 },
    enable: vi.fn(),
    disable: vi.fn(),
    toggle: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    setKeyword: vi.fn(),
    setThreshold: vi.fn(),
    availableKeywords: [{ id: 'hey_jarvis', label: 'Hey Jarvis' }]
  })
}));

// Mock WAKEWORD_CONFIG
vi.mock('../../../../src/frontend/src/config/wakeword', () => ({
  WAKEWORD_CONFIG: {
    activationDelayMs: 100
  }
}));

// Mock navigator.mediaDevices
const mockGetUserMedia = vi.fn();
Object.defineProperty(global.navigator, 'mediaDevices', {
  value: { getUserMedia: mockGetUserMedia },
  writable: true,
  configurable: true
});

// Mock AudioContext
global.AudioContext = vi.fn().mockImplementation(() => ({
  state: 'running',
  resume: vi.fn().mockResolvedValue(undefined),
  close: vi.fn().mockResolvedValue(undefined),
  createMediaStreamSource: vi.fn().mockReturnValue({ connect: vi.fn() }),
  createAnalyser: vi.fn().mockReturnValue({
    fftSize: 512,
    frequencyBinCount: 256,
    smoothingTimeConstant: 0.3,
    getByteFrequencyData: vi.fn()
  }),
  createBufferSource: vi.fn().mockReturnValue({
    buffer: null,
    connect: vi.fn(),
    start: vi.fn(),
    onended: null
  }),
  decodeAudioData: vi.fn().mockResolvedValue({ duration: 1.0 }),
  destination: {}
}));
global.webkitAudioContext = global.AudioContext;

// Mock MediaRecorder
global.MediaRecorder = vi.fn().mockImplementation(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  ondataavailable: null,
  onstop: null
}));

// Mock WebSocket
class MockWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.OPEN = 1;
    setTimeout(() => { if (this.onopen) this.onopen({}); }, 10);
  }
  send() {}
  close() { if (this.onclose) this.onclose({}); }
}
global.WebSocket = MockWebSocket;

// Mock scrollIntoView
Element.prototype.scrollIntoView = vi.fn();

describe('Chat Upload', () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`${BASE_URL}/api/knowledge/bases`, () => {
        return HttpResponse.json([]);
      })
    );
    mockGetUserMedia.mockResolvedValue({
      getTracks: () => [{ stop: vi.fn() }]
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('renders the upload button', async () => {
    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      const attachButton = screen.getByLabelText('Datei anhängen');
      expect(attachButton).toBeInTheDocument();
    });
  });

  it('has a hidden file input', async () => {
    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      const fileInput = document.querySelector('input[type="file"]');
      expect(fileInput).toBeTruthy();
      expect(fileInput.className).toContain('hidden');
    });
  });

  it('accepts correct file types', async () => {
    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      const fileInput = document.querySelector('input[type="file"]');
      expect(fileInput).toBeTruthy();
      expect(fileInput.getAttribute('accept')).toContain('.pdf');
      expect(fileInput.getAttribute('accept')).toContain('.txt');
      expect(fileInput.getAttribute('accept')).toContain('.docx');
    });
  });

  it('renders attachment chips from loaded history', async () => {
    // Mock a conversation with attachments in history
    const sessionId = 'session-with-attachments';
    server.use(
      http.get(`${BASE_URL}/api/chat/conversations`, () => {
        return HttpResponse.json({
          conversations: [{
            session_id: sessionId,
            preview: 'Check this doc',
            message_count: 2,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }],
          total: 1,
        });
      }),
      http.get(`${BASE_URL}/api/chat/history/${sessionId}`, () => {
        return HttpResponse.json({
          messages: [
            {
              role: 'user',
              content: 'What does this say?',
              timestamp: new Date().toISOString(),
              metadata: { attachment_ids: [42] },
              attachments: [
                { id: 42, filename: 'quarterly.pdf', file_type: 'pdf', file_size: 8000, status: 'completed' },
              ],
            },
            {
              role: 'assistant',
              content: 'The document talks about earnings.',
              timestamp: new Date().toISOString(),
              metadata: null,
            },
          ],
        });
      })
    );

    // Set localStorage to this session so it loads history on mount
    localStorage.setItem('renfield_current_session', sessionId);

    renderWithProviders(<ChatPage />);

    // The attachment chip from history should appear
    await waitFor(() => {
      expect(screen.getByText(/quarterly\.pdf/)).toBeInTheDocument();
    }, { timeout: 5000 });

    // Clean up
    localStorage.removeItem('renfield_current_session');
  });

  it('renders quick actions menu on attachment chip', async () => {
    const sessionId = 'session-quick-actions';
    server.use(
      http.get(`${BASE_URL}/api/chat/conversations`, () => {
        return HttpResponse.json({
          conversations: [{
            session_id: sessionId,
            preview: 'Check doc',
            message_count: 1,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }],
          total: 1,
        });
      }),
      http.get(`${BASE_URL}/api/chat/history/${sessionId}`, () => {
        return HttpResponse.json({
          messages: [
            {
              role: 'user',
              content: 'Check this doc',
              timestamp: new Date().toISOString(),
              metadata: { attachment_ids: [10] },
              attachments: [
                { id: 10, filename: 'report.pdf', file_type: 'pdf', file_size: 5000, status: 'completed' },
              ],
            },
          ],
        });
      })
    );

    localStorage.setItem('renfield_current_session', sessionId);
    const user = userEvent.setup();
    renderWithProviders(<ChatPage />);

    // Wait for attachment chip to appear
    await waitFor(() => {
      expect(screen.getByText(/report\.pdf/)).toBeInTheDocument();
    }, { timeout: 5000 });

    // Click the 3-dot menu button (quick actions)
    const quickActionBtn = screen.getByLabelText('Schnellaktionen');
    await user.click(quickActionBtn);

    // Menu items should be visible
    await waitFor(() => {
      expect(screen.getByText('Zur Wissensdatenbank')).toBeInTheDocument();
      expect(screen.getByText('An Paperless senden')).toBeInTheDocument();
      expect(screen.getByText('Zusammenfassen')).toBeInTheDocument();
    });

    localStorage.removeItem('renfield_current_session');
  });

  it('allows multiple file selection', async () => {
    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      const fileInput = document.querySelector('input[type="file"]');
      expect(fileInput).toBeTruthy();
      expect(fileInput.hasAttribute('multiple')).toBe(true);
    });
  });

  it('shows upload progress indicators', async () => {
    // Create a delayed upload response to catch the progress state
    let resolveUpload;
    const uploadPromise = new Promise(resolve => { resolveUpload = resolve; });

    server.use(
      http.post(`${BASE_URL}/api/chat/upload`, async () => {
        await uploadPromise;
        return HttpResponse.json({
          id: 1,
          filename: 'report.pdf',
          file_type: 'pdf',
          file_size: 12345,
          status: 'completed',
          text_preview: 'text',
          error_message: null,
          created_at: '2026-02-10T12:00:00',
        });
      })
    );

    const user = userEvent.setup();
    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByLabelText('Datei anhängen')).toBeInTheDocument();
    });

    const fileInput = document.querySelector('input[type="file"]');
    const file = new File(['test content'], 'progress-test.pdf', { type: 'application/pdf' });

    await user.upload(fileInput, file);

    // The progress bar should be rendered (role="progressbar")
    await waitFor(() => {
      const progressBar = document.querySelector('[role="progressbar"]');
      expect(progressBar).toBeTruthy();
    }, { timeout: 3000 });

    // Resolve the upload so it completes
    resolveUpload();

    // After resolution, the progress bar should disappear
    await waitFor(() => {
      expect(screen.getByText(/progress-test\.pdf/)).toBeInTheDocument();
    }, { timeout: 5000 });
  });

  it('renders email quick action in menu', async () => {
    const sessionId = 'session-email-action';
    server.use(
      http.get(`${BASE_URL}/api/chat/conversations`, () => {
        return HttpResponse.json({
          conversations: [{
            session_id: sessionId,
            preview: 'Check doc',
            message_count: 1,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }],
          total: 1,
        });
      }),
      http.get(`${BASE_URL}/api/chat/history/${sessionId}`, () => {
        return HttpResponse.json({
          messages: [
            {
              role: 'user',
              content: 'Check this doc',
              timestamp: new Date().toISOString(),
              metadata: { attachment_ids: [20] },
              attachments: [
                { id: 20, filename: 'invoice.pdf', file_type: 'pdf', file_size: 3000, status: 'completed' },
              ],
            },
          ],
        });
      })
    );

    localStorage.setItem('renfield_current_session', sessionId);
    const user = userEvent.setup();
    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByText(/invoice\.pdf/)).toBeInTheDocument();
    }, { timeout: 5000 });

    const quickActionBtn = screen.getByLabelText('Schnellaktionen');
    await user.click(quickActionBtn);

    await waitFor(() => {
      expect(screen.getByText('Per E-Mail senden')).toBeInTheDocument();
    });

    localStorage.removeItem('renfield_current_session');
  });

  it('shows email success toast', async () => {
    const sessionId = 'session-email-toast';
    server.use(
      http.get(`${BASE_URL}/api/chat/conversations`, () => {
        return HttpResponse.json({
          conversations: [{
            session_id: sessionId,
            preview: 'Check doc',
            message_count: 1,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }],
          total: 1,
        });
      }),
      http.get(`${BASE_URL}/api/chat/history/${sessionId}`, () => {
        return HttpResponse.json({
          messages: [
            {
              role: 'user',
              content: 'Check this doc',
              timestamp: new Date().toISOString(),
              metadata: { attachment_ids: [30] },
              attachments: [
                { id: 30, filename: 'memo.pdf', file_type: 'pdf', file_size: 2000, status: 'completed' },
              ],
            },
          ],
        });
      }),
      http.post(`${BASE_URL}/api/chat/upload/30/email`, () => {
        return HttpResponse.json({
          success: true,
          message: 'Sent to test@example.com',
        });
      })
    );

    localStorage.setItem('renfield_current_session', sessionId);
    const user = userEvent.setup();
    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByText(/memo\.pdf/)).toBeInTheDocument();
    }, { timeout: 5000 });

    // Open quick actions menu
    const quickActionBtn = screen.getByLabelText('Schnellaktionen');
    await user.click(quickActionBtn);

    // Click "Per E-Mail senden"
    await waitFor(() => {
      expect(screen.getByText('Per E-Mail senden')).toBeInTheDocument();
    });
    await user.click(screen.getByText('Per E-Mail senden'));

    // Email dialog should appear
    await waitFor(() => {
      expect(screen.getByText('Dokument per E-Mail senden')).toBeInTheDocument();
    });

    // Fill in email address and submit
    const emailInput = screen.getByPlaceholderText('user@example.com');
    await user.type(emailInput, 'test@example.com');
    await user.click(screen.getByText('Senden'));

    // Success toast should appear
    await waitFor(() => {
      expect(screen.getByText('Per E-Mail gesendet')).toBeInTheDocument();
    }, { timeout: 5000 });

    localStorage.removeItem('renfield_current_session');
  });

  it('shows attachment chip after successful upload', async () => {
    server.use(
      http.post(`${BASE_URL}/api/chat/upload`, () => {
        return HttpResponse.json({
          id: 1,
          filename: 'report.pdf',
          file_type: 'pdf',
          file_size: 12345,
          status: 'completed',
          text_preview: 'Extracted text from the report...',
          error_message: null,
          created_at: '2026-02-09T12:00:00',
        });
      })
    );

    const user = userEvent.setup();
    renderWithProviders(<ChatPage />);

    // Wait for page to render
    await waitFor(() => {
      expect(screen.getByLabelText('Datei anhängen')).toBeInTheDocument();
    });

    // Simulate file selection
    const fileInput = document.querySelector('input[type="file"]');
    const file = new File(['test content'], 'report.pdf', { type: 'application/pdf' });

    await user.upload(fileInput, file);

    // The attachment chip should appear (shows filename)
    await waitFor(() => {
      expect(screen.getByText(/report\.pdf/)).toBeInTheDocument();
    }, { timeout: 5000 });
  });
});
