import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CopyCheck } from 'lucide-react';
import DocumentDuplicateCard from './DocumentDuplicateCard';
import {
  useApproveDocumentDuplicate,
  useDocumentDuplicatesQuery,
  useRejectDocumentDuplicate,
  type DocumentDuplicateProposal,
  type DuplicateResolution,
} from '../api/resources/documentDuplicates';

const UNDO_WINDOW_MS = 5000;

interface PendingApprove {
  id: number;
  survivorId: number;
  resolution: DuplicateResolution;
}

interface Props {
  enabled: boolean;
}

/**
 * KB near-duplicate document review section on /brain/review (#1170, Phase 2).
 * Mirrors MergeProposalsSection: owns the query + approve/reject mutations + the
 * 5s undo toast. Approve (survivor + supersede/delete) fires only when the window
 * closes; reject fires immediately. Gated on the document_dedupe_enabled feature
 * flag (dark by default) — returns null when off or when there's nothing pending.
 */
export default function DocumentDuplicatesSection({ enabled }: Props) {
  const { t } = useTranslation();
  const query = useDocumentDuplicatesQuery(enabled);
  const approve = useApproveDocumentDuplicate();
  const reject = useRejectDocumentDuplicate();

  const proposals: DocumentDuplicateProposal[] = query.data ?? [];
  const [dismissedIds, setDismissedIds] = useState<Set<number>>(() => new Set());
  const [pending, setPending] = useState<PendingApprove | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const commit = useCallback(
    (p: PendingApprove) => {
      approve.mutate({ id: p.id, survivorId: p.survivorId, resolution: p.resolution });
    },
    [approve],
  );

  const handleApprove = useCallback(
    (proposal: DocumentDuplicateProposal, survivorId: number, resolution: DuplicateResolution) => {
      if (pending) {
        clearTimer();
        commit(pending);
      }
      setDismissedIds((prev) => new Set(prev).add(proposal.id));
      const next: PendingApprove = { id: proposal.id, survivorId, resolution };
      setPending(next);
      timerRef.current = setTimeout(() => {
        commit(next);
        setPending(null);
        timerRef.current = null;
      }, UNDO_WINDOW_MS);
    },
    [pending, clearTimer, commit],
  );

  const handleUndo = useCallback(() => {
    clearTimer();
    if (pending) {
      setDismissedIds((prev) => {
        const n = new Set(prev);
        n.delete(pending.id);
        return n;
      });
    }
    setPending(null);
  }, [clearTimer, pending]);

  const handleReject = useCallback(
    (proposal: DocumentDuplicateProposal) => {
      setDismissedIds((prev) => new Set(prev).add(proposal.id));
      reject.mutate(proposal.id);
    },
    [reject],
  );

  useEffect(() => clearTimer, [clearTimer]);

  if (!enabled) {
    return null;
  }

  const visible = proposals.filter((p) => !dismissedIds.has(p.id));
  if (visible.length === 0 && !pending) {
    return null;
  }

  return (
    <section aria-labelledby="document-duplicates-heading" className="space-y-3">
      <h2
        id="document-duplicates-heading"
        className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300"
      >
        <CopyCheck className="h-4 w-4" aria-hidden="true" />
        {t('documentDuplicates.sectionTitle')}
      </h2>

      <ul className="space-y-3 animate-stagger">
        {visible.map((p) => (
          <DocumentDuplicateCard
            key={p.id}
            proposal={p}
            onApprove={(survivorId, resolution) => handleApprove(p, survivorId, resolution)}
            onReject={() => handleReject(p)}
          />
        ))}
      </ul>

      {pending && (
        <div className="toast left-1/2 bottom-6 -translate-x-1/2" role="status" aria-live="polite">
          <div className="flex items-center justify-between gap-4">
            <span className="text-sm text-gray-800 dark:text-gray-100">
              {t('documentDuplicates.resolved')}
            </span>
            <button type="button" className="btn btn-ghost text-sm" onClick={handleUndo}>
              {t('documentDuplicates.undo')}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
