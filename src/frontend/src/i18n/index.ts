import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import de from './locales/de.json';
import en from './locales/en.json';
import dePro from './locales/de.pro.json';
import enPro from './locales/en.pro.json';

/**
 * Recursively merge two plain-object trees. Source values win when keys
 * collide; nested objects merge instead of replacing wholesale. Used for
 * the `pro` edition translation overlay so each `*.pro.json` only needs
 * to list the keys it changes — everything else falls through to the
 * base locale.
 *
 * Not lodash to avoid the dep. Conservative: array values replace, only
 * plain objects merge. The `_comment` top-level key in the overlay JSON
 * is silently included; i18next ignores keys it isn't asked to look up.
 */
function deepMerge<T extends Record<string, unknown>>(base: T, overlay: Record<string, unknown>): T {
  const out: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(overlay)) {
    const baseVal = out[key];
    if (
      value && typeof value === 'object' && !Array.isArray(value) &&
      baseVal && typeof baseVal === 'object' && !Array.isArray(baseVal)
    ) {
      out[key] = deepMerge(baseVal as Record<string, unknown>, value as Record<string, unknown>);
    } else {
      out[key] = value;
    }
  }
  return out as T;
}

/**
 * Resolve final translation resources for the active edition.
 *
 * Editions:
 * - `community` (default): household-flavored vocabulary. Tier 2 = "Household",
 *   Settings page = "Circles & Members", peers come from "household members".
 *   This is the right Renfield single-household experience.
 * - `pro`: enterprise/banking-flavored vocabulary. Tier 2 = "Team", Settings
 *   page = "Visibility & Members", peers come from "organization members".
 *   Used by white-label deploys (e.g. Reva at X-IDRA Systems) that ship
 *   the same backend feature but talk to enterprise tenants where
 *   "household" is meaningless.
 *
 * Driven by the `VITE_APP_EDITION` build arg so the same source tree
 * produces both bundles. The pro overlays live alongside the base
 * locales as `de.pro.json` / `en.pro.json` and only list the keys that
 * differ from the base.
 */
const edition = (import.meta.env.VITE_APP_EDITION || 'community') as 'community' | 'pro';
const deResources = edition === 'pro' ? deepMerge(de, dePro as Record<string, unknown>) : de;
const enResources = edition === 'pro' ? deepMerge(en, enPro as Record<string, unknown>) : en;

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      de: { translation: deResources },
      en: { translation: enResources }
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
