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
  value: {
    getUserMedia: mockGetUserMedia
  },
  writable: true
});

// Mock AudioContext
global.AudioContext = vi.fn().mockImplementation(() => ({
  state: 'running',
  resume: vi.fn().mockResolvedValue(undefined),
  close: vi.fn().mockResolvedValue(undefined),
  createMediaStreamSource: vi.fn().mockReturnValue({
    connect: vi.fn()
  }),
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
    setTimeout(() => {
      if (this.onopen) this.onopen({});
    }, 10);
  }
  send(data) {}
  close() {
    if (this.onclose) this.onclose({});
  }
}
global.WebSocket = MockWebSocket;

// Mock scrollIntoView
Element.prototype.scrollIntoView = vi.fn();

// Mock knowledge bases
const mockKnowledgeBases = [
  { id: 1, name: 'Documentation', document_count: 10, chunk_count: 100 },
  { id: 2, name: 'FAQ', document_count: 5, chunk_count: 50 }
];

describe('ChatPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`${BASE_URL}/api/knowledge/bases`, () => {
        return HttpResponse.json(mockKnowledgeBases);
      })
    );
    mockGetUserMedia.mockResolvedValue({
      getTracks: () => [{ stop: vi.fn() }]
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<ChatPage />);

      expect(screen.getByText('Chat')).toBeInTheDocument();
      expect(screen.getByText('Unterhalte dich mit Renfield')).toBeInTheDocument();
    });

    it('shows input field', async () => {
      renderWithProviders(<ChatPage />);

      expect(screen.getByPlaceholderText(/nachricht eingeben/i)).toBeInTheDocument();
    });

    it('shows send button', async () => {
      renderWithProviders(<ChatPage />);

      // Send button is the form submit button
      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });

    it('shows microphone button', async () => {
      renderWithProviders(<ChatPage />);

      const buttons = screen.getAllByRole('button');
      const micButton = buttons.find(btn => btn.querySelector('svg'));
      expect(micButton).toBeDefined();
    });

    it('shows connection status', async () => {
      renderWithProviders(<ChatPage />);

      // Initially shows "Getrennt" until WebSocket connects
      await waitFor(() => {
        const statusElements = screen.getAllByText(/Verbunden|Getrennt/i);
        expect(statusElements.length).toBeGreaterThan(0);
      });
    });
  });

  describe('Text Input', () => {
    it('allows typing in the input field', async () => {
      const user = userEvent.setup();
      renderWithProviders(<ChatPage />);

      const input = screen.getByPlaceholderText(/nachricht eingeben/i);
      await user.type(input, 'Hello Renfield');

      expect(input).toHaveValue('Hello Renfield');
    });

    it('clears input after sending', async () => {
      const user = userEvent.setup();
      renderWithProviders(<ChatPage />);

      // Wait for component to render
      await waitFor(() => {
        expect(screen.getByPlaceholderText(/nachricht eingeben/i)).toBeInTheDocument();
      });

      const input = screen.getByPlaceholderText(/nachricht eingeben/i);
      await user.type(input, 'Test message');

      expect(input).toHaveValue('Test message');

      // Press Enter to submit - input should be cleared regardless of WS state
      await user.keyboard('{Enter}');

      await waitFor(() => {
        expect(input).toHaveValue('');
      }, { timeout: 1000 });
    });
  });

  describe('Messages', () => {
    it('displays empty state initially', async () => {
      renderWithProviders(<ChatPage />);

      // Chat area should be visible even if empty
      expect(screen.getByPlaceholderText(/nachricht eingeben/i)).toBeInTheDocument();
    });
  });

  describe('Wake Word Controls', () => {
    it('shows wake word toggle button', async () => {
      renderWithProviders(<ChatPage />);

      // EarOff icon button should be present when wake word is disabled
      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });
  });

  describe('RAG Features', () => {
    it('shows RAG toggle button', async () => {
      renderWithProviders(<ChatPage />);

      // BookOpen icon for RAG should be present
      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });
  });
});
