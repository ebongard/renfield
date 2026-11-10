/**
 * CitationChip — inline pill rendered for `<cite entity="UUID">Display</cite>`
 * tags emitted by the agent. Click refocuses the WissensbasisSidePanel /
 * WissensbasisPage on the cited entity via WissensbasisContext.
 *
 * Outside a WissensbasisProvider (e.g. on the legacy Brain page), the
 * context shim makes the chip non-interactive but still styled, so chat
 * answers don't visually break in places where the panel isn't mounted.
 */

import { useTranslation } from 'react-i18next';
import {
  Box,
  FileText,
  GitBranch,
  Lightbulb,
  type LucideIcon,
  Ticket,
  User,
  AlertTriangle,
} from 'lucide-react';

import { useWissensbasis } from '../../context/WissensbasisContext';

export type CitationChipEntityType =
  | 'release'
  | 'ticket'
  | 'person'
  | 'document'
  | 'incident'
  | 'concept'
  | string;

export interface CitationChipProps {
  entity: string;
  label: string;
  entityType?: CitationChipEntityType;
  /** Backend marks chips with invalid / unresolvable entity ids. */
  missing?: boolean;
}

const TYPE_ICON: Record<string, LucideIcon> = {
  release: GitBranch,
  ticket: Ticket,
  person: User,
  document: FileText,
  incident: AlertTriangle,
  concept: Lightbulb,
};

function chipIcon(type?: string): LucideIcon {
  // Own keys only: `type` comes from model markup, and "constructor" or
  // "__proto__" would otherwise resolve to Object.prototype members.
  return type && Object.prototype.hasOwnProperty.call(TYPE_ICON, type) ? TYPE_ICON[type] : Box;
}

export function CitationChip({ entity, label, entityType, missing }: CitationChipProps) {
  const { t } = useTranslation();
  const { setFocus, focusEntityId } = useWissensbasis();
  const Icon = chipIcon(entityType);
  const isFocused = !missing && entity === focusEntityId;
  const interactive = !missing && !!entity;

  // DESIGN.md only allows crimson + turquoise + neutral grays. Citation
  // chips are an informational element → accent (turquoise) tier. Existing
  // Badge.tsx uses the same accent-100/accent-700/accent-600/20/accent-400
  // pairing — match it for visual consistency.
  const baseClasses =
    'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-xs font-medium align-baseline transition-colors';
  const tone = missing
    ? 'bg-gray-100 dark:bg-gray-700/50 text-gray-400 dark:text-gray-500 line-through cursor-not-allowed'
    : isFocused
      ? 'bg-accent-200 dark:bg-accent-600/40 text-accent-900 dark:text-accent-100 ring-1 ring-accent-500 dark:ring-accent-400'
      : 'bg-accent-100 dark:bg-accent-600/20 text-accent-700 dark:text-accent-400 hover:bg-accent-200 dark:hover:bg-accent-600/30';
  const focusRing = interactive ? 'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500' : '';

  if (!interactive) {
    return (
      <span
        className={`${baseClasses} ${tone}`}
        title={
          missing
            ? t('wissensbasis.chip.missingTooltip', 'Could not resolve entity')
            : label
        }
      >
        <Icon className="h-3 w-3" aria-hidden="true" />
        <span>{label}</span>
      </span>
    );
  }

  return (
    <button
      type="button"
      className={`${baseClasses} ${tone} ${focusRing} cursor-pointer`}
      onClick={(e) => {
        // Stop propagation so the chip click doesn't also trigger
        // ColumnSet selectActions or other parent click handlers in
        // the Adaptive Card tree.
        e.stopPropagation();
        setFocus(entity);
      }}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          setFocus(null);
        }
      }}
      aria-label={t('wissensbasis.chip.ariaLabel', 'Focus on {{label}}', { label })}
      aria-pressed={isFocused}
      title={label}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      <span>{label}</span>
    </button>
  );
}
