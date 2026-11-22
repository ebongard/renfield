import type { TFunction } from 'i18next';

import type { SimbaUploadBlocked } from '../../api/resources/simbaIngest';

/** The finding "this file is over the portal's limit" in the viewer's language
 *  (#1609) — rendered from data, never the backend's German fallback sentence. */
export function tooLargeText(t: TFunction, language: string, blocked: SimbaUploadBlocked): string {
  const mb = (n: number) => new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(n);
  return t('simbaReview.tooLarge', { size: mb(blocked.size_mb), max: mb(blocked.max_mb) });
}
