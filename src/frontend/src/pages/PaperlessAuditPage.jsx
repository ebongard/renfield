/**
 * Paperless Document Audit Page
 *
 * Admin page for auditing Paperless documents using LLM analysis.
 * Provides audit control, review queue, OCR issue tracking, and statistics.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import {
  FileSearch, Play, Square, Loader, AlertCircle, Check, X,
  RotateCcw, BarChart3, ClipboardList, Eye, ChevronLeft, ChevronRight
} from 'lucide-react';

const TABS = ['control', 'review', 'ocr', 'stats'];
const PAGE_SIZE = 20;

const OCR_COLORS = {
  1: 'bg-red-500',
  2: 'bg-orange-500',
  3: 'bg-yellow-500',
  4: 'bg-green-500',
  5: 'bg-green-700',
};

const OCR_TEXT_COLORS = {
  1: 'text-red-600 dark:text-red-400',
  2: 'text-orange-600 dark:text-orange-400',
  3: 'text-yellow-600 dark:text-yellow-400',
  4: 'text-green-600 dark:text-green-400',
  5: 'text-green-700 dark:text-green-300',
};

export default function PaperlessAuditPage() {
  const { t } = useTranslation();
  const { getAccessToken } = useAuth();

  const [activeTab, setActiveTab] = useState('control');
  const [notConfigured, setNotConfigured] = useState(false);
  const [error, setError] = useState(null);

  // Control tab state
  const [auditStatus, setAuditStatus] = useState(null);
  const [mode, setMode] = useState('new_only');
  const [fixMode, setFixMode] = useState('review');
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.8);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const pollRef = useRef(null);

  // Review tab state
  const [reviewResults, setReviewResults] = useState([]);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewPage, setReviewPage] = useState(0);
  const [reviewTotal, setReviewTotal] = useState(0);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [actionLoading, setActionLoading] = useState(new Set());

  // OCR tab state
  const [ocrResults, setOcrResults] = useState([]);
  const [ocrLoading, setOcrLoading] = useState(false);
  const [ocrPage, setOcrPage] = useState(0);
  const [ocrTotal, setOcrTotal] = useState(0);
  const [ocrActionLoading, setOcrActionLoading] = useState(new Set());

  // Stats tab state
  const [stats, setStats] = useState(null);
  const [statsLoading, setStatsLoading] = useState(false);

  const authHeaders = useCallback(async () => {
    const token = await getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }, [getAccessToken]);

  const handleApiError = useCallback((err) => {
    if (err.response?.status === 503) {
      setNotConfigured(true);
      return;
    }
    setError(t('paperlessAudit.error'));
  }, [t]);

  // --- Control Tab ---
  const loadStatus = useCallback(async () => {
    try {
      const headers = await authHeaders();
      const res = await apiClient.get('/api/admin/paperless-audit/status', { headers });
      setAuditStatus(res.data);
      setNotConfigured(false);
    } catch (err) {
      handleApiError(err);
    }
  }, [authHeaders, handleApiError]);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  // Poll status while running
  useEffect(() => {
    if (auditStatus?.running) {
      pollRef.current = setInterval(loadStatus, 2000);
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [auditStatus?.running, loadStatus]);

  const startAudit = async () => {
    setStarting(true);
    setError(null);
    try {
      const headers = await authHeaders();
      await apiClient.post('/api/admin/paperless-audit/start', {
        mode,
        fix_mode: fixMode,
        confidence_threshold: confidenceThreshold,
      }, { headers });
      await loadStatus();
    } catch (err) {
      handleApiError(err);
    } finally {
      setStarting(false);
    }
  };

  const stopAudit = async () => {
    setStopping(true);
    try {
      const headers = await authHeaders();
      await apiClient.post('/api/admin/paperless-audit/stop', {}, { headers });
      await loadStatus();
    } catch (err) {
      handleApiError(err);
    } finally {
      setStopping(false);
    }
  };

  // --- Review Tab ---
  const loadReview = useCallback(async () => {
    setReviewLoading(true);
    setError(null);
    try {
      const headers = await authHeaders();
      const res = await apiClient.get('/api/admin/paperless-audit/results', {
        headers,
        params: { status: 'pending', changes_needed: true, limit: PAGE_SIZE, offset: reviewPage * PAGE_SIZE },
      });
      setReviewResults(res.data.results || res.data || []);
      setReviewTotal(res.data.total ?? (res.data.results || res.data || []).length);
      setSelectedIds(new Set());
      setNotConfigured(false);
    } catch (err) {
      handleApiError(err);
    } finally {
      setReviewLoading(false);
    }
  }, [authHeaders, handleApiError, reviewPage]);

  useEffect(() => {
    if (activeTab === 'review') loadReview();
  }, [activeTab, loadReview]);

  const approveResults = async (ids) => {
    setActionLoading(prev => new Set([...prev, ...ids]));
    try {
      const headers = await authHeaders();
      await apiClient.post('/api/admin/paperless-audit/apply', { result_ids: ids }, { headers });
      await loadReview();
    } catch (err) {
      handleApiError(err);
    } finally {
      setActionLoading(prev => {
        const next = new Set(prev);
        ids.forEach(id => next.delete(id));
        return next;
      });
    }
  };

  const skipResults = async (ids) => {
    setActionLoading(prev => new Set([...prev, ...ids]));
    try {
      const headers = await authHeaders();
      await apiClient.post('/api/admin/paperless-audit/skip', { result_ids: ids }, { headers });
      await loadReview();
    } catch (err) {
      handleApiError(err);
    } finally {
      setActionLoading(prev => {
        const next = new Set(prev);
        ids.forEach(id => next.delete(id));
        return next;
      });
    }
  };

  const toggleSelected = (id) => {
    setSelectedIds(prev => {
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
      setSelectedIds(new Set(reviewResults.map(r => r.id)));
    }
  };

  // --- OCR Tab ---
  const loadOcr = useCallback(async () => {
    setOcrLoading(true);
    setError(null);
    try {
      const headers = await authHeaders();
      const res = await apiClient.get('/api/admin/paperless-audit/results', {
        headers,
        params: { ocr_quality_max: 2, limit: PAGE_SIZE, offset: ocrPage * PAGE_SIZE },
      });
      setOcrResults(res.data.results || res.data || []);
      setOcrTotal(res.data.total ?? (res.data.results || res.data || []).length);
      setNotConfigured(false);
    } catch (err) {
      handleApiError(err);
    } finally {
      setOcrLoading(false);
    }
  }, [authHeaders, handleApiError, ocrPage]);

  useEffect(() => {
    if (activeTab === 'ocr') loadOcr();
  }, [activeTab, loadOcr]);

  const reOcr = async (ids) => {
    setOcrActionLoading(prev => new Set([...prev, ...ids]));
    try {
      const headers = await authHeaders();
      await apiClient.post('/api/admin/paperless-audit/re-ocr', { result_ids: ids }, { headers });
      await loadOcr();
    } catch (err) {
      handleApiError(err);
    } finally {
      setOcrActionLoading(prev => {
        const next = new Set(prev);
        ids.forEach(id => next.delete(id));
        return next;
      });
    }
  };

  // --- Stats Tab ---
  const loadStats = useCallback(async () => {
    setStatsLoading(true);
    setError(null);
    try {
      const headers = await authHeaders();
      const res = await apiClient.get('/api/admin/paperless-audit/stats', { headers });
      setStats(res.data);
      setNotConfigured(false);
    } catch (err) {
      handleApiError(err);
    } finally {
      setStatsLoading(false);
    }
  }, [authHeaders, handleApiError]);

  useEffect(() => {
    if (activeTab === 'stats') loadStats();
  }, [activeTab, loadStats]);

  // --- Not Configured State ---
  if (notConfigured) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white flex items-center gap-3">
            <FileSearch className="w-7 h-7" />
            {t('paperlessAudit.title')}
          </h1>
        </div>
        <div className="card p-8 text-center">
          <AlertCircle className="w-12 h-12 text-yellow-500 mx-auto mb-4" />
          <p className="text-gray-600 dark:text-gray-300">{t('paperlessAudit.notConfigured')}</p>
        </div>
      </div>
    );
  }

  const tabIcons = {
    control: Play,
    review: ClipboardList,
    ocr: Eye,
    stats: BarChart3,
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white flex items-center gap-3">
          <FileSearch className="w-7 h-7" />
          {t('paperlessAudit.title')}
        </h1>
      </div>

      {/* Error */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 flex items-center gap-3">
          <AlertCircle className="w-5 h-5 text-red-500 shrink-0" />
          <p className="text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

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

