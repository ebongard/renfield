import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  ThemeProvider,
  useTheme,
} from '../../../../src/frontend/src/context/ThemeContext';

// Test component that uses the theme context.
// Type comes from the real `useTheme` return — no local re-declaration.
function TestConsumer() {
  const { theme, isDark, setTheme, toggleTheme } = useTheme();

  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <span data-testid="is-dark">{isDark ? 'dark' : 'light'}</span>
      <button onClick={() => setTheme('light')}>Set Light</button>
      <button onClick={() => setTheme('dark')}>Set Dark</button>
      <button onClick={() => setTheme('system')}>Set System</button>
      <button onClick={toggleTheme}>Toggle</button>
    </div>
  );
}

// Shape that ThemeContext needs from window.matchMedia: enough of MediaQueryList
// to satisfy the addEventListener/removeEventListener('change', ...) calls.
type MatchMediaListener = (event: MediaQueryListEvent) => void;
type MatchMediaImpl = (query: string) => MediaQueryList;

describe('ThemeContext', () => {
  let matchMediaMock: Mock<MatchMediaImpl>;
  let matchMediaListeners: MatchMediaListener[] = [];

  beforeEach(() => {
    // Clear localStorage mock
    window.localStorage.clear();
    vi.clearAllMocks();
    matchMediaListeners = [];

    // Reset document class
    document.documentElement.classList.remove('dark');

    // Create a controllable matchMedia mock
    matchMediaMock = vi.fn<MatchMediaImpl>().mockImplementation((query: string) => ({
      matches: false, // Default to light system preference
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn((event: string, listener: EventListenerOrEventListenerObject) => {
        if (event === 'change' && typeof listener === 'function') {
          matchMediaListeners.push(listener as MatchMediaListener);
        }
      }),
      removeEventListener: vi.fn((event: string, listener: EventListenerOrEventListenerObject) => {
        if (event === 'change') {
          matchMediaListeners = matchMediaListeners.filter(l => l !== listener);
        }
      }),
      dispatchEvent: vi.fn(),
    } as unknown as MediaQueryList));

    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: matchMediaMock,
    });
  });

  afterEach(() => {
    document.documentElement.classList.remove('dark');
  });

  describe('ThemeProvider', () => {
    it('renders children', () => {
      render(
        <ThemeProvider>
          <div data-testid="child">Hello</div>
        </ThemeProvider>
      );

      expect(screen.getByTestId('child')).toBeInTheDocument();
    });

    it('provides default theme value of system', () => {
      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      expect(screen.getByTestId('theme')).toHaveTextContent('system');
    });

    it('reads initial theme from localStorage', () => {
      window.localStorage.setItem('renfield_theme', 'dark');

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      expect(screen.getByTestId('theme')).toHaveTextContent('dark');
    });

    it('persists theme changes to localStorage', async () => {
      const user = userEvent.setup();

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      await user.click(screen.getByText('Set Dark'));

      expect(window.localStorage.setItem).toHaveBeenCalledWith('renfield_theme', 'dark');
    });
  });

  describe('setTheme', () => {
    it('changes theme to light', async () => {
      const user = userEvent.setup();

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      await user.click(screen.getByText('Set Light'));

      expect(screen.getByTestId('theme')).toHaveTextContent('light');
      expect(screen.getByTestId('is-dark')).toHaveTextContent('light');
      expect(document.documentElement.classList.contains('dark')).toBe(false);
    });

    it('changes theme to dark', async () => {
      const user = userEvent.setup();

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      await user.click(screen.getByText('Set Dark'));

      expect(screen.getByTestId('theme')).toHaveTextContent('dark');
      expect(screen.getByTestId('is-dark')).toHaveTextContent('dark');
      expect(document.documentElement.classList.contains('dark')).toBe(true);
    });

    it('changes theme to system', async () => {
      const user = userEvent.setup();
      window.localStorage.setItem('renfield_theme', 'dark');

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      await user.click(screen.getByText('Set System'));

      expect(screen.getByTestId('theme')).toHaveTextContent('system');
    });
  });

  describe('toggleTheme', () => {
    it('toggles from light to dark', async () => {
      const user = userEvent.setup();
      window.localStorage.setItem('renfield_theme', 'light');

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      await user.click(screen.getByText('Toggle'));

      expect(screen.getByTestId('theme')).toHaveTextContent('dark');
    });

    it('toggles from dark to light', async () => {
      const user = userEvent.setup();
      window.localStorage.setItem('renfield_theme', 'dark');

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      await user.click(screen.getByText('Toggle'));

      expect(screen.getByTestId('theme')).toHaveTextContent('light');
    });

    it('toggles from system to explicit theme based on current state', async () => {
      const user = userEvent.setup();
      // System preference is light (matches: false = light)

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      // Theme should be 'system', isDark should be false (light)
      expect(screen.getByTestId('theme')).toHaveTextContent('system');
      expect(screen.getByTestId('is-dark')).toHaveTextContent('light');

      await user.click(screen.getByText('Toggle'));

      // Should toggle to dark
      expect(screen.getByTestId('theme')).toHaveTextContent('dark');
    });
  });

  describe('system preference', () => {
    it('follows system dark preference when theme is system', () => {
      // Mock system preference as dark
      matchMediaMock.mockImplementation((query: string) => ({
        matches: true, // Dark system preference
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      } as unknown as MediaQueryList));

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      expect(screen.getByTestId('theme')).toHaveTextContent('system');
      expect(screen.getByTestId('is-dark')).toHaveTextContent('dark');
      expect(document.documentElement.classList.contains('dark')).toBe(true);
    });

    it('follows system light preference when theme is system', () => {
      // matchMedia already mocked to return matches: false (light)

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      expect(screen.getByTestId('theme')).toHaveTextContent('system');
      expect(screen.getByTestId('is-dark')).toHaveTextContent('light');
      expect(document.documentElement.classList.contains('dark')).toBe(false);
    });
  });

  describe('dark class application', () => {
    it('adds dark class when theme is dark', async () => {
      const user = userEvent.setup();

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      await user.click(screen.getByText('Set Dark'));

      expect(document.documentElement.classList.contains('dark')).toBe(true);
    });

    it('removes dark class when theme is light', async () => {
      const user = userEvent.setup();
      window.localStorage.setItem('renfield_theme', 'dark');

      render(
        <ThemeProvider>
          <TestConsumer />
        </ThemeProvider>
      );

      // Initially dark
      expect(document.documentElement.classList.contains('dark')).toBe(true);

      await user.click(screen.getByText('Set Light'));

      expect(document.documentElement.classList.contains('dark')).toBe(false);
    });
  });

  describe('useTheme hook', () => {
    it('throws error when used outside provider', () => {
      // Suppress console.error for this test
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

      expect(() => {
        render(<TestConsumer />);
      }).toThrow('useTheme must be used within a ThemeProvider');

      consoleSpy.mockRestore();
    });

    it('returns all expected values', () => {
      let contextValue: ReturnType<typeof useTheme> | undefined;

      function Capture() {
        contextValue = useTheme();
        return null;
      }

      render(
        <ThemeProvider>
          <Capture />
        </ThemeProvider>
      );

      expect(contextValue).toHaveProperty('theme');
      expect(contextValue).toHaveProperty('isDark');
      expect(contextValue).toHaveProperty('setTheme');
      expect(contextValue).toHaveProperty('toggleTheme');
      expect(typeof contextValue?.setTheme).toBe('function');
      expect(typeof contextValue?.toggleTheme).toBe('function');
    });
  });
});
