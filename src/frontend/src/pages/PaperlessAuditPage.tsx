/**
 * Paperless Document Audit Page
 *
 * Admin page for auditing Paperless documents using LLM analysis.
 * Provides audit control, review queue, OCR issue tracking, and statistics.
 */
import { useState, useEffect, useRef } from 'react';
import type { TFunction } from 'i18next';
import type { AxiosError } from 'axios';
import type { LucideIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  FileSearch, Play, Square, Loader, Check, X,
  RotateCcw, BarChart3, ClipboardList, Eye, ChevronLeft, ChevronRight,
  Copy, Users, FileText,
} from 'lucide-react';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import {
  useAuditStatusQuery,
  useReviewResultsQuery,
  useOcrResultsQuery,
  useCompletenessResultsQuery,
  useAuditStatsQuery,
  useDuplicateGroupsQuery,
  useCorrespondentClustersQuery,
  useStartAudit,
  useStopAudit,
  useApplyResults,
  useSkipResults,
  useReOcr,
  useDetectDuplicates,
  type AuditMode,
  type FixMode,
  type AuditStatus,
  type AuditResult,
  type AuditStats,
  type DuplicateGroup,
  type CorrespondentCluster,
} from '../api/resources/paperlessAudit';

type AuditTab = 'control' | 'review' | 'ocr' | 'completeness' | 'duplicates' | 'correspondents' | 'stats';

const TABS: AuditTab[] = ['control', 'review', 'ocr', 'completeness', 'duplicates', 'correspondents', 'stats'];
const PAGE_SIZE = 20;

const OCR_COLORS: Partial<Record<number, string>> = {
  1: 'bg-red-500',
  2: 'bg-orange-500',
  3: 'bg-yellow-500',
  4: 'bg-green-500',
  5: 'bg-green-700',
};

const OCR_TEXT_COLORS: Partial<Record<number, string>> = {
  1: 'text-red-600 dark:text-red-400',
  2: 'text-orange-600 dark:text-orange-400',
  3: 'text-yellow-600 dark:text-yellow-400',
  4: 'text-green-600 dark:text-green-400',
  5: 'text-green-700 dark:text-green-300',
};

