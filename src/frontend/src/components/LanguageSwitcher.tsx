import { useState, useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Globe, ChevronDown, Check } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import {
  useLanguagePreferenceQuery,
  useSetLanguagePreference,
} from '../api/resources/preferences';

type LanguageCode = 'de' | 'en';

interface Language {
  code: LanguageCode;
  name: string;
  flag: string;
}

const LANGUAGES: Language[] = [
  { code: 'de', name: 'Deutsch', flag: '🇩🇪' },
  { code: 'en', name: 'English', flag: '🇬🇧' },
];

interface LanguageSwitcherProps {
  compact?: boolean;
}

export default function LanguageSwitcher({ compact = false }: LanguageSwitcherProps) {
  const { i18n, t } = useTranslation();
  const { isAuthenticated, authEnabled } = useAuth();
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement | null>(null);
  const initialLoadDone = useRef(false);

  const currentLanguage = LANGUAGES.find((lang) => lang.code === i18n.language) ?? LANGUAGES[0];

  const enabled = Boolean(authEnabled && isAuthenticated);
  const languageQuery = useLanguagePreferenceQuery(enabled);
  const setLanguageMutation = useSetLanguagePreference();

  useEffect(() => {
    if (initialLoadDone.current) return;
    const userLanguage = languageQuery.data?.language;
    if (userLanguage && userLanguage !== i18n.language) {
      i18n.changeLanguage(userLanguage);
    }
    if (languageQuery.data !== undefined) {
      initialLoadDone.current = true;
    }
  }, [languageQuery.data, i18n]);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (dropdownRef.current && target && !dropdownRef.current.contains(target)) {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsOpen(false);
      }
    };

    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, []);

  const changeLanguage = async (code: LanguageCode) => {
    await i18n.changeLanguage(code);
    setIsOpen(false);
    if (enabled) {
      try {
        await setLanguageMutation.mutateAsync(code);
      } catch {
        // Non-critical: language already changed locally
      }
    }
  };

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center space-x-1.5 px-3 py-2.5 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
        aria-expanded={isOpen}
        aria-haspopup="listbox"
        aria-label={t('language.label')}
      >
        {compact ? (
          <>
            <span className="text-base">{currentLanguage.flag}</span>
            <ChevronDown className={`w-3 h-3 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
          </>
        ) : (
          <>
            <Globe className="w-4 h-4" />
            <span className="text-sm">{currentLanguage.code.toUpperCase()}</span>
            <ChevronDown className={`w-3 h-3 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
          </>
        )}
      </button>

      {isOpen && (
        <div
          className="absolute right-0 mt-2 w-40 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 py-1 z-50"
          role="listbox"
          aria-label={t('language.label')}
        >
          {LANGUAGES.map((lang) => (
            <button
              key={lang.code}
              onClick={() => changeLanguage(lang.code)}
              className={`w-full flex items-center justify-between px-3 py-2 text-sm transition-colors ${
                i18n.language === lang.code
                  ? 'bg-primary-100 text-primary-700 dark:bg-primary-600/20 dark:text-primary-400'
                  : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
              role="option"
              aria-selected={i18n.language === lang.code}
            >
              <div className="flex items-center space-x-2">
                <span className="text-base">{lang.flag}</span>
                <span>{lang.name}</span>
              </div>
              {i18n.language === lang.code && <Check className="w-4 h-4" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
