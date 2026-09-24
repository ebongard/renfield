import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { GitMerge } from 'lucide-react';
import { extractApiError } from '../utils/axios';
import MergeProposalCard from './MergeProposalCard';
import MergeClusterCard from './MergeClusterCard';
import { groupProposals } from './mergeClusters';
import {
  useApproveMergeProposal,
  useMergeProposalsQuery,
  useRejectMergeProposal,
  useResolveCluster,
  type MergeProposal,
} from '../api/resources/knowledgeGraph';

const UNDO_WINDOW_MS = 5000;

interface PendingMerge {
  id: number;
  winnerId: number;
}

/**
 * The "Zusammenführungs-Vorschläge" section at the top of /brain/review (D7).
 * Owns the query + approve/reject mutations + the 5s undo toast (D3):
 * clicking Zusammenführen optimistically removes the card and starts a 5s
 * window; the actual merge fires only when the window closes (Undo = no-op,
 * nothing is written). Reject fires immediately (non-destructive).
 *
 * Renders nothing when there are no pending proposals, so it never clutters the
 * tier-review list below it.
 */
export default function MergeProposalsSection() {
  const { t } = useTranslation();
  const query = useMergeProposalsQuery();
  const approve = useApproveMergeProposal();
  const reject = useRejectMergeProposal();
  const resolveCluster = useResolveCluster();

  const proposals: MergeProposal[] = query.data ?? [];
  const [dismissedIds, setDismissedIds] = useState<Set<number>>(() => new Set());
  const [pending, setPending] = useState<PendingMerge | null>(null);
  // What a cluster decision REFUSED to do. The service can resolve a cluster
  // partially — pairs that change visibility, cross a type boundary, or do not
  // reach the survivor stay pending on purpose. Throwing that away made a
  // partial refusal look exactly like a success: the cards vanished optimistically
  // and the pairs sat open in the database until the next page load.
  const [clusterNote, setClusterNote] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // commit a pending merge immediately (used on window expiry or when a second
  // merge is started before the first window closed).
  const commit = useCallback((p: PendingMerge) => {
    approve.mutate({ id: p.id, winnerId: p.winnerId });
  }, [approve]);

  const handleApprove = useCallback((proposal: MergeProposal, winnerId: number) => {
    // flush any in-flight pending so we never silently drop one
    if (pending) {
      clearTimer();
      commit(pending);
    }
    setDismissedIds((prev) => new Set(prev).add(proposal.id));
    const next: PendingMerge = { id: proposal.id, winnerId };
    setPending(next);
    timerRef.current = setTimeout(() => {
      commit(next);
      setPending(null);
      timerRef.current = null;
    }, UNDO_WINDOW_MS);
  }, [pending, clearTimer, commit]);

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

  const handleReject = useCallback((proposal: MergeProposal) => {
    setDismissedIds((prev) => new Set(prev).add(proposal.id));
    reject.mutate(proposal.id);
  }, [reject]);

  useEffect(() => clearTimer, [clearTimer]);

  const visible = proposals.filter((p) => !dismissedIds.has(p.id));
  if (visible.length === 0 && !pending) {
    return null;
  }

  // The queue is dominated by clusters of the same thing under the same name;
  // a cluster is one decision, a lone pair stays the familiar pair card.
  const { clusters, singles } = groupProposals(visible);

  /**
   * A refused fold, translated. EVERY refusal carries a code, not just the
   * undecidable-pair one — translating that single case would have left its six
   * siblings ("cluster spans more than one tier", "merge needs a survivor", …)
   * as raw English in a German UI. Null when the error is not a refusal at all.
   */
  const refusalMessage = (err: unknown): string | null => {
    const detail = (err as { response?: { data?: { detail?: unknown } } })
      ?.response?.data?.detail as
      { code?: string; pairs?: [string, string][]; total?: number;
        notes?: string[] } | undefined;
    const code = detail?.code;
    if (!code) return null;
    if (code !== 'cluster_has_undecidable_pair') {
      const key = `circles.mergeProposals.cluster.refused_${code}`;
      const text = t(key, { defaultValue: '' });
      if (text) return text;
      // Unbekannter Code. Das Backend LIEFERT den Grund mit (`notes`) — nur
      // eben auf Englisch, und damit unbrauchbar auf dem Bildschirm. Ihn
      // wegzuwerfen liess den Eigentuemer aber mit dem blossen Wort "Fehler"
      // zurueck, waehrend ein diagnostizierbarer Satz in der Antwort stand.
      // Also beides: ein wahrer allgemeiner Satz fuer die Anzeige, der genaue
      // Grund in die Konsole fuer den, der ihn auswerten muss. Das greift, wenn
      // das Backend dem Bundle vorauseilt (zwischengespeicherter Service
      // Worker nach einem Rollout) oder ein neuer Code ohne Schluessel ging.
      console.warn('[merge-cluster] untranslated refusal', code, detail?.notes);
      return t('circles.mergeProposals.cluster.refused_generic');
    }
    const shown = (detail?.pairs ?? []).map(([a, b]) => `${a} / ${b}`).join('; ');
    const total = detail?.total ?? (detail?.pairs?.length ?? 0);
    const hidden = total - (detail?.pairs?.length ?? 0);
    // Never truncate in silence: say how many are not listed.
    const pairs = hidden > 0
      ? t('circles.mergeProposals.cluster.undecidableMore', { pairs: shown, count: hidden })
      : shown;
    return t('circles.mergeProposals.cluster.undecidable', { count: total, pairs });
  };

  const handleCluster = (
    entityIds: number[], decision: 'merge' | 'reject', survivorId?: number,
  ): void => {
    // Optimistic: the whole cluster leaves the list at once. No undo window
    // here — a cluster fold touches many rows, and "undo" would have to unpick
    // merges the backend has already committed.
    const touched = visible
      .filter((p) => entityIds.includes(p.loser.id) && entityIds.includes(p.winner.id))
      .map((p) => p.id);
    setDismissedIds((prev) => new Set([...prev, ...touched]));
    setClusterNote(null);
    const restore = (): void => setDismissedIds((prev) => {
      const n = new Set(prev);
      for (const id of touched) n.delete(id);
      return n;
    });
    // On failure nothing was written — put the cluster back. Without this the
    // owner watches the queue shrink on an error and cannot get it back short
    // of reloading the page.
    void resolveCluster.mutateAsync({ entityIds, decision, survivorId })
      .then((res) => {
        const left = (res.skipped_cross_tier ?? 0)
          + (res.skipped_cross_type ?? 0)
          + (res.skipped_unreachable ?? 0)
          + (res.skipped_weak_edge ?? 0);
        if (left === 0 && (res.notes?.length ?? 0) === 0) return;
        // Partial refusal. The success path already invalidated the query, so a
        // refetch is on its way with the truth; undoing the optimistic dismissal
        // lets whatever stayed pending come back instead of disappearing until a
        // page reload. And say so — a refusal is never silent here.
        restore();
        // NOT `res.notes[0]`: that is a backend sentence, and a backend sentence
        // cannot be translated — the same leak the refusal codes just closed.
        // Every `notes` line in `resolve_cluster` today hangs off a refusal that
        // returns 400, so this branch is unreachable; it stays translated so the
        // next note appended on a SUCCESS path does not ship English into a
        // German UI.
        setClusterNote(
          left > 0
            ? t('circles.mergeProposals.cluster.partial', { count: left })
            : t('common.error'),
        );
      })
      .catch((err: unknown) => {
        // A refusal must not be silent either, and it must not be English in a
        // German UI. The service refuses a whole fold with a STRUCTURED detail
        // (`cluster_has_undecidable_pair` + the pairs + a total); the sentence
        // is built here so it can be translated. Anything else falls back to the
        // generic extractor.
        restore();
        setClusterNote(refusalMessage(err) ?? extractApiError(err, t('common.error')));
      });
  };

  return (
    <section aria-labelledby="merge-proposals-heading" className="space-y-3">
      <h2
        id="merge-proposals-heading"
        className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300"
      >
        <GitMerge className="w-4 h-4" aria-hidden="true" />
        {t('circles.mergeProposals.sectionTitle')}
      </h2>

      {clusterNote && (
        <p
          className="merge-visibility-warning"
          role="status"
          aria-live="polite"
        >
          <span aria-hidden="true">⚠</span>
          <span>{clusterNote}</span>
        </p>
      )}

      <ul className="space-y-3 animate-stagger">
        {clusters.map((c) => (
          <MergeClusterCard
            key={`cluster-${c.key}`}
            cluster={c}
            busy={resolveCluster.isPending}
            onMerge={(survivorId) => handleCluster(
              c.entities.map((e) => e.id), 'merge', survivorId,
            )}
            onReject={() => handleCluster(c.entities.map((e) => e.id), 'reject')}
          />
        ))}
        {singles.map((p) => (
          <MergeProposalCard
            key={p.id}
            proposal={p}
            onApprove={(winnerId) => handleApprove(p, winnerId)}
            onReject={() => handleReject(p)}
          />
        ))}
      </ul>

      {pending && (
        <div className="toast left-1/2 bottom-6 -translate-x-1/2" role="status" aria-live="polite">
          <div className="flex items-center justify-between gap-4">
            <span className="text-sm text-gray-800 dark:text-gray-100">
              {t('circles.mergeProposals.merged')}
            </span>
            <button type="button" className="btn btn-ghost text-sm" onClick={handleUndo}>
              {t('circles.mergeProposals.undo')}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
