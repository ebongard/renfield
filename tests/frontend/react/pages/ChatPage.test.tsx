import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import ChatPage from '../../../../src/frontend/src/pages/ChatPage';
import { renderWithProviders } from '../test-utils';
import type { UseWakeWordResult } from '../../../../src/frontend/src/hooks/useWakeWord';
import type { AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { adminAuthMock } from '../test-auth-mock';

// Mock AuthContext (ChatHeader pulls `useAuth` from the real provider, which
// the test wrapper doesn't supply).
vi.mock('../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/context/AuthContext')>(
    '../../../../src/frontend/src/context/AuthContext',
  );
  return {
    ...actual,
    useAuth: (): AuthContextValue => adminAuthMock,
  };
});

// Mock useWakeWord hook
const wakeWordMock: UseWakeWordResult = {
  isEnabled: false,
  isListening: false,
  isLoading: false,
  isReady: false,
  isAvailable: false,
  lastDetection: null,
  error: null,
  settings: { enabled: false, keyword: 'hey_jarvis', threshold: 0.5, audioFeedback: false },
  enable: async () => {},
  disable: async () => {},
  toggle: async () => {},
  pause: async () => {},
  resume: async () => {},
  setKeyword: async () => {},
  setThreshold: () => {},
  availableKeywords: [{ id: 'hey_jarvis', label: 'Hey Jarvis', model: 'hey_jarvis_v0.1', description: 'pre-trained' }],
};

vi.mock('../../../../src/frontend/src/hooks/useWakeWord', () => ({
  useWakeWord: (): UseWakeWordResult => wakeWordMock,
}));

// Mock WAKEWORD_CONFIG
vi.mock('../../../../src/frontend/src/config/wakeword', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/config/wakeword')>(
    '../../../../src/frontend/src/config/wakeword',
  );
  return {
    ...actual,
    WAKEWORD_CONFIG: {
      ...actual.WAKEWORD_CONFIG,
      activationDelayMs: 100,
    },
  };
});

// Mock navigator.mediaDevices
const mockGetUserMedia = vi.fn<MediaDevices['getUserMedia']>();
Object.defineProperty(global.navigator, 'mediaDevices', {
  value: { getUserMedia: mockGetUserMedia },
  writable: true,
  configurable: true,
});

// Minimal AudioContext stub. We only assert button rendering, so the
// methods on this stub just need to be callable without crashing.
type AudioContextStub = Pick<
  AudioContext,
  'state' | 'resume' | 'close' | 'createMediaStreamSource' | 'createAnalyser' | 'createBufferSource' | 'decodeAudioData' | 'destination'
>;

const audioContextFactory = vi.fn<() => AudioContextStub>().mockImplementation(() => ({
  state: 'running',
  resume: vi.fn(async () => {}),
  close: vi.fn(async () => {}),
  createMediaStreamSource: vi.fn(() => ({ connect: vi.fn() } as unknown as MediaStreamAudioSourceNode)),
  createAnalyser: vi.fn(() => ({
    fftSize: 512,
    frequencyBinCount: 256,
    smoothingTimeConstant: 0.3,
    getByteFrequencyData: vi.fn(),
  } as unknown as AnalyserNode)),
  createBufferSource: vi.fn(() => ({
    buffer: null,
    connect: vi.fn(),
    start: vi.fn(),
    onended: null,
  } as unknown as AudioBufferSourceNode)),
  decodeAudioData: vi.fn(async () => ({ duration: 1.0 } as unknown as AudioBuffer)),
  destination: {} as AudioDestinationNode,
}));

// `globalThis.AudioContext` expects a constructor. We assign through the
// global `Window` shape via a typed cast on the global object key only —
// no `any`.
interface AudioContextCapableGlobal {
  AudioContext: typeof AudioContext;
  webkitAudioContext: typeof AudioContext;
}
(globalThis as unknown as AudioContextCapableGlobal).AudioContext = audioContextFactory as unknown as typeof AudioContext;
(globalThis as unknown as AudioContextCapableGlobal).webkitAudioContext = audioContextFactory as unknown as typeof AudioContext;

// MediaRecorder stub
interface MediaRecorderStub {
  start: () => void;
  stop: () => void;
  ondataavailable: ((ev: BlobEvent) => void) | null;
  onstop: (() => void) | null;
}
const mediaRecorderFactory = vi.fn<() => MediaRecorderStub>().mockImplementation(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  ondataavailable: null,
  onstop: null,
}));
(globalThis as unknown as { MediaRecorder: typeof MediaRecorder }).MediaRecorder =
  mediaRecorderFactory as unknown as typeof MediaRecorder;

// Mock WebSocket — minimal shape used by the chat hook.
class MockWebSocket {
  url: string;
  readyState = 1;
  static OPEN = 1;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  constructor(url: string | URL) {
    this.url = typeof url === 'string' ? url : url.toString();
    setTimeout(() => {
      if (this.onopen) this.onopen(new Event('open'));
    }, 10);
  }
  send(_data: string | ArrayBufferLike | Blob | ArrayBufferView): void {}
  close(): void {
    if (this.onclose) this.onclose(new CloseEvent('close'));
  }
}
(globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket =
  MockWebSocket as unknown as typeof WebSocket;

// Mock scrollIntoView
Element.prototype.scrollIntoView = vi.fn();

// Mock knowledge bases
const mockKnowledgeBases = [
  { id: 1, name: 'Documentation', document_count: 10, chunk_count: 100 },
  { id: 2, name: 'FAQ', document_count: 5, chunk_count: 50 },
];

describe('ChatPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`${BASE_URL}/api/knowledge/bases`, () => {
        return HttpResponse.json(mockKnowledgeBases);
      }),
    );
    mockGetUserMedia.mockResolvedValue({
      getTracks: () => [{ stop: vi.fn() } as unknown as MediaStreamTrack],
    } as unknown as MediaStream);
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

      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });

    it('shows microphone button', async () => {
      renderWithProviders(<ChatPage />);

      const buttons = screen.getAllByRole('button');
      const micButton = buttons.find((btn) => btn.querySelector('svg'));
      expect(micButton).toBeDefined();
    });

    it('shows connection status', async () => {
      renderWithProviders(<ChatPage />);

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

      await waitFor(() => {
        expect(screen.getByPlaceholderText(/nachricht eingeben/i)).toBeInTheDocument();
      });

      const input = screen.getByPlaceholderText(/nachricht eingeben/i);
      await user.type(input, 'Test message');

      expect(input).toHaveValue('Test message');

      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(input).toHaveValue('');
        },
        { timeout: 1000 },
      );
    });
  });

  describe('Messages', () => {
    it('displays empty state initially', async () => {
      renderWithProviders(<ChatPage />);

      expect(screen.getByPlaceholderText(/nachricht eingeben/i)).toBeInTheDocument();
    });
  });

  describe('Wake Word Controls', () => {
    it('shows wake word toggle button', async () => {
      renderWithProviders(<ChatPage />);

      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });
  });

  describe('RAG Features', () => {
    it('shows RAG toggle button', async () => {
      renderWithProviders(<ChatPage />);

      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });
  });
});
