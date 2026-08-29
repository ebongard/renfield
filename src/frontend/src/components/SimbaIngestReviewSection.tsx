import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Landmark, CheckCircle2 } from 'lucide-react';

import {
  useSimbaProposalsQuery,
  useSimbaCategoriesQuery,
} from '../api/resources/simbaIngest';
import SimbaProposalForm, { type SimbaResolution } from './simba/SimbaProposalForm';

interface Props {
  /** From the `simba_ingest_review_enabled` feature flag. */
  enabled: boolean;
}

/**
 * Review queue for Simba tax-portal proposals (xidra): watch-folder PDFs and
 * documents queued via the doc-page "send to Simba" overlay. The upload is
 * irreversible, so each file is confirmed here — with an editable, prefilled
 * category/type/Bezeichnung — via the shared {@link SimbaProposalForm}. A
 * successful upload flashes a positive acknowledgement at the top of the section
 * (the row itself is removed once the proposal is no longer pending). Renders
 * nothing when disabled or when the queue is empty.
 */
export default function SimbaIngestReviewSection({ enabled }: Props) {
  const { t } = useTranslation();
  const proposalsQuery = useSimbaProposalsQuery(enabled);
  const categoriesQuery = useSimbaCategoriesQuery(enabled);
  const proposals = proposalsQuery.data ?? [];
  // Positive confirmation for the irreversible upload: the confirmed row is
  // removed by the query invalidation, so surface success at the section level.
  const [flash, setFlash] = useState<string | null>(null);

  // Deep-link from the "send to Simba" toast/queue link: #simba-{id} scrolls to
  // and centers the just-queued row (no manual hunting across pages).
  useEffect(() => {
    const hash = window.location.hash;
    if (!hash.startsWith('#simba-') || proposals.length === 0) return;
    const el = document.getElementById(hash.slice(1));
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [proposals.length]);

  useEffect(() => {
    if (!flash) return;
    const id = setTimeout(() => setFlash(null), 6000);
    return () => clearTimeout(id);
  }, [flash]);

  const onResolved = (r: SimbaResolution) => {
    if (r.uploaded) setFlash(r.filename);
  };

  if (!enabled) return null;
  if (proposalsQuery.isLoading || (proposals.length === 0 && !flash)) return null;

  return (
    <section className="card p-4">
      <h2 className="text-sm font-semibold flex items-center gap-2 mb-3">
        <Landmark className="w-4 h-4" aria-hidden="true" />
        {t('simbaReview.title')}
        <span className="text-xs font-normal text-gray-500">({proposals.length})</span>
      </h2>

      {flash && (
        <div
          role="status"
          className="mb-3 flex items-center gap-2 rounded-lg border border-green-300 bg-green-50 p-2 text-xs text-green-800 dark:border-green-700/60 dark:bg-green-900/20 dark:text-green-200"
        >
          <CheckCircle2 className="w-4 h-4 shrink-0" aria-hidden="true" />
          {t('simbaSend.success', { filename: flash })}
        </div>
      )}

      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">{t('simbaReview.hint')}</p>
      <div className="space-y-2">
        {proposals.map((p) => (
          <div
            key={p.id}
            id={`simba-${p.id}`}
            className="border border-gray-200 dark:border-gray-700 rounded-lg p-3"
          >
            <p className="text-sm font-medium truncate mb-2" title={p.filename}>
              {p.filename}
            </p>
            <SimbaProposalForm
              proposal={p}
              categories={categoriesQuery.data ?? {}}
              onResolved={onResolved}
              showReject
            />
          </div>
        ))}
      </div>
    </section>
  );
}
