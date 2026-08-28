import { useEffect, useState } from 'react';
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
  const proposals = proposalsQuery.data ?? [];

  // Deep-link from the "send to Simba" toast: /wissen/review#simba-{id} scrolls
  // to and highlights the just-queued row (no manual hunting across pages).
  useEffect(() => {
    const hash = window.location.hash;
    if (!hash.startsWith('#simba-') || proposals.length === 0) return;
    const el = document.getElementById(hash.slice(1));
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [proposals.length]);

  if (!enabled) return null;
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
  const { t, i18n } = useTranslation();
  const confirm = useConfirmSimbaProposal();
  const reject = useRejectSimbaProposal();

  const now = new Date();
  const categoryNames = Object.keys(categories);
  const [category, setCategory] = useState(proposal.suggested_category ?? '');
  const [type, setType] = useState(proposal.suggested_type ?? '');
  const [description, setDescription] = useState(proposal.suggested_description ?? '');
  // Buchungszeitraum (Simba booking period) — default the current month/year, but
  // ALWAYS shown + editable: the portal otherwise silently stamps "now".
  const [month, setMonth] = useState(now.getMonth() + 1);
  const [year, setYear] = useState(now.getFullYear());
  const [error, setError] = useState<string | null>(null);
  // Set when Simba already has a matching transfer — the user must explicitly
  // force the (irreversible) upload past this warning.
  const [dupWarning, setDupWarning] = useState<string | null>(null);

  const monthName = (m: number) =>
    new Intl.DateTimeFormat(i18n.language, { month: 'long' }).format(new Date(2000, m - 1, 1));
  const yearOptions = [now.getFullYear(), now.getFullYear() - 1, now.getFullYear() - 2];
  const typeOptions = category ? categories[category] ?? [] : [];
  const busy = confirm.isPending || reject.isPending;

  const submit = (force: boolean) => {
    setError(null);
    setDupWarning(null);
    confirm.mutate(
      { id: proposal.id, category, type, description: description.trim(), month, year, force },
      {
        onSuccess: (data) => {
          if (data.already_in_simba) {
            setDupWarning(data.existing || t('simbaReview.dupUnknown'));
          }
        },
        onError: (e) => setError((e as Error).message || t('simbaReview.uploadFailed')),
      },
    );
  };

  const onConfirm = () => {
    setError(null);
    setDupWarning(null);
    if (!category || !type) {
      setError(t('simbaReview.needCategoryType'));
      return;
    }
    // eslint-disable-next-line no-alert
    if (!window.confirm(
      t('simbaReview.confirmUpload', {
        category, type, filename: proposal.filename,
        period: `${String(month).padStart(2, '0')}/${year}`,
      }),
    )) {
      return;
    }
    submit(false);
  };

  return (
    <div id={`simba-${proposal.id}`} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="text-sm font-medium truncate" title={proposal.filename}>
          {proposal.filename}
        </span>
        {busy && <Loader className="w-4 h-4 animate-spin flex-shrink-0" aria-hidden="true" />}
      </div>
      <label className="flex flex-col text-xs gap-1 mb-2">
        <span className="text-gray-500">{t('simbaReview.description')}</span>
        <input
          type="text"
          className="input py-1 text-xs"
          value={description}
          maxLength={100}
          placeholder={t('simbaReview.descriptionPlaceholder')}
          onChange={(e) => setDescription(e.target.value)}
          disabled={busy}
        />
      </label>
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
        <label className="flex flex-col text-xs gap-1">
          <span className="text-gray-500">{t('simbaReview.month')}</span>
          <select
            className="input py-1 text-xs"
            value={month}
            onChange={(e) => setMonth(Number(e.target.value))}
            disabled={busy}
          >
            {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
              <option key={m} value={m}>
                {monthName(m)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-xs gap-1">
          <span className="text-gray-500">{t('simbaReview.year')}</span>
          <select
            className="input py-1 text-xs"
            value={year}
            onChange={(e) => setYear(Number(e.target.value))}
            disabled={busy}
          >
            {yearOptions.map((y) => (
              <option key={y} value={y}>
                {y}
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
      {error && <p className="text-xs text-primary-700 dark:text-primary-300 mt-2">{error}</p>}
      {dupWarning && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-primary-300 bg-primary-50 p-2 text-xs text-primary-800 dark:border-primary-800 dark:bg-primary-900/20 dark:text-primary-200">
          <span className="grow">{t('simbaReview.dupWarning', { existing: dupWarning })}</span>
          <button
            onClick={() => submit(true)}
            disabled={busy}
            className="btn-secondary text-xs py-1 px-2"
          >
            {t('simbaReview.forceUpload')}
          </button>
        </div>
      )}
    </div>
  );
}
