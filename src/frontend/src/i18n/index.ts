import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import de from './locales/de.json';
import en from './locales/en.json';

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      de: { translation: de },
      en: { translation: en }
    },
    fallbackLng: 'de',
    supportedLngs: ['de', 'en'],
    detection: {
      order: ['localStorage', 'navigator'],
      caches: ['localStorage'],
      lookupLocalStorage: 'renfield_language'
    },
    interpolation: {
      escapeValue: false, // React already escapes values
      // Make {{appName}} resolve everywhere without each call site passing
      // it explicitly. Driven by the VITE_APP_NAME build arg so the same
      // bundle can be branded as Renfield (default) or Reva / etc. via
      // the white-label Dockerfile args.
      defaultVariables: {
        appName: import.meta.env.VITE_APP_NAME || 'Renfield',
      },
    }
  });

export default i18n;
