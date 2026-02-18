/**
 * Admin Maintenance Page
 *
 * Admin page for system maintenance operations:
 * - FTS reindex, HA keyword refresh
 * - Re-embed all vectors
 * - Intent debugging
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import {
  Wrench, Search, Database, Bug, Loader, AlertCircle, CheckCircle
} from 'lucide-react';

function ActionRow({ title, description, buttonLabel, icon: Icon, loading, onAction, variant }) {
  return (
    <div className="flex items-center justify-between py-3">
      <div className="flex-1 min-w-0 mr-4">
        <h3 className="text-sm font-medium text-gray-900 dark:text-white">{title}</h3>
        <p className="text-sm text-gray-500 dark:text-gray-400">{description}</p>
      </div>
      <button
        onClick={onAction}
        disabled={loading}
        className={`btn flex items-center gap-2 shrink-0 ${
          variant === 'warning' ? 'btn-secondary' : 'btn-primary'
        }`}
      >
        {loading ? (
          <Loader className="w-4 h-4 animate-spin" />
        ) : Icon ? (
          <Icon className="w-4 h-4" />
        ) : null}
        {buttonLabel}
      </button>
    </div>
  );
}

function ResultBox({ success, children }) {
  if (!children) return null;
  return (
    <div className={`mt-2 p-3 rounded-lg flex items-start gap-2 text-sm ${
      success
        ? 'bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 text-green-700 dark:text-green-400'
        : 'bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400'
    }`}>
      {success ? (
        <CheckCircle className="w-4 h-4 shrink-0 mt-0.5" />
      ) : (
        <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
      )}
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export default function MaintenancePage() {
  const { t } = useTranslation();
  const { getAccessToken } = useAuth();

  // FTS Reindex
  const [ftsLoading, setFtsLoading] = useState(false);
  const [ftsResult, setFtsResult] = useState(null);
  const [ftsError, setFtsError] = useState(null);

  // HA Keywords
  const [kwLoading, setKwLoading] = useState(false);
  const [kwResult, setKwResult] = useState(null);
  const [kwError, setKwError] = useState(null);

  // Re-embed
  const [embedLoading, setEmbedLoading] = useState(false);
  const [embedResult, setEmbedResult] = useState(null);
  const [embedError, setEmbedError] = useState(null);

  // Intent debug
  const [intentMessage, setIntentMessage] = useState('');
  const [intentLoading, setIntentLoading] = useState(false);
  const [intentResult, setIntentResult] = useState(null);
  const [intentError, setIntentError] = useState(null);

  const getAuthHeaders = async () => {
    const token = await getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  };

  // --- Actions ---

  const handleReindexFts = async () => {
    setFtsLoading(true);
    setFtsResult(null);
    setFtsError(null);
    try {
      const headers = await getAuthHeaders();
      const response = await apiClient.post('/api/knowledge/reindex-fts', null, { headers });
      setFtsResult(response.data);
    } catch (err) {
      setFtsError(err.response?.data?.detail || t('maintenance.errors.reindexFailed'));
    } finally {
      setFtsLoading(false);
    }
  };

  const handleRefreshKeywords = async () => {
    setKwLoading(true);
    setKwResult(null);
    setKwError(null);
    try {
      const headers = await getAuthHeaders();
      const response = await apiClient.post('/admin/refresh-keywords', null, { headers });
      setKwResult(response.data);
    } catch (err) {
      setKwError(err.response?.data?.detail || t('maintenance.errors.refreshKeywordsFailed'));
    } finally {
      setKwLoading(false);
    }
  };

  const handleReembed = async () => {
    if (!window.confirm(t('maintenance.embeddings.confirmReembed'))) return;
    setEmbedLoading(true);
    setEmbedResult(null);
    setEmbedError(null);
    try {
      const headers = await getAuthHeaders();
      const response = await apiClient.post('/admin/reembed', null, {
        headers,
        timeout: 1800000 // 30 minutes
      });
      setEmbedResult(response.data);
    } catch (err) {
      setEmbedError(err.response?.data?.detail || t('maintenance.errors.reembedFailed'));
    } finally {
      setEmbedLoading(false);
    }
  };

  const handleTestIntent = async () => {
    if (!intentMessage.trim()) return;
    setIntentLoading(true);
    setIntentResult(null);
    setIntentError(null);
    try {
      const headers = await getAuthHeaders();
      const response = await apiClient.post(
        `/debug/intent?message=${encodeURIComponent(intentMessage.trim())}`,
        null,
        { headers }
      );
      setIntentResult(response.data);
    } catch (err) {
      setIntentError(err.response?.data?.detail || t('maintenance.errors.intentTestFailed'));
    } finally {
      setIntentLoading(false);
    }
  };

  return (
    <div className="p-6 max-w-4xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <Wrench className="w-8 h-8 text-blue-500" />
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            {t('maintenance.title')}
          </h1>
          <p className="text-gray-600 dark:text-gray-400">{t('maintenance.subtitle')}</p>
        </div>
      </div>

      {/* Section 1: Search & Indexing */}
      <div className="card mb-6">
        <div className="flex items-center gap-2 mb-4">
          <Search className="w-6 h-6 text-blue-500" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('maintenance.searchIndexing.title')}
          </h2>
        </div>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          {t('maintenance.searchIndexing.description')}
        </p>

        {/* Reindex FTS */}
        <ActionRow
          title={t('maintenance.searchIndexing.reindexFts')}
          description={t('maintenance.searchIndexing.reindexFtsDescription')}
          buttonLabel={t('maintenance.searchIndexing.reindexFts')}
          loading={ftsLoading}
          onAction={handleReindexFts}
        />
        {ftsResult && (
          <ResultBox success>
            <p className="font-medium">{t('maintenance.searchIndexing.reindexFtsSuccess')}</p>
            <p>{t('maintenance.searchIndexing.updatedCount')}: {ftsResult.updated_count ?? ftsResult.updated ?? '—'}</p>
            {ftsResult.fts_config && (
              <p>{t('maintenance.searchIndexing.ftsConfig')}: {ftsResult.fts_config}</p>
            )}
          </ResultBox>
        )}
        {ftsError && <ResultBox success={false}>{ftsError}</ResultBox>}

        <div className="border-t border-gray-200 dark:border-gray-700 my-2" />

        {/* Refresh HA Keywords */}
        <ActionRow
          title={t('maintenance.searchIndexing.refreshKeywords')}
          description={t('maintenance.searchIndexing.refreshKeywordsDescription')}
          buttonLabel={t('maintenance.searchIndexing.refreshKeywords')}
          loading={kwLoading}
          onAction={handleRefreshKeywords}
        />
        {kwResult && (
          <ResultBox success>
            <p className="font-medium">{t('maintenance.searchIndexing.refreshKeywordsSuccess')}</p>
            <p>{t('maintenance.searchIndexing.keywordsCount')}: {kwResult.keywords_count ?? kwResult.count ?? '—'}</p>
            {kwResult.sample && (
              <p>{t('maintenance.searchIndexing.sampleKeywords')}: {
                Array.isArray(kwResult.sample) ? kwResult.sample.join(', ') : String(kwResult.sample)
              }</p>
            )}
          </ResultBox>
        )}
        {kwError && <ResultBox success={false}>{kwError}</ResultBox>}
      </div>

      {/* Section 2: Embeddings */}
      <div className="card mb-6">
        <div className="flex items-center gap-2 mb-4">
          <Database className="w-6 h-6 text-blue-500" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('maintenance.embeddings.title')}
          </h2>
        </div>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          {t('maintenance.embeddings.description')}
        </p>

        {/* Warning */}
        <div className="mb-4 p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg text-sm text-amber-700 dark:text-amber-400">
          {t('maintenance.embeddings.reembedWarning')}
        </div>

        <ActionRow
          title={t('maintenance.embeddings.reembedAll')}
          description={t('maintenance.embeddings.description')}
          buttonLabel={t('maintenance.embeddings.reembedAll')}
          loading={embedLoading}
          onAction={handleReembed}
          variant="warning"
        />
        {embedResult && (
          <ResultBox success>
            <p className="font-medium">{t('maintenance.embeddings.reembedSuccess')}</p>
            {embedResult.model && (
              <p>{t('maintenance.embeddings.reembedModel')}: {embedResult.model}</p>
            )}
            {embedResult.counts && typeof embedResult.counts === 'object' && (
              <div className="mt-1">
                {Object.entries(embedResult.counts).map(([table, count]) => (
                  <p key={table}>{t('maintenance.embeddings.reembedTable')}: {table} — {count}</p>
                ))}
              </div>
            )}
            {embedResult.errors && embedResult.errors.length > 0 && (
              <div className="mt-1 text-amber-700 dark:text-amber-400">
                <p>{t('maintenance.embeddings.reembedErrors')}:</p>
                {embedResult.errors.map((err, i) => (
                  <p key={i} className="ml-2">- {err}</p>
                ))}
              </div>
            )}
          </ResultBox>
        )}
        {embedError && <ResultBox success={false}>{embedError}</ResultBox>}
      </div>

      {/* Section 3: Debug */}
      <div className="card">
        <div className="flex items-center gap-2 mb-4">
          <Bug className="w-6 h-6 text-blue-500" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('maintenance.debug.title')}
          </h2>
        </div>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          {t('maintenance.debug.description')}
        </p>

        {/* Intent Test */}
        <div className="mb-4">
          <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-1">
            {t('maintenance.debug.testIntent')}
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-3">
            {t('maintenance.debug.testIntentDescription')}
          </p>
          <div className="flex gap-2">
            <input
              type="text"
              value={intentMessage}
              onChange={(e) => setIntentMessage(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleTestIntent()}
              placeholder={t('maintenance.debug.messagePlaceholder')}
              className="input flex-1"
            />
            <button
              onClick={handleTestIntent}
              disabled={intentLoading || !intentMessage.trim()}
              className="btn btn-primary flex items-center gap-2 shrink-0"
            >
              {intentLoading ? (
                <Loader className="w-4 h-4 animate-spin" />
              ) : (
                <Bug className="w-4 h-4" />
              )}
              {t('maintenance.debug.testIntent')}
            </button>
          </div>
        </div>

        {intentResult && (
          <ResultBox success>
            <p className="font-medium mb-1">{t('maintenance.debug.extractedIntent')}</p>
            <pre className="text-xs bg-gray-100 dark:bg-gray-800 p-3 rounded overflow-x-auto whitespace-pre-wrap">
              {JSON.stringify(intentResult, null, 2)}
            </pre>
          </ResultBox>
        )}
        {intentError && <ResultBox success={false}>{intentError}</ResultBox>}
      </div>
    </div>
  );
}
