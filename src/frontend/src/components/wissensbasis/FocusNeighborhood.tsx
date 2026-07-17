/**
 * FocusNeighborhood (A4 panel) — focused entity + 1-hop + 2-hop neighbors.
 * Source: GET /api/wissensbasis/focus.
 *
 * Layout: focus card on top, hop1 chips beneath grouped by entity_type,
 * hop2 chips below in a faded section. Importance scores drive ordering
 * (already sorted server-side) and chip halo size.
 *
 * Overflow: when overflow_hop1/hop2 > 0, render "+N more" chip that
 * navigates to /wissensbasis?focus=<entity_id> for the deep-browse view.
 *
 * Empty state: friendly placeholder when no focus is set + CTA to click
 * a citation chip in the answer.
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { formatDate } from '../../utils/datetime';
import { Clock, Eye, MoreHorizontal } from 'lucide-react';

import { CitationChip } from './CitationChip';
import type {
  FocusEntity,
  FocusNeighborhood as FocusNeighborhoodData,
  ObservedField,
} from '../../api/resources/wissensbasis';

export interface FocusNeighborhoodProps {
  data: FocusNeighborhoodData | null;
  isLoading?: boolean;
  errorMessage?: string | null;
}

export function FocusNeighborhood({ data, isLoading, errorMessage }: FocusNeighborhoodProps) {
  const { t } = useTranslation();

  if (isLoading) {
    return <NeighborhoodSkeleton />;
  }

  if (errorMessage) {
    return (
      <div
        role="alert"
        className="text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 rounded px-3 py-2"
      >
        {errorMessage}
      </div>
    );
  }

  if (!data) {
    return (
      <div className="text-xs text-gray-500 dark:text-gray-400 italic px-3 py-4 text-center">
        <Eye className="h-4 w-4 inline-block mr-1" aria-hidden="true" />
        {t(
          'wissensbasis.focus.empty',
          'Click a citation chip in the answer to focus an entity here.',
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3 px-3 py-2">
      <FocusCard focus={data.focus} sourcePriority={data.source_priority ?? null} />
      <ObservedFieldsSection observed={data.observed_fields ?? []} />
      <NeighborSection
        title={t('wissensbasis.focus.directNeighbors', 'Direct neighbors')}
        entities={data.hop1}
        overflow={data.overflow_hop1}
        focusEntity={data.focus.entity_id}
      />
      {data.hop2.length > 0 && (
        <NeighborSection
          title={t('wissensbasis.focus.indirectNeighbors', 'Two hops away')}
          entities={data.hop2}
          overflow={data.overflow_hop2}
          focusEntity={data.focus.entity_id}
          faded
        />
      )}
    </div>
  );
}

function NeighborhoodSkeleton() {
  return (
    <div className="space-y-2 px-3 py-3">
      <div className="h-6 w-1/2 rounded bg-gray-200 dark:bg-gray-700 animate-pulse" />
      <div className="h-3 w-3/4 rounded bg-gray-200 dark:bg-gray-700 animate-pulse" />
      <div className="flex flex-wrap gap-1.5 mt-2">
        {[...Array(6)].map((_, i) => (
          <div
            key={i}
            className="h-5 w-16 rounded bg-gray-200 dark:bg-gray-700 animate-pulse"
          />
        ))}
      </div>
    </div>
  );
}

function FocusCard({
  focus,
  sourcePriority,
}: {
  focus: FocusEntity;
  sourcePriority: 1 | 2 | 3 | null;
}) {
  const { t } = useTranslation();
  const importancePct = Math.round(focus.importance * 100);
  const sourceLabelKey =
    sourcePriority === 1
      ? 'wissensbasis.focus.sourcePriority1'
      : sourcePriority === 2
        ? 'wissensbasis.focus.sourcePriority2'
        : sourcePriority === 3
          ? 'wissensbasis.focus.sourcePriority3'
          : null;
  return (
    <div className="rounded-md border border-accent-300 dark:border-accent-700 bg-accent-50 dark:bg-accent-900/20 px-3 py-2">
      <p className="font-semibold text-sm text-accent-900 dark:text-accent-100 break-words">
        {focus.display_name}
      </p>
      <p className="text-xs text-accent-700 dark:text-accent-400 mt-0.5">
        {focus.entity_type}
        {importancePct > 0 && (
          <>
            <span className="mx-1.5 opacity-50">·</span>
            <span title={t('wissensbasis.focus.importanceTooltip', 'Connectivity score')}>
              {t('wissensbasis.focus.importance', 'importance {{pct}}%', { pct: importancePct })}
            </span>
          </>
        )}
        {sourceLabelKey && (
          <>
            <span className="mx-1.5 opacity-50">·</span>
            <span className="italic">{t(sourceLabelKey)}</span>
          </>
        )}
      </p>
    </div>
  );
}

/**
 * Observed values section — renders the sprint-2 wb_field_provenance
 * snapshots returned in `FocusNeighborhood.observed_fields[]`. Each
 * entry is a (field_path, value, fetched_at) triple. Source priority
 * 1 = cached provenance, 2 = KG only, 3 = trace fallback. Observed
 * fields only appear when priority is 1 (P2/P3 don't contribute).
 *
 * Backend coarsening keeps values JSON-serializable; for display the
 * component stringifies them — leaves dicts/lists readable rather
 * than blank.
 */
