/**
 * Admin Maintenance Page
 *
 * Admin page for system maintenance operations:
 * - FTS reindex, HA keyword refresh
 * - Re-embed all vectors
 * - Intent debugging
 */
import { useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';
import {
  Wrench, Search, Database, Bug, Loader, AlertCircle, CheckCircle,
} from 'lucide-react';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import {
  useReindexFts,
  useRefreshKeywords,
  useReembedAll,
  useTestIntent,
  type FtsResult,
  type KwResult,
  type EmbedResult,
  type IntentResult,
} from '../api/resources/maintenance';

interface ActionRowProps {
  title: string;
  description: string;
  buttonLabel: string;
  icon?: LucideIcon;
  loading: boolean;
  onAction: () => void | Promise<void>;
  variant?: 'warning' | 'primary';
}

function ActionRow({ title, description, buttonLabel, icon: Icon, loading, onAction, variant }: ActionRowProps) {
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

interface ResultBoxProps {
  success: boolean;
  children?: ReactNode;
}

function ResultBox({ success, children }: ResultBoxProps) {
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

  const reindex = useReindexFts();
  const refreshKw = useRefreshKeywords();
  const reembed = useReembedAll();
  const intent = useTestIntent();

  const [ftsResult, setFtsResult] = useState<FtsResult | null>(null);
  const [kwResult, setKwResult] = useState<KwResult | null>(null);
  const [embedResult, setEmbedResult] = useState<EmbedResult | null>(null);
  const [intentResult, setIntentResult] = useState<IntentResult | null>(null);

  const [intentMessage, setIntentMessage] = useState('');

  const handleReindexFts = async () => {
    setFtsResult(null);
    try {
      const data = await reindex.mutateAsync(undefined);
      setFtsResult(data);
    } catch {
      // errorMessage surfaces via reindex.errorMessage
    }
  };

  const handleRefreshKeywords = async () => {
    setKwResult(null);
    try {
      const data = await refreshKw.mutateAsync(undefined);
      setKwResult(data);
    } catch {
      // errorMessage surfaces via refreshKw.errorMessage
    }
  };

  const handleReembed = async () => {
    if (!window.confirm(t('maintenance.embeddings.confirmReembed'))) return;
    setEmbedResult(null);
    try {
      const data = await reembed.mutateAsync(undefined);
      setEmbedResult(data);
    } catch {
      // errorMessage surfaces via reembed.errorMessage
    }
  };

  const handleTestIntent = async () => {
    if (!intentMessage.trim()) return;
    setIntentResult(null);
    try {
      const data = await intent.mutateAsync(intentMessage.trim());
      setIntentResult(data);
    } catch {
      // errorMessage surfaces via intent.errorMessage
    }
  };

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="mb-6">
        <PageHeader icon={Wrench} title={t('maintenance.title')} subtitle={t('maintenance.subtitle')} />
      </div>

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

        <ActionRow
          title={t('maintenance.searchIndexing.reindexFts')}
          description={t('maintenance.searchIndexing.reindexFtsDescription')}
          buttonLabel={t('maintenance.searchIndexing.reindexFts')}
          loading={reindex.isPending}
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
        {reindex.errorMessage && <ResultBox success={false}>{reindex.errorMessage}</ResultBox>}

        <div className="border-t border-gray-200 dark:border-gray-700 my-2" />

        <ActionRow
          title={t('maintenance.searchIndexing.refreshKeywords')}
          description={t('maintenance.searchIndexing.refreshKeywordsDescription')}
          buttonLabel={t('maintenance.searchIndexing.refreshKeywords')}
          loading={refreshKw.isPending}
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
        {refreshKw.errorMessage && <ResultBox success={false}>{refreshKw.errorMessage}</ResultBox>}
      </div>

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

        <Alert variant="warning" className="mb-4">
          {t('maintenance.embeddings.reembedWarning')}
        </Alert>

        <ActionRow
          title={t('maintenance.embeddings.reembedAll')}
          description={t('maintenance.embeddings.description')}
          buttonLabel={t('maintenance.embeddings.reembedAll')}
          loading={reembed.isPending}
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
        {reembed.errorMessage && <ResultBox success={false}>{reembed.errorMessage}</ResultBox>}
      </div>

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
              disabled={intent.isPending || !intentMessage.trim()}
              className="btn btn-primary flex items-center gap-2 shrink-0"
            >
              {intent.isPending ? (
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
        {intent.errorMessage && <ResultBox success={false}>{intent.errorMessage}</ResultBox>}
      </div>
    </div>
  );
}
