import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader, Check, X, AlertTriangle } from 'lucide-react';

import {
  useConfirmSimbaProposal,
  useRejectSimbaProposal,
  type SimbaProposal,
} from '../../api/resources/simbaIngest';

/** Terminal outcome reported to the parent (overlay closes / queue flashes). */
export interface SimbaResolution {
  uploaded: boolean; // true = irreversible upload succeeded; false = rejected
  filename: string;
  message: string;
}

interface Props {
  proposal: SimbaProposal;
  categories: Record<string, string[]>;
  /** Called once on a terminal outcome (successful upload OR reject). The dup
   * gate and validation errors are NOT terminal — they stay in the form. */
  onResolved?: (r: SimbaResolution) => void;
  /** Show the Reject action (the queue does; the send-overlay uses Cancel). */
  showReject?: boolean;
  autoFocus?: boolean;
}

/**
 * Shared editable confirm form for one Simba proposal — the single surface that
 * captures Bezeichnung / Kategorie / Typ / Buchungszeitraum and performs the
 * IRREVERSIBLE upload. Used by both the doc-page send overlay and the
 * /brain/review queue, so the safety affordances live in one place:
 * - a styled, in-line "endgültig übertragen?" confirm step (never window.confirm),
 * - ≥44px touch targets on every action,
 * - an explicit already-in-Simba gate with a deliberate "force" re-confirm.
 * Positive success feedback is the parent's job (overlay success panel / queue
 * flash) via onResolved — this form just drives the flow.
 */
