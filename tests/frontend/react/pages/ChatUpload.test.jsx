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

  it('shows upload confirmation message after successful upload', async () => {
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

    // The upload confirmation message should appear (i18n key: chat.documentUploaded)
    await waitFor(() => {
      expect(screen.getByText(/Dokument hochgeladen.*report\.pdf/)).toBeInTheDocument();
    }, { timeout: 5000 });
  });
});