export default function PaperlessAuditPage() {
  const { t } = useTranslation();

  const [activeTab, setActiveTab] = useState<AuditTab>('control');
  const [error, setError] = useState<string | null>(null);

  // Control tab state
  const [mode, setMode] = useState<AuditMode>('new_only');
  const [fixMode, setFixMode] = useState<FixMode>('review');
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.8);

  // Review tab state
  const [reviewPage, setReviewPage] = useState(0);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [actionLoading, setActionLoading] = useState<Set<number>>(new Set());
  const [reviewSortBy, setReviewSortBy] = useState<string | null>(null);
  const [reviewSortOrder, setReviewSortOrder] = useState<'asc' | 'desc'>('desc');
  const [reviewSearch, setReviewSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const reviewSearchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // OCR tab state
  const [ocrPage, setOcrPage] = useState(0);
  const [ocrActionLoading, setOcrActionLoading] = useState<Set<number>>(new Set());

  // Completeness tab state
  const [completenessPage, setCompletenessPage] = useState(0);

  // Correspondents tab state
  const [corrThreshold, setCorrThreshold] = useState(0.82);
  const [scanCorrespondents, setScanCorrespondents] = useState(false);

  // Status query (always on; polls every 2s while running via the query's
  // own refetchInterval callback that reads from the response data).
  const statusQuery = useAuditStatusQuery();
  const auditStatus = statusQuery.data ?? null;

  // Tab-gated data queries
  const reviewQuery = useReviewResultsQuery(
    {
      page: reviewPage,
      perPage: PAGE_SIZE,
      sortBy: reviewSortBy,
      sortOrder: reviewSortOrder,
      search: debouncedSearch,
    },
    activeTab === 'review',
  );
  const reviewResults: AuditResult[] = reviewQuery.data?.results ?? [];
  const reviewTotal = reviewQuery.data?.total ?? 0;
  const reviewLoading = reviewQuery.isLoading;

  const ocrQuery = useOcrResultsQuery(
    { page: ocrPage, perPage: PAGE_SIZE },
    activeTab === 'ocr',
  );
  const ocrResults: AuditResult[] = ocrQuery.data?.results ?? [];
  const ocrTotal = ocrQuery.data?.total ?? 0;
  const ocrLoading = ocrQuery.isLoading;

  const completenessQuery = useCompletenessResultsQuery(
    { page: completenessPage, perPage: PAGE_SIZE },
    activeTab === 'completeness',
  );
  const completenessResults: AuditResult[] = completenessQuery.data?.results ?? [];
  const completenessTotal = completenessQuery.data?.total ?? 0;
  const completenessLoading = completenessQuery.isLoading;

  const statsQuery = useAuditStatsQuery(activeTab === 'stats');
  const stats: AuditStats | null = statsQuery.data ?? null;
  const statsLoading = statsQuery.isLoading;

  const duplicatesQuery = useDuplicateGroupsQuery(activeTab === 'duplicates');
  const duplicateGroups: DuplicateGroup[] = duplicatesQuery.data ?? [];
  const duplicatesLoading = duplicatesQuery.isLoading;

  const correspondentsQuery = useCorrespondentClustersQuery(
    corrThreshold,
    activeTab === 'correspondents' && scanCorrespondents,
  );
  const correspondentClusters: CorrespondentCluster[] = correspondentsQuery.data ?? [];
  const correspondentsLoading = correspondentsQuery.isFetching;

  // Mutations
  const startMutation = useStartAudit();
  const stopMutation = useStopAudit();
  const applyMutation = useApplyResults();
  const skipMutation = useSkipResults();
  const reOcrMutation = useReOcr();
  const detectDuplicatesMutation = useDetectDuplicates();

  const starting = startMutation.isPending;
  const stopping = stopMutation.isPending;
  const detectingDuplicates = detectDuplicatesMutation.isPending;

  // 503 → "not configured" surface. Any query returning 503 means the
  // Paperless integration isn't wired up; the page collapses to a banner.
  const queryErrors = [
    statusQuery.error,
    reviewQuery.error,
    ocrQuery.error,
    completenessQuery.error,
    statsQuery.error,
    duplicatesQuery.error,
    correspondentsQuery.error,
  ];
  const notConfigured = queryErrors.some(
    (e) => (e as AxiosError | null)?.response?.status === 503,
  );

  // Reset selectedIds whenever a fresh review fetch lands. Tracking
  // reviewQuery.data (the actual query result) avoids the infinite-loop
  // trap that comes from depending on reviewResults, which gets a new
  // array reference every render thanks to the `?? []` fallback.
  useEffect(() => {
    if (reviewQuery.data !== undefined) {
      setSelectedIds(new Set());
    }
  }, [reviewQuery.data]);

  const startAudit = async () => {
    setError(null);
    try {
      await startMutation.mutateAsync({
        mode,
        fix_mode: fixMode,
        confidence_threshold: confidenceThreshold,
      });
    } catch (err) {
      const status = (err as AxiosError | undefined)?.response?.status;
      if (status !== 503) setError(t('paperlessAudit.error'));
    }
  };

  const stopAudit = async () => {
    try {
      await stopMutation.mutateAsync(undefined);
    } catch (err) {
      const status = (err as AxiosError | undefined)?.response?.status;
      if (status !== 503) setError(t('paperlessAudit.error'));
    }
  };

  const approveResults = async (ids: number[]) => {
    setActionLoading((prev) => new Set([...prev, ...ids]));
    try {
      await applyMutation.mutateAsync(ids);
    } catch (err) {
      const status = (err as AxiosError | undefined)?.response?.status;
      if (status !== 503) setError(t('paperlessAudit.error'));
    } finally {
      setActionLoading((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    }
  };

  const skipResults = async (ids: number[]) => {
    setActionLoading((prev) => new Set([...prev, ...ids]));
    try {
      await skipMutation.mutateAsync(ids);
    } catch (err) {
      const status = (err as AxiosError | undefined)?.response?.status;
      if (status !== 503) setError(t('paperlessAudit.error'));
    } finally {
      setActionLoading((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    }
  };

  const toggleSelected = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === reviewResults.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(reviewResults.map((r) => r.id)));
    }
  };

  const handleReviewSort = (column: string) => {
    if (reviewSortBy === column) {
      setReviewSortOrder((prev) => prev === 'asc' ? 'desc' : 'asc');
    } else {
      setReviewSortBy(column);
      setReviewSortOrder('asc');
    }
    setReviewPage(0);
  };

  const handleReviewSearch = (value: string) => {
    setReviewSearch(value);
    if (reviewSearchTimer.current) clearTimeout(reviewSearchTimer.current);
    reviewSearchTimer.current = setTimeout(() => {
      setDebouncedSearch(value);
      setReviewPage(0);
    }, 300);
  };

  const reOcr = async (ids: number[]) => {
    setOcrActionLoading((prev) => new Set([...prev, ...ids]));
    try {
      await reOcrMutation.mutateAsync(ids);
    } catch (err) {
      const status = (err as AxiosError | undefined)?.response?.status;
      if (status !== 503) setError(t('paperlessAudit.error'));
    } finally {
      setOcrActionLoading((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    }
  };

  const detectDuplicates = async () => {
    setError(null);
    try {
      await detectDuplicatesMutation.mutateAsync(undefined);
    } catch (err) {
      const status = (err as AxiosError | undefined)?.response?.status;
      if (status !== 503) setError(t('paperlessAudit.error'));
    }
  };

  const loadCorrespondents = () => {
    // Flipping `enabled` to true is sufficient — RQ fetches automatically
    // on the next render when the query has no cached data for the current
    // queryKey. No manual refetch() needed (and calling it on a disabled
    // query relies on a behavior we shouldn't depend on).
    setScanCorrespondents(true);
  };

  // --- Not Configured State ---
  if (notConfigured) {
    return (
      <div className="space-y-6">
        <PageHeader icon={FileSearch} title={t('paperlessAudit.title')} />
        <Alert variant="warning">{t('paperlessAudit.notConfigured')}</Alert>
      </div>
    );
  }

  const tabIcons: Record<AuditTab, LucideIcon> = {
    control: Play,
    review: ClipboardList,
    ocr: Eye,
    completeness: FileText,
    duplicates: Copy,
    correspondents: Users,
    stats: BarChart3,
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <PageHeader icon={FileSearch} title={t('paperlessAudit.title')} />

      {/* Error */}
      {error && <Alert variant="error">{error}</Alert>}

      {/* Tabs */}
      <div className="border-b border-gray-200 dark:border-gray-700">
        <nav className="flex space-x-4" role="tablist">
          {TABS.map((tab) => {
            const Icon = tabIcons[tab];
            return (
              <button
                key={tab}
                role="tab"
                aria-selected={activeTab === tab}
                onClick={() => setActiveTab(tab)}
                className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab
                    ? 'border-primary-500 text-primary-600 dark:text-primary-400'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300'
                }`}
              >
                <Icon className="w-4 h-4" />
                {t(`paperlessAudit.tabs.${tab}`)}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'control' && (
        <ControlTab
          t={t}
          auditStatus={auditStatus}
          mode={mode}
          setMode={setMode}
          fixMode={fixMode}
          setFixMode={setFixMode}
          confidenceThreshold={confidenceThreshold}
          setConfidenceThreshold={setConfidenceThreshold}
          starting={starting}
          stopping={stopping}
          onStart={startAudit}
          onStop={stopAudit}
        />
      )}

      {activeTab === 'review' && (
        <ReviewTab
          t={t}
          results={reviewResults}
          loading={reviewLoading}
          total={reviewTotal}
          page={reviewPage}
          setPage={setReviewPage}
          selectedIds={selectedIds}
          actionLoading={actionLoading}
          onToggleSelected={toggleSelected}
          onToggleSelectAll={toggleSelectAll}
          onApprove={approveResults}
          onSkip={skipResults}
          sortBy={reviewSortBy}
          sortOrder={reviewSortOrder}
          onSort={handleReviewSort}
          search={reviewSearch}
          onSearch={handleReviewSearch}
        />
      )}

      {activeTab === 'ocr' && (
        <OcrTab
          t={t}
          results={ocrResults}
          loading={ocrLoading}
          total={ocrTotal}
          page={ocrPage}
          setPage={setOcrPage}
          actionLoading={ocrActionLoading}
          onReOcr={reOcr}
        />
      )}

      {activeTab === 'completeness' && (
        <CompletenessTab
          t={t}
          results={completenessResults}
          loading={completenessLoading}
          total={completenessTotal}
          page={completenessPage}
          setPage={setCompletenessPage}
        />
      )}

      {activeTab === 'duplicates' && (
        <DuplicatesTab
          t={t}
          groups={duplicateGroups}
          loading={duplicatesLoading}
          detecting={detectingDuplicates}
          onDetect={detectDuplicates}
        />
      )}

      {activeTab === 'correspondents' && (
        <CorrespondentsTab
          t={t}
          clusters={correspondentClusters}
          loading={correspondentsLoading}
          threshold={corrThreshold}
          setThreshold={setCorrThreshold}
          onScan={loadCorrespondents}
        />
      )}

      {activeTab === 'stats' && (
        <StatsTab
          t={t}
          stats={stats}
          loading={statsLoading}
        />
      )}
    </div>
  );
}

interface ControlTabProps {
  t: TFunction;
  auditStatus: AuditStatus | null;
  mode: AuditMode;
  setMode: (m: AuditMode) => void;
  fixMode: FixMode;
  setFixMode: (m: FixMode) => void;
  confidenceThreshold: number;
  setConfidenceThreshold: (n: number) => void;
  starting: boolean;
  stopping: boolean;
  onStart: () => void;
  onStop: () => void;
}

// --- Control Tab Component ---
function ControlTab({ t, auditStatus, mode, setMode, fixMode, setFixMode, confidenceThreshold, setConfidenceThreshold, starting, stopping, onStart, onStop }: ControlTabProps) {
  const running = auditStatus?.running;
  const current = auditStatus?.progress ?? 0;
  const total = auditStatus?.total ?? 0;

  return (
    <div className="card p-6 space-y-6">
      {/* Mode Selector */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            {t('paperlessAudit.control.mode')}
          </label>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as AuditMode)}
            disabled={running}
            className="input w-full"
          >
            <option value="new_only">{t('paperlessAudit.control.modeNewOnly')}</option>
            <option value="full">{t('paperlessAudit.control.modeFull')}</option>
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            {t('paperlessAudit.control.fixMode')}
          </label>
          <select
            value={fixMode}
            onChange={(e) => setFixMode(e.target.value as FixMode)}
            disabled={running}
            className="input w-full"
          >
            <option value="review">{t('paperlessAudit.control.fixReview')}</option>
            <option value="auto_threshold">{t('paperlessAudit.control.fixAutoThreshold')}</option>
            <option value="auto_all">{t('paperlessAudit.control.fixAutoAll')}</option>
          </select>
        </div>
      </div>

      {/* Confidence Threshold (only for auto_threshold) */}
      {fixMode === 'auto_threshold' && (
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            {t('paperlessAudit.control.confidenceThreshold')}: {confidenceThreshold.toFixed(2)}
          </label>
          <input
            type="range"
            min="0.5"
            max="1.0"
            step="0.05"
            value={confidenceThreshold}
            onChange={(e) => setConfidenceThreshold(parseFloat(e.target.value))}
            disabled={running}
            className="w-full"
          />
          <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400 mt-1">
            <span>0.50</span>
            <span>1.00</span>
          </div>
        </div>
      )}

      {/* Progress */}
      {running && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <Loader className="w-4 h-4 animate-spin" />
            <span>{t('paperlessAudit.control.running')}</span>
            <span className="ml-auto">
              {t('paperlessAudit.control.progress', { current, total })}
            </span>
          </div>
          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2.5">
            <div
              className="bg-primary-600 h-2.5 rounded-full transition-all duration-300"
              style={{ width: `${total ? (current / total) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-3">
        {!running ? (
          <button
            onClick={onStart}
            disabled={starting}
            className="btn-primary flex items-center gap-2"
          >
            {starting ? <Loader className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            {t('paperlessAudit.control.start')}
          </button>
        ) : (
          <button
            onClick={onStop}
            disabled={stopping}
            className="btn btn-danger flex items-center gap-2"
          >
            {stopping ? <Loader className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
            {t('paperlessAudit.control.stop')}
          </button>
        )}
      </div>
    </div>
  );
}

interface ReviewTabProps {
  t: TFunction;
  results: AuditResult[];
  loading: boolean;
  total: number;
  page: number;
  setPage: (n: number) => void;
  selectedIds: Set<number>;
  actionLoading: Set<number>;
  onToggleSelected: (id: number) => void;
  onToggleSelectAll: () => void;
  onApprove: (ids: number[]) => void;
  onSkip: (ids: number[]) => void;
  sortBy: string | null;
  sortOrder: 'asc' | 'desc';
  onSort: (column: string) => void;
  search: string;
  onSearch: (value: string) => void;
}

interface SortHeaderProps {
  column: string;
  children: React.ReactNode;
  className?: string;
}

// --- Review Tab Component ---
function ReviewTab({ t, results, loading, total, page, setPage, selectedIds, actionLoading, onToggleSelected, onToggleSelectAll, onApprove, onSkip, sortBy, sortOrder, onSort, search, onSearch }: ReviewTabProps) {
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const allSelected = selectedIds.size === results.length && results.length > 0;

  const SortHeader = ({ column, children, className = '' }: SortHeaderProps) => {
    const active = sortBy === column;
    return (
      <th
        className={`text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400 cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-200 transition-colors ${className}`}
        onClick={() => onSort(column)}
      >
        <span className="inline-flex items-center gap-1">
          {children}
          {active && (
            <span className="text-primary-500">{sortOrder === 'asc' ? '\u2191' : '\u2193'}</span>
          )}
        </span>
      </th>
    );
  };

  return (
    <div className="space-y-4">
      {/* Search + Bulk Actions */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <FileSearch className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder={t('paperlessAudit.review.searchPlaceholder')}
            className="input pl-9 py-1.5 text-sm w-full"
          />
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={onToggleSelectAll}
              className="rounded border-gray-300 dark:border-gray-600"
            />
            {t('common.all')}
          </label>
          {selectedIds.size > 0 && (
            <>
              <button
                onClick={() => onApprove([...selectedIds])}
                className="btn-primary text-sm flex items-center gap-1"
              >
                <Check className="w-3.5 h-3.5" />
                {t('paperlessAudit.review.approveSelected')}
              </button>
              <button
                onClick={() => onSkip([...selectedIds])}
                className="btn-secondary text-sm flex items-center gap-1"
              >
                <X className="w-3.5 h-3.5" />
                {t('paperlessAudit.review.skipSelected')}
              </button>
            </>
          )}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      ) : results.length === 0 ? (
        <div className="card p-8 text-center">
          <ClipboardList className="w-12 h-12 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
          <p className="text-gray-500 dark:text-gray-400">{search ? t('paperlessAudit.review.noSearchResults') : t('paperlessAudit.review.noResults')}</p>
        </div>
      ) : (
      <>
      {/* Results Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700">
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400 w-8"></th>
              <SortHeader column="paperless_doc_id">{t('paperlessAudit.review.docId')}</SortHeader>
              <SortHeader column="current_title">{t('paperlessAudit.review.currentTitle')} / {t('paperlessAudit.review.suggestedTitle')}</SortHeader>
              <SortHeader column="current_correspondent">{t('paperlessAudit.review.correspondent')}</SortHeader>
              <SortHeader column="current_document_type">{t('paperlessAudit.review.type')}</SortHeader>
              <SortHeader column="current_date">{t('paperlessAudit.review.date')}</SortHeader>
              <SortHeader column="detected_language">{t('paperlessAudit.review.language')}</SortHeader>
              <SortHeader column="current_storage_path">{t('paperlessAudit.review.storagePath')}</SortHeader>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.missing')}</th>
              <SortHeader column="confidence">{t('paperlessAudit.review.confidence')}</SortHeader>
              <th className="text-right py-3 px-2 font-medium text-gray-500 dark:text-gray-400"></th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => {
              const isLoading = actionLoading.has(r.id);
              return (
                <tr key={r.id} className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="py-3 px-2">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(r.id)}
                      onChange={() => onToggleSelected(r.id)}
                      className="rounded border-gray-300 dark:border-gray-600"
                    />
                  </td>
                  <td className="py-3 px-2 text-gray-900 dark:text-gray-100 font-mono text-xs">{r.paperless_doc_id}</td>
                  <td className="py-3 px-2 max-w-xs">
                    <DiffValue current={r.current_title} suggested={r.suggested_title} />
                    {r.suggested_tags && r.suggested_tags.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {r.suggested_tags.map((tag, i) => (
                          <Badge key={i} color="accent">{tag}</Badge>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="py-3 px-2">
                    <DiffValue current={r.current_correspondent} suggested={r.suggested_correspondent} />
                  </td>
                  <td className="py-3 px-2">
                    <DiffValue current={r.current_document_type} suggested={r.suggested_document_type} />
                  </td>
                  <td className="py-3 px-2">
                    <DiffValue current={r.current_date} suggested={r.suggested_date} />
                  </td>
                  <td className="py-3 px-2">
                    {r.detected_language && (
                      <Badge color="blue" className="font-mono">{r.detected_language}</Badge>
                    )}
                  </td>
                  <td className="py-3 px-2">
                    <DiffValue current={r.current_storage_path} suggested={r.suggested_storage_path} />
                  </td>
                  <td className="py-3 px-2">
                    {r.missing_fields && r.missing_fields.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {r.missing_fields.map((f, i) => (
                          <Badge key={i} color="yellow">{f}</Badge>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="py-3 px-2">
                    <ConfidenceBadge value={r.confidence} />
                  </td>
                  <td className="py-3 px-2 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => onApprove([r.id])}
                        disabled={isLoading}
                        className="btn-icon text-green-600 dark:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/20"
                        title={t('paperlessAudit.review.approve')}
                      >
                        {isLoading ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                      </button>
                      <button
                        onClick={() => onSkip([r.id])}
                        disabled={isLoading}
                        className="btn-icon btn-icon-ghost"
                        title={t('paperlessAudit.review.skip')}
                      >
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
      )}
      </>
      )}
    </div>
  );
}

interface OcrTabProps {
  t: TFunction;
  results: AuditResult[];
  loading: boolean;
  total: number;
  page: number;
  setPage: (n: number) => void;
  actionLoading: Set<number>;
  onReOcr: (ids: number[]) => void;
}

// --- OCR Tab Component ---
function OcrTab({ t, results, loading, total, page, setPage, actionLoading, onReOcr }: OcrTabProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader className="w-6 h-6 animate-spin text-gray-400" />
      </div>
    );
  }

  if (results.length === 0) {
    return (
      <div className="card p-8 text-center">
        <Eye className="w-12 h-12 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
        <p className="text-gray-500 dark:text-gray-400">{t('paperlessAudit.ocr.noIssues')}</p>
      </div>
    );
  }

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700">
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.docId')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.currentTitle')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.ocr.quality')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.ocr.issues')}</th>
              <th className="text-right py-3 px-2 font-medium text-gray-500 dark:text-gray-400"></th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => {
              const isLoading = actionLoading.has(r.id);
              const quality = r.ocr_quality ?? 0;
              return (
                <tr key={r.id} className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="py-3 px-2 text-gray-900 dark:text-gray-100 font-mono text-xs">{r.paperless_doc_id}</td>
                  <td className="py-3 px-2 text-gray-900 dark:text-gray-100">{r.current_title || r.suggested_title || '-'}</td>
                  <td className="py-3 px-2">
                    <span className={`inline-flex items-center gap-1.5 ${OCR_TEXT_COLORS[quality] || 'text-gray-500'}`}>
                      <span className={`w-2.5 h-2.5 rounded-full ${OCR_COLORS[quality] || 'bg-gray-400'}`} />
                      {quality}/5
                    </span>
                  </td>
                  <td className="py-3 px-2 text-gray-600 dark:text-gray-400 text-xs max-w-xs truncate">
                    {r.ocr_issues || '-'}
                  </td>
                  <td className="py-3 px-2 text-right">
                    <button
                      onClick={() => onReOcr([r.id])}
                      disabled={isLoading}
                      className="btn-secondary text-xs flex items-center gap-1 ml-auto"
                      title={t('paperlessAudit.ocr.reocr')}
                    >
                      {isLoading ? <Loader className="w-3.5 h-3.5 animate-spin" /> : <RotateCcw className="w-3.5 h-3.5" />}
                      {t('paperlessAudit.ocr.reocr')}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
      )}
    </div>
  );
}

interface StatsTabProps {
  t: TFunction;
  stats: AuditStats | null;
  loading: boolean;
}

// --- Stats Tab Component ---
function StatsTab({ t, stats, loading }: StatsTabProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader className="w-6 h-6 animate-spin text-gray-400" />
      </div>
    );
  }

  if (!stats) return null;

  const statCards = [
    { key: 'totalAudited', value: stats.total_audited ?? 0, color: 'bg-blue-500' },
    { key: 'changesNeeded', value: stats.changes_needed ?? 0, color: 'bg-yellow-500' },
    { key: 'applied', value: stats.applied ?? 0, color: 'bg-green-500' },
    { key: 'skipped', value: stats.skipped ?? 0, color: 'bg-gray-500' },
    { key: 'pending', value: stats.pending ?? 0, color: 'bg-orange-500' },
    { key: 'failed', value: stats.failed ?? 0, color: 'bg-red-500' },
    { key: 'missingMetadata', value: stats.missing_metadata_count ?? 0, color: 'bg-yellow-500' },
    { key: 'duplicateGroups', value: stats.duplicate_groups ?? 0, color: 'bg-purple-500' },
  ];

  const ocrDist = stats.ocr_quality_distribution || stats.ocr_distribution || {};
  const maxOcr = Math.max(...Object.values(ocrDist), 1);

  return (
    <div className="space-y-6">
      {/* Stat Cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        {statCards.map(({ key, value, color }) => (
          <div key={key} className="card p-4">
            <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">{t(`paperlessAudit.stats.${key}`)}</div>
            <div className="text-2xl font-bold text-gray-900 dark:text-white">{value}</div>
            <div className={`h-1 ${color} rounded-full mt-2 w-full opacity-50`} />
          </div>
        ))}
      </div>

      {/* Average Confidence */}
      {stats.avg_confidence != null && (
        <div className="card p-4">
          <div className="text-sm text-gray-500 dark:text-gray-400 mb-1">{t('paperlessAudit.stats.avgConfidence')}</div>
          <div className="text-xl font-bold text-gray-900 dark:text-white">
            {(stats.avg_confidence * 100).toFixed(1)}%
          </div>
        </div>
      )}

      {/* OCR Quality Distribution */}
      {Object.keys(ocrDist).length > 0 && (
        <div className="card p-4">
          <div className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">
            {t('paperlessAudit.stats.ocrDistribution')}
          </div>
          <div className="space-y-2">
            {[1, 2, 3, 4, 5].map((level) => {
              const count = ocrDist[level] || 0;
              const pct = (count / maxOcr) * 100;
              return (
                <div key={level} className="flex items-center gap-3">
                  <span className={`text-sm font-mono w-4 ${OCR_TEXT_COLORS[level]}`}>{level}</span>
                  <div className="flex-1 h-5 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                    <div
                      className={`h-full ${OCR_COLORS[level]} transition-all duration-300 rounded`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-sm text-gray-600 dark:text-gray-400 w-10 text-right">{count}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Language Distribution */}
      {stats.language_distribution && Object.keys(stats.language_distribution).length > 0 && (
        <div className="card p-4">
          <div className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">
            {t('paperlessAudit.stats.languageDistribution')}
          </div>
          <div className="flex flex-wrap gap-3">
            {Object.entries(stats.language_distribution)
              .sort(([, a], [, b]) => b - a)
              .map(([lang, count]) => (
                <Badge key={lang} color="blue" className="font-mono text-sm px-3 py-2">
                  {lang} <span className="text-gray-600 dark:text-gray-400 ml-1">{count}</span>
                </Badge>
              ))}
          </div>
        </div>
      )}

      {/* Completeness Distribution */}
      {stats.completeness_distribution && Object.keys(stats.completeness_distribution).length > 0 && (
        <div className="card p-4">
          <div className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">
            {t('paperlessAudit.stats.completenessDistribution')}
          </div>
          <div className="space-y-2">
            {[1, 2, 3, 4, 5].map((level) => {
              const completenessDist = stats.completeness_distribution ?? {};
              const count = completenessDist[level] || 0;
              const maxComp = Math.max(...Object.values(completenessDist), 1);
              const pct = (count / maxComp) * 100;
              return (
                <div key={level} className="flex items-center gap-3">
                  <span className={`text-sm font-mono w-4 ${OCR_TEXT_COLORS[level]}`}>{level}</span>
                  <div className="flex-1 h-5 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                    <div
                      className={`h-full ${OCR_COLORS[level]} transition-all duration-300 rounded`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-sm text-gray-600 dark:text-gray-400 w-10 text-right">{count}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

interface CompletenessTabProps {
  t: TFunction;
  results: AuditResult[];
  loading: boolean;
  total: number;
  page: number;
  setPage: (n: number) => void;
}

// --- Completeness Tab Component ---
function CompletenessTab({ t, results, loading, total, page, setPage }: CompletenessTabProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader className="w-6 h-6 animate-spin text-gray-400" />
      </div>
    );
  }

  if (results.length === 0) {
    return (
      <div className="card p-8 text-center">
        <FileText className="w-12 h-12 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
        <p className="text-gray-500 dark:text-gray-400">{t('paperlessAudit.completeness.noIssues')}</p>
      </div>
    );
  }

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700">
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.docId')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.currentTitle')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.completeness.score')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.completeness.issues')}</th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => {
              const score = r.content_completeness ?? 0;
              return (
                <tr key={r.id} className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="py-3 px-2 text-gray-900 dark:text-gray-100 font-mono text-xs">{r.paperless_doc_id}</td>
                  <td className="py-3 px-2 text-gray-900 dark:text-gray-100">{r.current_title || '-'}</td>
                  <td className="py-3 px-2">
                    <span className={`inline-flex items-center gap-1.5 ${OCR_TEXT_COLORS[score] || 'text-gray-500'}`}>
                      <span className={`w-2.5 h-2.5 rounded-full ${OCR_COLORS[score] || 'bg-gray-400'}`} />
                      {score}/5
                    </span>
                  </td>
                  <td className="py-3 px-2 text-gray-600 dark:text-gray-400 text-xs max-w-xs truncate">
                    {r.completeness_issues || '-'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
      )}
    </div>
  );
}

interface DuplicatesTabProps {
  t: TFunction;
  groups: DuplicateGroup[];
  loading: boolean;
  detecting: boolean;
  onDetect: () => void;
}

// --- Duplicates Tab Component ---
function DuplicatesTab({ t, groups, loading, detecting, onDetect }: DuplicatesTabProps) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <button
          onClick={onDetect}
          disabled={detecting}
          className="btn-primary flex items-center gap-2"
        >
          {detecting ? <Loader className="w-4 h-4 animate-spin" /> : <Copy className="w-4 h-4" />}
          {t('paperlessAudit.duplicates.detect')}
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      ) : groups.length === 0 ? (
        <div className="card p-8 text-center">
          <Copy className="w-12 h-12 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
          <p className="text-gray-500 dark:text-gray-400">{t('paperlessAudit.duplicates.noGroups')}</p>
        </div>
      ) : (
        <div className="space-y-4">
          {groups.map((group) => (
            <div key={group.group_id} className="card p-4">
              <div className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                {t('paperlessAudit.duplicates.group')}: {group.group_id}
              </div>
              <div className="space-y-2">
                {group.documents.map((doc) => (
                  <div key={doc.id} className="flex items-center gap-3 p-2 rounded bg-gray-50 dark:bg-gray-800/50">
                    <span className="font-mono text-xs text-gray-500">{doc.paperless_doc_id}</span>
                    <span className="text-sm text-gray-900 dark:text-gray-100 flex-1 truncate">{doc.current_title || '-'}</span>
                    <span className="text-xs text-gray-500 dark:text-gray-400">{doc.current_correspondent}</span>
                    {doc.duplicate_score != null && (
                      <Badge color="amber" className="font-mono">{(doc.duplicate_score * 100).toFixed(0)}%</Badge>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface CorrespondentsTabProps {
  t: TFunction;
  clusters: CorrespondentCluster[];
  loading: boolean;
  threshold: number;
  setThreshold: (n: number) => void;
  onScan: () => void;
}

// --- Correspondents Tab Component ---
function CorrespondentsTab({ t, clusters, loading, threshold, setThreshold, onScan }: CorrespondentsTabProps) {
  return (
    <div className="space-y-4">
      <div className="card p-4 flex flex-col sm:flex-row items-start sm:items-center gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            {t('paperlessAudit.correspondents.threshold')}: {threshold.toFixed(2)}
          </label>
          <input
            type="range"
            min="0.5"
            max="1.0"
            step="0.01"
            value={threshold}
            onChange={(e) => setThreshold(parseFloat(e.target.value))}
            className="w-full"
          />
        </div>
        <button
          onClick={onScan}
          disabled={loading}
          className="btn-primary flex items-center gap-2"
        >
          {loading ? <Loader className="w-4 h-4 animate-spin" /> : <Users className="w-4 h-4" />}
          {t('paperlessAudit.correspondents.scan')}
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      ) : clusters.length === 0 ? (
        <div className="card p-8 text-center">
          <Users className="w-12 h-12 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
          <p className="text-gray-500 dark:text-gray-400">{t('paperlessAudit.correspondents.noClusters')}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {clusters.map((cluster, i) => (
            <div key={i} className="card p-4">
              <div className="font-medium text-gray-900 dark:text-white mb-2">{cluster.canonical}</div>
              <div className="flex flex-wrap gap-2">
                {cluster.variants.map((v, j) => (
                  <Badge key={j} color="amber">
                    {v.name}
                    <span className="text-xs font-mono opacity-70 ml-1">{(v.similarity * 100).toFixed(0)}%</span>
                  </Badge>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface DiffValueProps {
  current?: string | null;
  suggested?: string | null;
}

// --- Shared Components ---
function DiffValue({ current, suggested }: DiffValueProps) {
  if (!suggested || current === suggested) {
    return <span className="text-gray-900 dark:text-gray-100">{current || '-'}</span>;
  }
  return (
    <div className="space-y-0.5">
      {current && (
        <div className="text-red-600 dark:text-red-400 line-through text-xs">{current}</div>
      )}
      <div className="text-green-600 dark:text-green-400 text-xs font-medium">{suggested}</div>
    </div>
  );
}

function ConfidenceBadge({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-gray-400">-</span>;
  const pct = (value * 100).toFixed(0);
  const color = value >= 0.8 ? 'text-green-600 dark:text-green-400' : value >= 0.6 ? 'text-yellow-600 dark:text-yellow-400' : 'text-red-600 dark:text-red-400';
  return <span className={`text-xs font-medium ${color}`}>{pct}%</span>;
}

interface PaginationProps {
  page: number;
  totalPages: number;
  onPageChange: (n: number) => void;
}

function Pagination({ page, totalPages, onPageChange }: PaginationProps) {
  return (
    <div className="flex items-center justify-center gap-2 pt-2">
      <button
        onClick={() => onPageChange(Math.max(0, page - 1))}
        disabled={page === 0}
        className="btn-icon btn-icon-ghost disabled:opacity-30"
      >
        <ChevronLeft className="w-4 h-4" />
      </button>
      <span className="text-sm text-gray-600 dark:text-gray-400">
        {page + 1} / {totalPages}
      </span>
      <button
        onClick={() => onPageChange(Math.min(totalPages - 1, page + 1))}
        disabled={page >= totalPages - 1}
        className="btn-icon btn-icon-ghost disabled:opacity-30"
      >
        <ChevronRight className="w-4 h-4" />
      </button>
    </div>
  );
}
