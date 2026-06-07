import { useMemo, type ReactNode } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { BookOpen } from 'lucide-react';
import ObligationRow from '../../components/ObligationRow';
import TierBadge from '../../components/TierBadge';
import AreaCard from '../../components/wissen/AreaCard';
import { useObligationsQuery, useAtomsForReviewQuery } from '../../api/resources/brain';
import { useKnowledgeStatsQuery, useKnowledgeDocumentsQuery } from '../../api/resources/knowledge';
import { useKgStatsQuery, useKgEntitiesQuery } from '../../api/resources/knowledgeGraph';
import { useMemoriesQuery } from '../../api/resources/memories';
import { urgencyGroup, relativeDays } from '../../utils/frist';
import { useAuth } from '../../context/AuthContext';
import { LENSES, isLensVisible } from './lenses';

/** ISO yyyy-mm-dd `days` from today (matches ObligationsPage). */
function isoInDays(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

const OVERVIEW_PREVIEW_LIMIT = 3;

/** A uniform two-line preview row shared by every card (primary + muted detail,
 *  optional tier badge where the source carries a tier). */
function PreviewRow({
  primary,
  secondary,
  tier,
}: {
  primary: string;
  secondary?: string | null;
  tier?: number;
}) {
  return (
    <li className={tier != null ? `atom-row tier-ring-${tier}` : 'atom-row'}>
      <div className="flex-1 min-w-0 space-y-0.5">
        <p className="text-sm font-medium text-gray-900 dark:text-white truncate">{primary}</p>
        {secondary && (
          <p className="text-xs text-gray-500 dark:text-gray-400 truncate">{secondary}</p>
        )}
      </div>
      {tier != null && <TierBadge tier={tier} className="shrink-0 ml-2" />}
    </li>
  );
}

/** Card body: loading / empty / the preview list — uniform across areas. */
function CardBody({
  loading,
  empty,
  emptyText,
  children,
}: {
  loading: boolean;
  empty: boolean;
  emptyText: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  if (loading)
    return <p className="text-sm text-gray-500 dark:text-gray-400">{t('common.loading')}</p>;
  if (empty) return <p className="text-sm text-gray-500 dark:text-gray-400">{emptyText}</p>;
  return <ul className="space-y-2">{children}</ul>;
}

/**
 * Übersicht — the `/wissen` index. A dashboard of uniform area cards (DD3 +
 * user request): every area renders through `AreaCard` so each shows the SAME
 * level of detail — a count, a few preview rows, and one consistently-placed
 * "Öffnen →" link. Pure composition of existing hooks; no new endpoints.
 */
export default function OverviewLens() {
  const { t, i18n } = useTranslation();
  const auth = useAuth();
  const { isFeatureEnabled } = auth;
  const now = useMemo(() => new Date(), []);
  const dueBefore = useMemo(() => isoInDays(7), []);
  const rtf = useMemo(
    () => new Intl.RelativeTimeFormat(i18n.language, { numeric: 'auto' }),
    [i18n.language]
  );

  const schichtA = isFeatureEnabled('schicht_a_extraction_enabled');
  const lensByKey = (k: string) => LENSES.find((l) => l.key === k);
  const dokumenteLens = lensByKey('dokumente');
  const graphLens = lensByKey('graph');
  const docVisible = dokumenteLens ? isLensVisible(dokumenteLens, auth) : false;
  const graphVisible = graphLens ? isLensVisible(graphLens, auth) : false;

  // All hooks run unconditionally (rules of hooks). The knowledge + KG queries
  // carry an `enabled` gate so a hidden/ungranted area issues no request (the
  // docs query is gated on `docVisible` → no 403 noise for non-kb users);
  // obligations/review/memories are always fetched (cheap, ungated by design).
  const obligationsQuery = useObligationsQuery({ dueBefore, limit: 200 });
  const reviewQuery = useAtomsForReviewQuery(7);
  const kbStats = useKnowledgeStatsQuery(isFeatureEnabled('knowledge'));
  const kgStats = useKgStatsQuery(isFeatureEnabled('knowledge_graph'));
  const memoriesQuery = useMemoriesQuery(null);
  const docsQuery = useKnowledgeDocumentsQuery(
    { knowledgeBaseId: null, statusFilter: 'all' },
    docVisible,
  );
  // Graph preview: the stats endpoint doesn't return top_entities live, so pull
  // the first few entities directly (gated on the card being visible).
  const entitiesQuery = useKgEntitiesQuery(
    { page: 1, size: OVERVIEW_PREVIEW_LIMIT },
    graphVisible,
  );

  // Fristen: soonest-first already; keep overdue + this-week for the glance.
  const upcoming = (obligationsQuery.data ?? []).filter((f) =>
    f.obligation_date ? urgencyGroup(f.obligation_date, now) !== 'later' : false
  );
  const overdueCount = upcoming.filter(
    (f) => f.obligation_date && urgencyGroup(f.obligation_date, now) === 'overdue'
  ).length;
  const reviewAtoms = reviewQuery.data ?? [];
  const reviewCount = reviewAtoms.length;
  const docCount = kbStats.data?.document_count ?? 0;
  const entityCount = kgStats.data?.entity_count ?? 0;
  const relationCount = kgStats.data?.relation_count ?? 0;
  const memoryTotal = memoriesQuery.data?.total ?? 0;

  const recentDocs = [...(docsQuery.data ?? [])]
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    .slice(0, OVERVIEW_PREVIEW_LIMIT);
  const topEntities = (entitiesQuery.data?.entities ?? []).slice(0, OVERVIEW_PREVIEW_LIMIT);
  const recentMemories = [...(memoriesQuery.data?.memories ?? [])]
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    .slice(0, OVERVIEW_PREVIEW_LIMIT);

  const loading =
    obligationsQuery.isLoading ||
    reviewQuery.isLoading ||
    kbStats.isLoading ||
    kgStats.isLoading ||
    memoriesQuery.isLoading;

  // Cold-start empty-state: ONLY when every source — including memories — is
  // empty. (The pre-fix gate omitted memories, so a memory-only corpus wrongly
  // showed "leer".)
  const corpusEmpty =
    !loading &&
    upcoming.length === 0 &&
    reviewCount === 0 &&
    docCount === 0 &&
    entityCount === 0 &&
    relationCount === 0 &&
    memoryTotal === 0;

  if (corpusEmpty) {
    return (
      <div className="empty-state">
        <p className="text-2xl font-display text-gray-900 dark:text-white">
          {t('lens.overview.emptyTitle')}
        </p>
        <Link to="/wissen/dokumente" className="btn-primary inline-flex items-center gap-2 mt-2">
          <BookOpen className="w-4 h-4" aria-hidden="true" />
          {t('lens.overview.emptyCta')}
        </Link>
      </div>
    );
  }

  // Quiet corpus figures — zeros omitted so a sparse corpus reads cleanly.
  const figures = [
    docCount > 0 ? t('lens.overview.figDocs', { count: docCount }) : null,
    entityCount > 0 ? t('lens.overview.figEntities', { count: entityCount }) : null,
    relationCount > 0 ? t('lens.overview.figRelations', { count: relationCount }) : null,
    memoryTotal > 0 ? t('lens.overview.figMemories', { count: memoryTotal }) : null,
  ].filter(Boolean) as string[];

  const relLabel = (iso?: string): string | null => {
    if (!iso) return null;
    const days = relativeDays(iso, now);
    return Number.isFinite(days) ? rtf.format(days, 'day') : null;
  };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-display text-gray-900 dark:text-white">
          {t('lens.overview.title')}
        </h1>
        {figures.length > 0 && (
          <p className="text-sm text-gray-500 dark:text-gray-400 tabular-nums">
            {figures.join(' · ')}
          </p>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Nächste Fristen */}
        {schichtA && (
          <AreaCard
            title={t('lens.overview.fristen')}
            count={t('lens.overview.fristenSoon', { count: upcoming.length })}
            accent={
              overdueCount > 0 ? t('lens.overview.overdueAccent', { count: overdueCount }) : null
            }
            to="/wissen/fristen"
          >
            <CardBody
              loading={obligationsQuery.isLoading}
              empty={upcoming.length === 0}
              emptyText={t('lens.overview.noFristen')}
            >
              {upcoming.slice(0, OVERVIEW_PREVIEW_LIMIT).map((fact) => (
                <li
                  key={fact.id}
                  className={`atom-row tier-ring-${fact.circle_tier} flex-col sm:flex-row`}
                >
                  <ObligationRow fact={fact} now={now} />
                </li>
              ))}
            </CardBody>
          </AreaCard>
        )}

        {/* Zu prüfen */}
        <AreaCard
          title={t('lens.overview.review')}
          count={t('lens.overview.reviewShort', { count: reviewCount })}
          to="/wissen/review"
        >
          <CardBody
            loading={reviewQuery.isLoading}
            empty={reviewCount === 0}
            emptyText={t('lens.overview.reviewNone')}
          >
            {reviewAtoms.slice(0, OVERVIEW_PREVIEW_LIMIT).map((atom) => {
              const rel = relLabel(atom.created_at);
              const type = t(`circles.atomType.${atom.atom_type}`, {
                defaultValue: atom.atom_type,
              });
              return (
                <PreviewRow
                  key={atom.atom_id}
                  primary={atom.title || atom.preview || type}
                  secondary={rel ? `${type} · ${rel}` : type}
                  tier={atom.tier ?? 0}
                />
              );
            })}
          </CardBody>
        </AreaCard>

        {/* Dokumente */}
        {docVisible && (
          <AreaCard
            title={t('lens.dokumente')}
            count={t('lens.overview.figDocs', { count: docCount })}
            to="/wissen/dokumente"
          >
            <CardBody
              loading={docsQuery.isLoading}
              empty={recentDocs.length === 0}
              emptyText={t('lens.overview.docsNone')}
            >
              {recentDocs.map((doc) => (
                <PreviewRow
                  key={doc.id}
                  primary={doc.display_name || doc.title || doc.filename}
                  secondary={relLabel(doc.created_at)}
                />
              ))}
            </CardBody>
          </AreaCard>
        )}

        {/* Graph */}
        {graphVisible && (
          <AreaCard
            title={t('lens.graph')}
            count={`${t('lens.overview.figEntities', { count: entityCount })} · ${t('lens.overview.figRelations', { count: relationCount })}`}
            to="/wissen/graph"
          >
            <CardBody
              loading={entitiesQuery.isLoading}
              empty={topEntities.length === 0}
              emptyText={t('lens.overview.graphNone')}
            >
              {topEntities.map((entity) => (
                <PreviewRow
                  key={entity.id}
                  primary={entity.name}
                  secondary={t(`knowledgeGraph.${entity.entity_type}`, {
                    defaultValue: entity.entity_type,
                  })}
                />
              ))}
            </CardBody>
          </AreaCard>
        )}

        {/* Erinnerungen */}
        <AreaCard
          title={t('lens.erinnerungen')}
          count={t('lens.overview.figMemories', { count: memoryTotal })}
          to="/wissen/erinnerungen"
        >
          <CardBody
            loading={memoriesQuery.isLoading}
            empty={recentMemories.length === 0}
            emptyText={t('lens.overview.memoriesNone')}
          >
            {recentMemories.map((mem) => (
              <PreviewRow
                key={mem.id}
                primary={mem.content}
                secondary={t(`memory.categories.${mem.category}`, { defaultValue: mem.category })}
              />
            ))}
          </CardBody>
        </AreaCard>
      </div>
    </div>
  );
}
