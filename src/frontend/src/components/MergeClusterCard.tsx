import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, GitMerge, ShieldAlert } from 'lucide-react';
import TierBadge from './TierBadge';
import { formatDate } from '../utils/datetime';
import type { MergeProposal, MergeProposalEntityBrief } from '../api/resources/knowledgeGraph';

/**
 * One NAME CLUSTER of the review queue.
 *
 * The queue's unit of judgement is the cluster, not the pair: measured on the
 * live household 2026-09-24, 1 365 pending pairs sat over 952 entities in 199
 * name clusters — same name, empty description, which is exactly why the
 * reconciler refuses to auto-merge them. Deciding that pair by pair asks the
 * owner the same question a hundred times.
 *
 * So the card shows the whole cluster at once WITH the evidence the embedding
 * lacks (edges in the graph, mention count, the window it was seen in,
 * description when there is one) and takes one decision for all of it.
 *
 * Pairs that would change an atom's reach (cross-tier) never ride along: they
 * are counted in the footer and stay individually decidable below.
 */
export interface MergeCluster {
  /** Normalized name — the grouping key. */
  key: string;
  /** Display name (the most-mentioned spelling). */
  label: string;
  entities: MergeProposalEntityBrief[];
  /** Same-tier pairs: these take part in the bulk decision. */
  pairs: MergeProposal[];
  /** Cross-tier pairs: shown as a count, never swept. */
  crossTierPairs: MergeProposal[];
}

interface Props {
  cluster: MergeCluster;
  busy?: boolean;
  onMerge: (survivorId: number) => void;
  onReject: () => void;
}

export default function MergeClusterCard({ cluster, busy = false, onMerge, onReject }: Props) {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);

  // Default survivor = the most-established entity, the same rule the
  // reconciler uses to pick a winner (mentions, then the older first sighting).
  const sorted = useMemo(
    () => [...cluster.entities].sort((a, b) => (b.mention_count ?? 0) - (a.mention_count ?? 0)),
    [cluster.entities],
  );
  const [survivorId, setSurvivorId] = useState<number>(() => sorted[0]?.id ?? 0);
  // The queue refetches after every decision. If the cluster came back with a
  // different membership, a survivor chosen against the old one may no longer
  // exist — submitting it would 400 and (with the optimistic dismissal) make
  // the cluster vanish for nothing. Fall back to the new default.
  useEffect(() => {
    if (!cluster.entities.some((e) => e.id === survivorId)) {
      setSurvivorId(sorted[0]?.id ?? 0);
    }
  }, [cluster.entities, sorted, survivorId]);

  // Connected components over a SIMILARITY relation are not equivalence
  // classes: a chain A~B~C joins two things that were never compared. The
  // bigger the component, the weaker that transitive claim — so a large fold
  // asks once more, and says why.
  const LARGE_CLUSTER = 6;
  const [confirming, setConfirming] = useState(false);
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);

  const seen = (e: MergeProposalEntityBrief): string => {
    const opts: Intl.DateTimeFormatOptions = { year: '2-digit', month: '2-digit' };
    const from = e.first_seen_at ? formatDate(e.first_seen_at, i18n.language, opts) : '';
    const to = e.last_seen_at ? formatDate(e.last_seen_at, i18n.language, opts) : '';
    if (!from) return '';
    return from === to ? from : `${from} – ${to}`;
  };

  const headingId = `cluster-${cluster.key}-heading`;

  return (
    <li className="merge-proposal-card animate-fade-slide-in flex flex-col gap-3">
      <button
        type="button"
        className="flex items-center gap-2 text-left"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open
          ? <ChevronDown className="w-4 h-4 shrink-0" aria-hidden="true" />
          : <ChevronRight className="w-4 h-4 shrink-0" aria-hidden="true" />}
        <span id={headingId} className="font-medium text-gray-900 dark:text-white truncate">
          {cluster.label}
        </span>
        <span className="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
          {t('circles.mergeProposals.cluster.summary', {
            entities: cluster.entities.length,
            pairs: cluster.pairs.length + cluster.crossTierPairs.length,
          })}
        </span>
      </button>

      {open && (
        <ul
          role="radiogroup"
          aria-labelledby={headingId}
          className="flex flex-col gap-1"
        >
          {sorted.map((e) => (
            <li key={e.id}>
              <label className="flex items-start gap-2 cursor-pointer rounded px-2 py-1 hover:bg-gray-50 dark:hover:bg-gray-800">
                <input
                  type="radio"
                  name={`cluster-${cluster.key}-survivor`}
                  className="mt-1"
                  checked={survivorId === e.id}
                  onChange={() => setSurvivorId(e.id)}
                  disabled={busy}
                  aria-label={t('circles.mergeProposals.keepThis', { name: e.name })}
                />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm text-gray-900 dark:text-white truncate">{e.name}</span>
                  <span className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                    <TierBadge tier={e.circle_tier} />
                    {/* The KIND of thing, not decoration: a cluster that holds a
                        place and an organization is a wrong fold, and without the
                        type on the row there is nothing to see it by. */}
                    <span>{e.entity_type}</span>
                    <span className="tabular-nums">
                      {t('circles.mergeProposals.mentions', { count: e.mention_count })}
                    </span>
                    <span className="tabular-nums">
                      {t('circles.mergeProposals.cluster.edges', { count: e.relation_count ?? 0 })}
                    </span>
                    {seen(e) && <span className="tabular-nums">{seen(e)}</span>}
                  </span>
                  {e.description && (
                    <span className="mt-0.5 block text-xs text-gray-600 dark:text-gray-300 line-clamp-2">
                      {e.description}
                    </span>
                  )}
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-primary text-sm"
          disabled={busy || cluster.pairs.length === 0}
          onClick={() => {
            if (cluster.entities.length >= LARGE_CLUSTER && !confirming) {
              setConfirming(true);
              return;
            }
            setConfirming(false);
            onMerge(survivorId);
          }}
          ref={confirmRef}
        >
          <GitMerge className="w-4 h-4 mr-1 inline" aria-hidden="true" />
          {confirming
            ? t('circles.mergeProposals.cluster.confirmMerge', { count: cluster.entities.length })
            : t('circles.mergeProposals.cluster.mergeAll', { count: cluster.pairs.length })}
        </button>
        <button
          type="button"
          className="btn-secondary text-sm"
          disabled={busy || cluster.pairs.length === 0}
          onClick={onReject}
        >
          {t('circles.mergeProposals.cluster.rejectAll', { count: cluster.pairs.length })}
        </button>
      </div>

      {confirming && (
        <p className="flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400">
          <ShieldAlert className="w-4 h-4 shrink-0" aria-hidden="true" />
          <span>
            {t('circles.mergeProposals.cluster.chainWarning', {
              count: cluster.entities.length,
            })}
          </span>
        </p>
      )}

      {cluster.crossTierPairs.length > 0 && (
        <p className="flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400">
          <ShieldAlert className="w-4 h-4 shrink-0" aria-hidden="true" />
          <span>
            {t('circles.mergeProposals.cluster.crossTierLeft', {
              count: cluster.crossTierPairs.length,
            })}
          </span>
        </p>
      )}
    </li>
  );
}
