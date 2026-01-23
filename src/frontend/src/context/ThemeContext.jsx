import React, { createContext, useContext, useState, useEffect } from 'react';

const ThemeContext = createContext(undefined);

// LocalStorage key for theme preference
const THEME_STORAGE_KEY = 'renfield_theme';

/**
 * ThemeProvider manages the application's color scheme.
 * Supports 'light', 'dark', and 'system' (follows OS preference).
 */
export function ThemeProvider({ children }) {
  // Initialize theme from localStorage or default to 'system'
  const [theme, setTheme] = useState(() => {
    if (typeof window === 'undefined') return 'system';
    return localStorage.getItem(THEME_STORAGE_KEY) || 'system';
  });

  // Computed dark mode state
  const [isDark, setIsDark] = useState(false);

  // Update the actual theme based on preference
  useEffect(() => {
    const updateTheme = () => {
      let shouldBeDark;

      if (theme === 'system') {
        // Follow system preference
        shouldBeDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      } else {
        shouldBeDark = theme === 'dark';
      }

      setIsDark(shouldBeDark);

      // Apply/remove dark class on document element
      if (shouldBeDark) {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }

      // Update meta theme-color for mobile browsers
      const metaThemeColor = document.querySelector('meta[name="theme-color"]');
      if (metaThemeColor) {
        metaThemeColor.setAttribute('content', shouldBeDark ? '#1f2937' : '#f9fafb');
      }
    };

    updateTheme();

    // Listen for system preference changes when in 'system' mode
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = () => {
      if (theme === 'system') {
        updateTheme();
      }
    };

    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, [theme]);

  // Persist theme preference to localStorage
  useEffect(() => {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  /**
   * Set theme preference
   * @param {'light' | 'dark' | 'system'} newTheme
   */
  const setThemePreference = (newTheme) => {
    setTheme(newTheme);
  };

  /**
   * Toggle between light and dark (ignores system)
   */
  const toggleTheme = () => {
    setTheme(prev => {
      if (prev === 'system') {
        // If system, toggle based on current computed state
        return isDark ? 'light' : 'dark';
      }
      return prev === 'dark' ? 'light' : 'dark';
    });
  };

  const value = {
    theme,           // 'light' | 'dark' | 'system'
    isDark,          // computed boolean
    setTheme: setThemePreference,
    toggleTheme
  };

  return (
    <ThemeContext.Provider value={value}>
      {children}
    </ThemeContext.Provider>
  );
}

/**
 * Hook to access theme context
 */
export function useTheme() {
  const context = useContext(ThemeContext);
  if (context === undefined) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return context;
}

export default ThemeContext;
