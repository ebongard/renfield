import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Landmark, Loader, Check, X } from 'lucide-react';

import {
  useSimbaProposalsQuery,
  useSimbaCategoriesQuery,
  useConfirmSimbaProposal,
  useRejectSimbaProposal,
  type SimbaProposal,
} from '../api/resources/simbaIngest';

interface Props {
  /** From the `simba_ingest_review_enabled` feature flag. */
  enabled: boolean;
}

/**
 * Review queue for watch-folder PDFs proposed for the Simba tax portal (xidra).
 * The upload is irreversible, so each file is confirmed here (with an
 * editable, content-prefilled category/type) before it is sent — never
 * automatically. Renders nothing when disabled or when the queue is empty.
 */
export default function SimbaIngestReviewSection({ enabled }: Props) {
  const { t } = useTranslation();
  const proposalsQuery = useSimbaProposalsQuery(enabled);
  const categoriesQuery = useSimbaCategoriesQuery(enabled);

  if (!enabled) return null;
  const proposals = proposalsQuery.data ?? [];
  if (proposalsQuery.isLoading || proposals.length === 0) return null;

  return (
    <section className="card p-4">
      <h2 className="text-sm font-semibold flex items-center gap-2 mb-3">
        <Landmark className="w-4 h-4" aria-hidden="true" />
        {t('simbaReview.title')}
        <span className="text-xs font-normal text-gray-500">({proposals.length})</span>
      </h2>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">{t('simbaReview.hint')}</p>
      <div className="space-y-2">
        {proposals.map((p) => (
          <ProposalRow key={p.id} proposal={p} categories={categoriesQuery.data ?? {}} />
        ))}
      </div>
    </section>
  );
}

function ProposalRow({
  proposal,
  categories,
}: {
  proposal: SimbaProposal;
  categories: Record<string, string[]>;
}) {
  const { t } = useTranslation();
  const confirm = useConfirmSimbaProposal();
  const reject = useRejectSimbaProposal();

  const categoryNames = Object.keys(categories);
  const [category, setCategory] = useState(proposal.suggested_category ?? '');
  const [type, setType] = useState(proposal.suggested_type ?? '');
  const [error, setError] = useState<string | null>(null);

  const typeOptions = category ? categories[category] ?? [] : [];
  const busy = confirm.isPending || reject.isPending;

  const onConfirm = () => {
    setError(null);
    if (!category || !type) {
      setError(t('simbaReview.needCategoryType'));
      return;
    }
    // eslint-disable-next-line no-alert
    if (!window.confirm(t('simbaReview.confirmUpload', { category, type, filename: proposal.filename }))) {
      return;
    }
    confirm.mutate(
      { id: proposal.id, category, type },
      { onError: (e) => setError((e as Error).message || t('simbaReview.uploadFailed')) },
    );
  };

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="text-sm font-medium truncate" title={proposal.filename}>
          {proposal.filename}
        </span>
        {busy && <Loader className="w-4 h-4 animate-spin flex-shrink-0" aria-hidden="true" />}
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-xs gap-1">
          <span className="text-gray-500">{t('simbaReview.category')}</span>
          <select
            className="input py-1 text-xs"
            value={category}
            onChange={(e) => {
              setCategory(e.target.value);
              setType('');
            }}
            disabled={busy}
          >
            <option value="">—</option>
            {categoryNames.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-xs gap-1">
          <span className="text-gray-500">{t('simbaReview.type')}</span>
          <select
            className="input py-1 text-xs"
            value={type}
            onChange={(e) => setType(e.target.value)}
            disabled={busy || !category}
          >
            <option value="">—</option>
            {typeOptions.map((tp) => (
              <option key={tp} value={tp}>
                {tp}
              </option>
            ))}
          </select>
        </label>
        <button
          onClick={onConfirm}
          disabled={busy}
          className="btn-primary text-xs py-1 px-2 flex items-center gap-1"
        >
          <Check className="w-3 h-3" aria-hidden="true" />
          {t('simbaReview.confirm')}
        </button>
        <button
          onClick={() => reject.mutate(proposal.id)}
          disabled={busy}
          className="btn-secondary text-xs py-1 px-2 flex items-center gap-1"
        >
          <X className="w-3 h-3" aria-hidden="true" />
          {t('simbaReview.reject')}
        </button>
      </div>
      {error && <p className="text-xs text-red-600 dark:text-red-400 mt-2">{error}</p>}
    </div>
  );
}