// --- Control Tab Component ---
function ControlTab({ t, auditStatus, mode, setMode, fixMode, setFixMode, confidenceThreshold, setConfidenceThreshold, starting, stopping, onStart, onStop }) {
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
            onChange={(e) => setMode(e.target.value)}
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
            onChange={(e) => setFixMode(e.target.value)}
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
            className="btn-secondary flex items-center gap-2 text-red-600 dark:text-red-400 border-red-300 dark:border-red-700 hover:bg-red-50 dark:hover:bg-red-900/20"
          >
            {stopping ? <Loader className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
            {t('paperlessAudit.control.stop')}
          </button>
        )}
      </div>
    </div>
  );
}

// --- Review Tab Component ---
function ReviewTab({ t, results, loading, total, page, setPage, selectedIds, actionLoading, onToggleSelected, onToggleSelectAll, onApprove, onSkip }) {
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
        <ClipboardList className="w-12 h-12 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
        <p className="text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.noResults')}</p>
      </div>
    );
  }

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const allSelected = selectedIds.size === results.length && results.length > 0;

  return (
    <div className="space-y-4">
      {/* Bulk Actions */}
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

      {/* Results Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700">
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400 w-8"></th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.docId')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.currentTitle')} / {t('paperlessAudit.review.suggestedTitle')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.correspondent')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.type')}</th>
              <th className="text-left py-3 px-2 font-medium text-gray-500 dark:text-gray-400">{t('paperlessAudit.review.confidence')}</th>
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
                  <td className="py-3 px-2 text-gray-900 dark:text-gray-100 font-mono text-xs">{r.document_id}</td>
                  <td className="py-3 px-2 max-w-xs">
                    <DiffValue current={r.current_title} suggested={r.suggested_title} />
                    {r.suggested_tags && r.suggested_tags.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {r.suggested_tags.map((tag, i) => (
                          <span key={i} className="inline-block px-1.5 py-0.5 text-xs bg-primary-100 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300 rounded">
                            {tag}
                          </span>
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
                    <ConfidenceBadge value={r.confidence} />
                  </td>
                  <td className="py-3 px-2 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => onApprove([r.id])}
                        disabled={isLoading}
                        className="p-1.5 rounded text-green-600 dark:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/20 transition-colors"
                        title={t('paperlessAudit.review.approve')}
                      >
                        {isLoading ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                      </button>
                      <button
                        onClick={() => onSkip([r.id])}
                        disabled={isLoading}
                        className="p-1.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
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
    </div>
  );
}

// --- OCR Tab Component ---
function OcrTab({ t, results, loading, total, page, setPage, actionLoading, onReOcr }) {
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
                  <td className="py-3 px-2 text-gray-900 dark:text-gray-100 font-mono text-xs">{r.document_id}</td>
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

// --- Stats Tab Component ---
function StatsTab({ t, stats, loading }) {
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
  ];

  const ocrDist = stats.ocr_distribution || {};
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
    </div>
  );
}

// --- Shared Components ---
function DiffValue({ current, suggested }) {
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

function ConfidenceBadge({ value }) {
  if (value == null) return <span className="text-gray-400">-</span>;
  const pct = (value * 100).toFixed(0);
  const color = value >= 0.8 ? 'text-green-600 dark:text-green-400' : value >= 0.6 ? 'text-yellow-600 dark:text-yellow-400' : 'text-red-600 dark:text-red-400';
  return <span className={`text-xs font-medium ${color}`}>{pct}%</span>;
}

function Pagination({ page, totalPages, onPageChange }) {
  return (
    <div className="flex items-center justify-center gap-2 pt-2">
      <button
        onClick={() => onPageChange(Math.max(0, page - 1))}
        disabled={page === 0}
        className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30 transition-colors"
      >
        <ChevronLeft className="w-4 h-4 text-gray-600 dark:text-gray-300" />
      </button>
      <span className="text-sm text-gray-600 dark:text-gray-400">
        {page + 1} / {totalPages}
      </span>
      <button
        onClick={() => onPageChange(Math.min(totalPages - 1, page + 1))}
        disabled={page >= totalPages - 1}
        className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30 transition-colors"
      >
        <ChevronRight className="w-4 h-4 text-gray-600 dark:text-gray-300" />
      </button>
    </div>
  );
}
