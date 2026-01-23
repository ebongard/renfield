import React, { useState, useRef, useEffect } from 'react';
import { Sun, Moon, Monitor } from 'lucide-react';
import { useTheme } from '../context/ThemeContext';

/**
 * Theme toggle button with dropdown for light/dark/system selection.
 */
export default function ThemeToggle() {
  const { theme, isDark, setTheme } = useTheme();
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Close dropdown on escape key
  useEffect(() => {
    const handleEscape = (event) => {
      if (event.key === 'Escape') {
        setIsOpen(false);
      }
    };

    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, []);

  const options = [
    { value: 'light', label: 'Hell', icon: Sun },
    { value: 'dark', label: 'Dunkel', icon: Moon },
    { value: 'system', label: 'System', icon: Monitor }
  ];

  const currentIcon = theme === 'system' ? Monitor : isDark ? Moon : Sun;
  const CurrentIcon = currentIcon;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="p-2 rounded-lg transition-colors
                   text-gray-600 hover:bg-gray-200
                   dark:text-gray-300 dark:hover:bg-gray-700"
        aria-label="Theme wechseln"
        aria-expanded={isOpen}
        aria-haspopup="true"
      >
        <CurrentIcon className="w-5 h-5" aria-hidden="true" />
      </button>

      {isOpen && (
        <div
          className="absolute right-0 mt-2 w-36 py-1 rounded-lg shadow-lg
                     bg-white border border-gray-200
                     dark:bg-gray-800 dark:border-gray-700
                     z-50"
          role="menu"
          aria-orientation="vertical"
        >
          {options.map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              onClick={() => {
                setTheme(value);
                setIsOpen(false);
              }}
              className={`w-full flex items-center gap-2 px-4 py-2 text-sm transition-colors
                ${theme === value
                  ? 'bg-primary-50 text-primary-700 dark:bg-primary-900/30 dark:text-primary-400'
                  : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
                }`}
              role="menuitem"
            >
              <Icon className="w-4 h-4" aria-hidden="true" />
              <span>{label}</span>
              {theme === value && (
                <span className="ml-auto text-primary-600 dark:text-primary-400">
                  <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                    <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                  </svg>
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
