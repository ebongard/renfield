import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { GitMerge } from 'lucide-react';
import TierBadge from './TierBadge';
import type { MergeProposal, MergeProposalEntityBrief } from '../api/resources/knowledgeGraph';

/**
 * One entity-merge proposal in the /brain/review queue (Structured Memory
 * T10, design decisions D2/D4/D6).
 *
 * Presentational — the page wires the approve/reject hooks + the 5s undo toast.
 * Reconciler-proposed pairs are ALWAYS the cautious case (cross-tier or
 * gray-zone; same-tier high-confidence dupes auto-merge and never reach here),
 * so the button emphasis never pushes hard toward the merge:
 *   - cross_tier  → both buttons secondary, Ablehnen focused first, + an
 *                   explicit visibility-change warning (color is never alone).
 *   - gray_zone   → same tier, just below the auto bar → Zusammenführen primary.
 *
 * D2 survivor toggle: a radio group picks WHICH entity survives (default = the
 * reconciler's winner = more mentions); onApprove gets the chosen winner id.
 */
interface MergeProposalCardProps {
  proposal: MergeProposal;
  onApprove: (winnerId: number) => void;
  onReject: () => void;
  busy?: boolean;
}

export default function MergeProposalCard({ proposal, onApprove, onReject, busy = false }: MergeProposalCardProps) {
  const { t } = useTranslation();
  const [winnerId, setWinnerId] = useState<number>(proposal.winner.id);

  const crossTier = proposal.loser.circle_tier !== proposal.winner.circle_tier;
  // No merge nudge when the stakes are high: a visibility change (cross_tier) or
  // a pair that is "maybe two people" by definition (name_typo — one in-token
  // edit apart). The visibility warning itself stays keyed on the tiers.
  const cautious = crossTier || proposal.reason === 'name_typo';
  const pct = Math.round((proposal.similarity ?? 0) * 100);
  const radioName = `merge-${proposal.id}-survivor`;

  // the entity being dropped, for the visibility-change warning
  const survivor = winnerId === proposal.loser.id ? proposal.loser : proposal.winner;
  const dropped = winnerId === proposal.loser.id ? proposal.winner : proposal.loser;

  const col = (ent: MergeProposalEntityBrief) => {
    const selected = winnerId === ent.id;
    return (
      <div
        className={`flex-1 rounded-md border p-3 transition-colors ${
          selected
            ? 'border-primary-400 bg-primary-50 dark:border-primary-500 dark:bg-gray-700/40'
            : 'border-gray-200 dark:border-gray-700'
        }`}
      >
        <label className="flex items-start gap-2 cursor-pointer">
          <input
            type="radio"
            name={radioName}
            className="mt-1"
            checked={selected}
            onChange={() => setWinnerId(ent.id)}
            disabled={busy}
            aria-label={t('circles.mergeProposals.keepThis', { name: ent.name })}
          />
          <span className="min-w-0 flex-1">
            <span className="block font-medium text-gray-900 dark:text-white truncate">{ent.name}</span>
            <span className="mt-1 flex flex-wrap items-center gap-2">
              <span className="text-xs text-gray-500 dark:text-gray-400">{ent.entity_type}</span>
              <TierBadge tier={ent.circle_tier} />
              <span className="text-xs text-gray-400 tabular-nums">
                {t('circles.mergeProposals.mentions', { count: ent.mention_count })}
              </span>
            </span>
            {ent.surface_forms.length > 0 && (
              <span className="mt-1 flex flex-wrap gap-1">
                {ent.surface_forms.map((sf) => (
                  <span key={sf} className="surface-form-pill">{sf}</span>
                ))}
              </span>
            )}
          </span>
        </label>
      </div>
    );
  };

  return (
    <li className="merge-proposal-card animate-fade-slide-in flex flex-col gap-3">
      <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
        <GitMerge className="w-4 h-4" aria-hidden="true" />
        <span>{t('circles.mergeProposals.whySuggested', { pct })}</span>
        <span aria-hidden="true">·</span>
        <span>{t(`circles.mergeProposals.reason.${proposal.reason}`, { defaultValue: proposal.reason })}</span>
      </div>

      <div
        role="radiogroup"
        aria-label={t('circles.mergeProposals.survivorLegend')}
        className="flex flex-col sm:flex-row items-stretch gap-2"
      >
        {col(proposal.loser)}
        <div
          className="flex items-center justify-center text-gray-400 text-xs sm:flex-col"
          aria-hidden="true"
        >
          {t('circles.mergeProposals.willMerge')}
        </div>
        {col(proposal.winner)}
      </div>

      {crossTier && (
        <p className="merge-visibility-warning" role="note">
          <span aria-hidden="true">⚠</span>
          <span>
            {t('circles.mergeProposals.visibilityWarning', {
              from: t(`circles.tier.${Math.max(survivor.circle_tier, dropped.circle_tier)}`),
              to: t(`circles.tier.${Math.min(survivor.circle_tier, dropped.circle_tier)}`),
            })}
          </span>
        </p>
      )}

      <div className={`flex gap-2 ${cautious ? 'flex-row' : 'flex-row-reverse'} justify-end`}>
        {/* cross_tier / name_typo: Ablehnen first in DOM (focus-first, no merge nudge);
            gray_zone: Zusammenführen primary, rendered last via flex-row-reverse. */}
        <button
          type="button"
          className="btn btn-secondary"
          onClick={onReject}
          disabled={busy}
          // eslint-disable-next-line jsx-a11y/no-autofocus
          autoFocus={cautious}
        >
          {t('circles.mergeProposals.reject')}
        </button>
        <button
          type="button"
          className={cautious ? 'btn btn-secondary' : 'btn btn-primary'}
          onClick={() => onApprove(winnerId)}
          disabled={busy}
        >
          {t('circles.mergeProposals.merge')}
        </button>
      </div>
    </li>
  );
}