function ObservedFieldsSection({ observed }: { observed: ObservedField[] }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  if (observed.length === 0) return null;

  // Show 5 most-recent by default; expand reveals the full list.
  const sorted = [...observed].sort(
    (a, b) => new Date(b.fetched_at).getTime() - new Date(a.fetched_at).getTime(),
  );
  const VISIBLE = 5;
  const visible = expanded ? sorted : sorted.slice(0, VISIBLE);
  const hidden = sorted.length - visible.length;

  return (
    <div>
      <p
        className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1.5 flex items-center gap-1"
        title={t(
          'wissensbasis.focus.observedFieldsTooltip',
          'Field values pinned at observation time — source for audit replay',
        )}
      >
        <Clock className="h-3 w-3" aria-hidden="true" />
        {t('wissensbasis.focus.observedFields', 'Observed values')}
      </p>
      <ul className="space-y-1">
        {visible.map((f, idx) => (
          <ObservedFieldRow key={`${f.source_type}:${f.field_path}:${idx}`} field={f} />
        ))}
      </ul>
      {hidden > 0 && !expanded && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="mt-1.5 text-xs text-accent-700 dark:text-accent-400 hover:underline"
        >
          {t('wissensbasis.focus.observedFieldsMore', '+{{count}} more values', { count: hidden })}
        </button>
      )}
    </div>
  );
}

function ObservedFieldRow({ field }: { field: ObservedField }) {
  const { t, i18n } = useTranslation();
  const when = formatTimestamp(field.fetched_at, i18n.language);
  const valueText = stringifyValue(field.value);
  return (
    <li className="text-xs">
      <span className="font-mono text-gray-500 dark:text-gray-400">{field.field_path}</span>
      <span className="mx-1.5 opacity-50">=</span>
      <span className="text-gray-900 dark:text-gray-100 break-all">{valueText}</span>
      <span className="ml-1.5 text-gray-400 dark:text-gray-500 italic">
        {t('wissensbasis.focus.observedAt', 'as of {{when}}', { when })}
      </span>
    </li>
  );
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return '∅';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatTimestamp(iso: string, locale: string): string {
  // Show as relative when recent (< 24h) — "5m ago", "3h ago" — and
  // an absolute short-date otherwise. Avoids "2026-05-12T06:24:11.689175"
  // dominating the row while staying precise enough for audit context.
  try {
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return iso;
    const deltaMin = (Date.now() - t) / 60000;
    if (deltaMin < 1) return locale.startsWith('de') ? 'gerade eben' : 'just now';
    if (deltaMin < 60) {
      const mins = Math.floor(deltaMin);
      return locale.startsWith('de') ? `vor ${mins} Min` : `${mins}m ago`;
    }
    if (deltaMin < 60 * 24) {
      const hrs = Math.floor(deltaMin / 60);
      return locale.startsWith('de') ? `vor ${hrs} Std` : `${hrs}h ago`;
    }
    return formatDate(iso, locale, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  } catch {
    return iso;
  }
}

interface NeighborSectionProps {
  title: string;
  entities: FocusEntity[];
  overflow: number;
  focusEntity: string;
  faded?: boolean;
}

function NeighborSection({ title, entities, overflow, focusEntity, faded }: NeighborSectionProps) {
  const { t } = useTranslation();
  if (entities.length === 0 && overflow === 0) return null;

  return (
    <div className={faded ? 'opacity-75' : ''}>
      <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1.5">
        {title}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {entities.map((e) => (
          <CitationChip
            key={e.entity_id}
            entity={e.entity_id}
            label={e.display_name}
            entityType={e.entity_type}
          />
        ))}
        {overflow > 0 && (
          <Link
            to={`/wissensbasis?focus=${encodeURIComponent(focusEntity)}`}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-xs font-medium
              bg-gray-100 dark:bg-gray-700/50 text-gray-600 dark:text-gray-300
              hover:bg-gray-200 dark:hover:bg-gray-700"
            title={t('wissensbasis.focus.overflowTooltip', 'Open the standalone view to browse all neighbors')}
          >
            <MoreHorizontal className="h-3 w-3" aria-hidden="true" />
            {t('wissensbasis.focus.overflow', '+{{count}} more', { count: overflow })}
          </Link>
        )}
      </div>
    </div>
  );
}
