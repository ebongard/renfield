import { useTranslation } from 'react-i18next';

import type { DocumentFact } from '../api/resources/brain';
import { daysUntil } from '../utils/frist';
import FactProvenance from './FactProvenance';
import TierBadge from './TierBadge';

/**
 * The shared obligation content cluster used by BOTH surfaces (D6): the Fakten
 * panel's "Fristen" group and the obligations agenda. Renders
 *   kind · amount · frist-distance · ⚑ rechtlich · provenance · tier
 * Surface-specific chrome (the source-document title, the Bestätigen button,
 * the atom-row card) is added by the caller around this cluster.
 */
interface ObligationRowProps {
  fact: DocumentFact;
  now: Date;
  /** Strike the kind label when the obligation has been acknowledged. */
  confirmed?: boolean;
  className?: string;
}

function MidDot() {
  return <span aria-hidden="true" className="text-gray-300 dark:text-gray-600">·</span>;
}

export default function ObligationRow({ fact, now, confirmed = false, className = '' }: ObligationRowProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language === 'de' ? 'de-DE' : 'en-US';

  const kindLabel = t(`obligations.kind.${fact.kind}`, { defaultValue: fact.kind });

  let amount: string | null = null;
  if (fact.amount_value != null) {
    const currency = (fact.amount_currency || 'EUR').toUpperCase();
    try {
      amount = new Intl.NumberFormat(locale, { style: 'currency', currency }).format(
        fact.amount_value,
      );
    } catch {
      // The fallback is still needed, but its REASONING was out of date: until
      // 2026-09-27 this said "backend TODO", as though the backend did not
      // validate. It has since 2026-06-18 (`_clean_currency` in the layer-A
      // extractor, the only write path -- re-checked on 2026-09-27: one
      // `DocumentFact(...)`, two raw SQL statements that do not touch the
      // column, no API write path).
      //
      // Why the `catch` stands regardless: the OLD ROWS are older than the
      // validation. Production holds a row with `KWH` as its currency, created
      // on 2026-06-01 21:27 -- 17 days before. `Intl.NumberFormat` throws a
      // `RangeError` on it mid-render; without this branch a single legacy row
      // takes the agenda down with it.
      amount = `${new Intl.NumberFormat(locale, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(fact.amount_value)} ${currency}`;
    }
  }

  let fristLabel = '';
  let absDate = '';
  if (fact.obligation_date) {
    const days = daysUntil(fact.obligation_date, now);
    absDate = new Date(`${fact.obligation_date.slice(0, 10)}T00:00:00`).toLocaleDateString(
      locale,
      { dateStyle: 'medium' },
    );
    if (Number.isNaN(days)) {
      fristLabel = absDate;
    } else if (days < 0) {
      fristLabel = t('obligations.frist.overdue', { count: -days, date: absDate });
    } else if (days === 0) {
      fristLabel = t('obligations.frist.today', { date: absDate });
    } else {
      fristLabel = t('obligations.frist.future', { count: days, date: absDate });
    }
  }

  return (
    <div className={`flex flex-wrap items-center gap-x-2 gap-y-1 text-sm ${className}`}>
      <span
        className={
          confirmed
            ? 'line-through text-gray-400 dark:text-gray-500'
            : 'font-medium text-gray-900 dark:text-white'
        }
      >
        {kindLabel}
      </span>
      {amount && (
        <>
          <MidDot />
          <span className="tabular-nums text-gray-700 dark:text-gray-300">{amount}</span>
        </>
      )}
      {fristLabel && (
        <>
          <MidDot />
          <span className="text-gray-500 dark:text-gray-400">{fristLabel}</span>
        </>
      )}
      {fact.legal_gate && (
        <span className="legal-flag" aria-label={t('obligations.legalAria')}>
          <span aria-hidden="true">⚑</span>
          {t('obligations.legal')}
        </span>
      )}
      <FactProvenance source={fact.source} confidence={fact.confidence} />
      <TierBadge tier={fact.circle_tier} className="ml-auto" />
    </div>
  );
}