export default function SimbaProposalForm({
  proposal,
  categories,
  onResolved,
  showReject = true,
  autoFocus = false,
}: Props) {
  const { t, i18n } = useTranslation();
  const confirm = useConfirmSimbaProposal();
  const reject = useRejectSimbaProposal();

  const now = new Date();
  const categoryNames = Object.keys(categories);
  const [category, setCategory] = useState(proposal.suggested_category ?? '');
  const [type, setType] = useState(proposal.suggested_type ?? '');
  const [description, setDescription] = useState(proposal.suggested_description ?? '');
  // Default the booking period to the DOCUMENT's date (from Schicht-A facts), not
  // the current month (#1167) — falling back to now when no date was derivable.
  const [month, setMonth] = useState(proposal.suggested_month ?? now.getMonth() + 1);
  const [year, setYear] = useState(proposal.suggested_year ?? now.getFullYear());
  const [error, setError] = useState<string | null>(null);
  const [dupWarning, setDupWarning] = useState<string | null>(null);
  // Styled two-step confirm (replaces window.confirm): the transfer is
  // irreversible, so the primary action first asks for an explicit yes.
  const [confirming, setConfirming] = useState(false);

  const monthName = (m: number) =>
    new Intl.DateTimeFormat(i18n.language, { month: 'long' }).format(new Date(2000, m - 1, 1));
  const baseYears = [now.getFullYear(), now.getFullYear() - 1, now.getFullYear() - 2];
  // Ensure the document's year is selectable even if it's older than the last 3.
  const yearOptions =
    proposal.suggested_year && !baseYears.includes(proposal.suggested_year)
      ? [proposal.suggested_year, ...baseYears]
      : baseYears;
  const typeOptions = category ? categories[category] ?? [] : [];
  const busy = confirm.isPending || reject.isPending;

  const submit = (force: boolean) => {
    setError(null);
    setDupWarning(null);
    setConfirming(false);
    confirm.mutate(
      { id: proposal.id, category, type, description: description.trim(), month, year, force },
      {
        onSuccess: (data) => {
          if (data.already_in_simba) {
            setDupWarning(data.existing || t('simbaReview.dupUnknown'));
            return;
          }
          if (data.success) {
            onResolved?.({ uploaded: true, filename: proposal.filename, message: data.message });
            return;
          }
          setError(data.message || t('simbaReview.uploadFailed'));
        },
        onError: (e) => setError((e as Error).message || t('simbaReview.uploadFailed')),
      },
    );
  };

  const startConfirm = () => {
    setError(null);
    if (!category || !type) {
      setError(t('simbaReview.needCategoryType'));
      return;
    }
    setConfirming(true);
  };

  const onReject = () =>
    reject.mutate(proposal.id, {
      onSuccess: () => onResolved?.({ uploaded: false, filename: proposal.filename, message: '' }),
      onError: (e) => setError((e as Error).message || t('common.error')),
    });

  const period = `${String(month).padStart(2, '0')}/${year}`;

  return (
    <div>
      <label className="flex flex-col text-xs gap-1 mb-3">
        <span className="text-gray-500 dark:text-gray-400">{t('simbaReview.description')}</span>
        <input
          type="text"
          className="input"
          value={description}
          maxLength={100}
          placeholder={t('simbaReview.descriptionPlaceholder')}
          onChange={(e) => setDescription(e.target.value)}
          disabled={busy}
          autoFocus={autoFocus}
        />
      </label>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
        <label className="flex flex-col text-xs gap-1">
          <span className="text-gray-500 dark:text-gray-400">{t('simbaReview.category')}</span>
          <select
            className="input"
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
          <span className="text-gray-500 dark:text-gray-400">{t('simbaReview.type')}</span>
          <select
            className="input"
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
          <span className="text-gray-500 dark:text-gray-400">{t('simbaReview.month')}</span>
          <select
            className="input"
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
          <span className="text-gray-500 dark:text-gray-400">{t('simbaReview.year')}</span>
          <select
            className="input"
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
      </div>

      {/* Irreversible-action confirm: a styled two-step, not window.confirm. */}
      {confirming ? (
        <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-700/60 dark:bg-amber-900/20 dark:text-amber-100">
          <p className="flex items-start gap-2 mb-3">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
            <span>{t('simbaReview.confirmUploadInline', { filename: proposal.filename, category, type, period })}</span>
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => submit(false)}
              disabled={busy}
              className="btn-primary min-h-[44px] flex items-center gap-2"
            >
              {busy ? <Loader className="w-4 h-4 animate-spin" aria-hidden="true" /> : <Check className="w-4 h-4" aria-hidden="true" />}
              {t('simbaReview.confirmYes')}
            </button>
            <button
              onClick={() => setConfirming(false)}
              disabled={busy}
              className="btn-secondary min-h-[44px]"
            >
              {t('common.cancel')}
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={startConfirm}
            disabled={busy}
            className="btn-primary min-h-[44px] flex items-center gap-2"
          >
            <Check className="w-4 h-4" aria-hidden="true" />
            {t('simbaReview.confirm')}
          </button>
          {showReject && (
            <button
              onClick={onReject}
              disabled={busy}
              className="btn-secondary min-h-[44px] flex items-center gap-2"
            >
              <X className="w-4 h-4" aria-hidden="true" />
              {t('simbaReview.reject')}
            </button>
          )}
          {busy && <Loader className="w-4 h-4 animate-spin" aria-hidden="true" />}
        </div>
      )}

      {error && (
        <p className="mt-3 flex items-center gap-2 text-sm text-red-700 dark:text-red-300">
          <AlertTriangle className="w-4 h-4 shrink-0" aria-hidden="true" />
          {error}
        </p>
      )}
      {dupWarning && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-700/60 dark:bg-amber-900/20 dark:text-amber-100">
          <AlertTriangle className="w-4 h-4 shrink-0" aria-hidden="true" />
          <span className="grow">{t('simbaReview.dupWarning', { existing: dupWarning })}</span>
          <button
            onClick={() => submit(true)}
            disabled={busy}
            className="btn-secondary min-h-[44px]"
          >
            {t('simbaReview.forceUpload')}
          </button>
        </div>
      )}
    </div>
  );
}
