import { useTranslation } from 'react-i18next';
import { Check, Edit2, Pin, PinOff, Trash2, X } from 'lucide-react';

import type { Skill } from '../api/resources/skills';
import StatusBadge from './StatusBadge';
import TierBadge from './TierBadge';
import type { CircleTier } from './TierBadge';

interface SkillCardProps {
  skill: Skill;
  showAdminActions?: boolean;
  showOwnerActions?: boolean;
  onApprove?: (id: number) => void;
  onReject?: (id: number) => void;
  onEdit?: (skill: Skill) => void;
  onDelete?: (id: number) => void;
  onTogglePin?: (skill: Skill) => void;
  isBusy?: boolean;
}

/**
 * Skill list card used by both the admin Skills Inbox (showAdminActions)
 * and the owner's BrainSkillsPage (showOwnerActions). The card is the
 * primary visual unit of the self-learning admin surface — it has to
 * render in tight admin lists AND inside the owner's brain page without
 * a layout fork.
 */
export default function SkillCard({
  skill,
  showAdminActions = false,
  showOwnerActions = false,
  onApprove,
  onReject,
  onEdit,
  onDelete,
  onTogglePin,
  isBusy = false,
}: SkillCardProps) {
  const { t } = useTranslation();
  const total = skill.success_count + skill.failure_count;
  const successRate = total > 0
    ? Math.round((skill.success_count / total) * 100)
    : null;

  return (
    <article
      className="card p-4 flex flex-col gap-3"
      data-testid={`skill-card-${skill.id}`}
      data-status={skill.status}
    >
      <header className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <h3 className="text-base font-semibold text-gray-900 dark:text-white truncate">
            {skill.title}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <StatusBadge status={skill.status} />
            <TierBadge tier={skill.circle_tier as CircleTier} />
            <span>{t(`selfLearning.skills.source.${skill.source}`)}</span>
            <span>v{skill.version}</span>
            {skill.pinned && (
              <span className="inline-flex items-center gap-1 text-accent-700 dark:text-accent-300">
                <Pin className="w-3 h-3" aria-hidden="true" />
                {t('selfLearning.skills.pinned')}
              </span>
            )}
          </div>
        </div>
        {successRate != null && (
          <div className="text-right shrink-0">
            <div className="text-lg font-semibold text-gray-900 dark:text-white">
              {successRate}%
            </div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {t('selfLearning.skills.successRate', { count: total })}
            </div>
          </div>
        )}
      </header>

      {skill.trigger_examples.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" data-testid="trigger-list">
          {skill.trigger_examples.slice(0, 4).map((trigger) => (
            <li
              key={trigger}
              className="px-2 py-0.5 rounded-md bg-gray-100 dark:bg-gray-800 text-xs text-gray-700 dark:text-gray-300"
            >
              "{trigger}"
            </li>
          ))}
          {skill.trigger_examples.length > 4 && (
            <li className="text-xs text-gray-500 dark:text-gray-400 self-center">
              +{skill.trigger_examples.length - 4}
            </li>
          )}
        </ul>
      )}

      {skill.body_md && (
        <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-line line-clamp-3">
          {skill.body_md}
        </p>
      )}

      {skill.tool_sequence.length > 0 && (
        <div className="text-xs text-gray-500 dark:text-gray-400 truncate" data-testid="tool-sequence">
          {t('selfLearning.skills.toolsLabel')}: {skill.tool_sequence.join(' → ')}
        </div>
      )}

      {(showAdminActions || showOwnerActions) && (
        <footer className="flex flex-wrap gap-2 pt-2 border-t border-gray-200 dark:border-gray-700">
          {showAdminActions && skill.status === 'draft' && (
            <>
              <button
                type="button"
                className="btn-primary inline-flex items-center gap-1 px-3 py-1.5 text-sm"
                onClick={() => onApprove?.(skill.id)}
                disabled={isBusy}
                data-testid="approve-button"
              >
                <Check className="w-4 h-4" aria-hidden="true" />
                {t('selfLearning.skills.actions.approve')}
              </button>
              <button
                type="button"
                className="btn-secondary inline-flex items-center gap-1 px-3 py-1.5 text-sm"
                onClick={() => onReject?.(skill.id)}
                disabled={isBusy}
                data-testid="reject-button"
              >
                <X className="w-4 h-4" aria-hidden="true" />
                {t('selfLearning.skills.actions.reject')}
              </button>
            </>
          )}
          {onEdit && (
            <button
              type="button"
              className="btn-secondary inline-flex items-center gap-1 px-3 py-1.5 text-sm"
              onClick={() => onEdit(skill)}
              disabled={isBusy}
              data-testid="edit-button"
            >
              <Edit2 className="w-4 h-4" aria-hidden="true" />
              {t('selfLearning.skills.actions.edit')}
            </button>
          )}
          {showOwnerActions && onTogglePin && (
            <button
              type="button"
              className="btn-secondary inline-flex items-center gap-1 px-3 py-1.5 text-sm"
              onClick={() => onTogglePin(skill)}
              disabled={isBusy}
              data-testid="pin-button"
            >
              {skill.pinned
                ? <PinOff className="w-4 h-4" aria-hidden="true" />
                : <Pin className="w-4 h-4" aria-hidden="true" />}
              {t(skill.pinned ? 'selfLearning.skills.actions.unpin' : 'selfLearning.skills.actions.pin')}
            </button>
          )}
          {showOwnerActions && onDelete && (
            <button
              type="button"
              className="btn-secondary inline-flex items-center gap-1 px-3 py-1.5 text-sm text-rose-600 dark:text-rose-300"
              onClick={() => onDelete(skill.id)}
              disabled={isBusy}
              data-testid="delete-button"
            >
              <Trash2 className="w-4 h-4" aria-hidden="true" />
              {t('selfLearning.skills.actions.delete')}
            </button>
          )}
        </footer>
      )}
    </article>
  );
}
