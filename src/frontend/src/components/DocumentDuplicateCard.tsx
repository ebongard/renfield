import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileText } from 'lucide-react';
import type {
  DocumentDuplicateProposal,
  DuplicateDocBrief,
  DuplicateResolution,
} from '../api/resources/documentDuplicates';

interface Props {
  proposal: DocumentDuplicateProposal;
  onApprove: (survivorId: number, resolution: DuplicateResolution) => void;
  onReject: () => void;
  busy?: boolean;
}

/**
 * One near-duplicate document PAIR (#1170, Phase 2). The owner picks which
 * document survives (radio) and — per pair — whether the loser is SUPERSEDED
 * (recoverable, hidden from retrieval) or DELETED (removed via the standard
 * delete path). Presentational; the parent section wires the mutations + undo.
 */
export default function DocumentDuplicateCard({ proposal, onApprove, onReject, busy }: Props) {
  const { t, i18n } = useTranslation();
  const initialSurvivor = proposal.suggested_survivor_id ?? proposal.document_a.id;
  const [survivorId, setSurvivorId] = useState<number>(initialSurvivor);
  const [resolution, setResolution] = useState<DuplicateResolution>('supersede');

  const radioName = `dup-${proposal.id}-survivor`;
  const fmtDate = (iso: string | null) => {
    if (!iso) return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d.toLocaleDateString(i18n.language);
  };

  const docCol = (doc: DuplicateDocBrief) => {
    const selected = survivorId === doc.id;
    const date = fmtDate(doc.created_at);
    return (
      <label
        className={`flex-1 cursor-pointer rounded-md border p-3 transition-colors ${
          selected
            ? 'border-primary-400 bg-primary-50 dark:border-primary-500 dark:bg-primary-900/20'
            : 'border-gray-200 dark:border-gray-700'
        }`}
      >
        <div className="flex items-start gap-2">
          <input
            type="radio"
            name={radioName}
            checked={selected}
            onChange={() => setSurvivorId(doc.id)}
            disabled={busy}
            className="mt-1"
            aria-label={t('documentDuplicates.keepThis', { name: doc.name })}
          />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 font-medium text-gray-900 dark:text-gray-100">
              <FileText className="h-4 w-4 shrink-0 text-gray-400" aria-hidden="true" />
              <span className="truncate">{doc.name}</span>
            </div>
            {date && (
              <div className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                {t('documentDuplicates.imported', { date })}
              </div>
            )}
            <div className="mt-1 text-xs">
              {doc.paperless_document_id != null ? (
                <span className="inline-block rounded bg-green-100 px-1.5 py-0.5 text-green-800 dark:bg-green-900/40 dark:text-green-300">
                  {t('documentDuplicates.inPaperless', { id: doc.paperless_document_id })}
                </span>
              ) : (
                <span className="inline-block rounded bg-gray-100 px-1.5 py-0.5 text-gray-600 dark:bg-gray-800 dark:text-gray-400">
                  {t('documentDuplicates.notInPaperless')}
                </span>
              )}
            </div>
          </div>
        </div>
      </label>
    );
  };

  const willDelete = resolution === 'delete';

  return (
    <li className="card space-y-3 p-4">
      {proposal.shared_key && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {t('documentDuplicates.whyShared', { key: proposal.shared_key })}
        </p>
      )}

      <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
        {t('documentDuplicates.survivorLegend')}
      </p>
      <div
        role="radiogroup"
        aria-label={t('documentDuplicates.survivorLegend')}
        className="flex flex-col gap-2 sm:flex-row sm:items-stretch"
      >
        {docCol(proposal.document_a)}
        {docCol(proposal.document_b)}
      </div>

      {/* per-pair resolution choice */}
      <fieldset className="space-y-1.5">
        <legend className="text-xs font-medium text-gray-600 dark:text-gray-400">
          {t('documentDuplicates.resolutionLegend')}
        </legend>
        <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input
            type="radio"
            name={`dup-${proposal.id}-resolution`}
            checked={resolution === 'supersede'}
            onChange={() => setResolution('supersede')}
            disabled={busy}
          />
          <span>
            {t('documentDuplicates.supersede')}
            <span className="ml-1 text-xs text-gray-500">
              — {t('documentDuplicates.supersedeHint')}
            </span>
          </span>
        </label>
        <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input
            type="radio"
            name={`dup-${proposal.id}-resolution`}
            checked={willDelete}
            onChange={() => setResolution('delete')}
            disabled={busy}
          />
          <span>
            {t('documentDuplicates.delete')}
            <span className="ml-1 text-xs text-gray-500">
              — {t('documentDuplicates.deleteHint')}
            </span>
          </span>
        </label>
      </fieldset>

      {willDelete && (
        <p className="text-xs text-amber-700 dark:text-amber-400" role="note">
          ⚠ {t('documentDuplicates.deleteWarning')}
        </p>
      )}

      <div className="flex flex-row-reverse justify-end gap-2">
        <button
          type="button"
          className={willDelete ? 'btn btn-secondary' : 'btn btn-primary'}
          onClick={() => onApprove(survivorId, resolution)}
          disabled={busy}
        >
          {t('documentDuplicates.approve')}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={onReject}
          disabled={busy}
        >
          {t('documentDuplicates.reject')}
        </button>
      </div>
    </li>
  );
}
