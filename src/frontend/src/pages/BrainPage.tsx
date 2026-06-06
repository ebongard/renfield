import { useState } from 'react';
import type { FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Brain, Search } from 'lucide-react';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import type { BadgeColor } from '../components/Badge';
import TierBadge from '../components/TierBadge';
import { useAtomSearchQuery, type AtomType } from '../api/resources/brain';

const ATOM_TYPE_COLORS: Record<AtomType, BadgeColor> = {
  kb_document: 'blue',
  kg_node: 'amber',
  kg_edge: 'purple',
  conversation_memory: 'teal',
  document_fact: 'green',
};

export default function BrainPage() {
  const { t } = useTranslation();

  const [query, setQuery] = useState('');
  const [activeQuery, setActiveQuery] = useState('');
  const [factsOnly, setFactsOnly] = useState(false);
  const searchQuery = useAtomSearchQuery(activeQuery);
  const results = searchQuery.data ?? [];
  const searched = activeQuery.trim().length > 0 && !searchQuery.isLoading;
  // Client-side "Fakten" scope: results are mixed-type; this chip narrows to
  // document_fact atoms only (no extra fetch).
  const factCount = results.filter((m) => m.atom.atom_type === 'document_fact').length;
  const displayed = factsOnly
    ? results.filter((m) => m.atom.atom_type === 'document_fact')
    : results;

  const handleSearch = (e?: FormEvent<HTMLFormElement>) => {
    e?.preventDefault?.();
    const q = query.trim();
    if (!q) return;
    setActiveQuery(q);
  };

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6">
      <PageHeader
        icon={Brain}
        title={t('circles.brainTitle')}
        subtitle={t('circles.brainSubtitle')}
      />

      {searchQuery.errorMessage && <Alert variant="error">{searchQuery.errorMessage}</Alert>}

      <form onSubmit={handleSearch} className="flex gap-2">
        <div className="relative flex-1">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
            aria-hidden="true"
          />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('circles.brainSearchPlaceholder')}
            className="input pl-10"
            autoFocus
          />
        </div>
        <button
          type="submit"
          disabled={searchQuery.isLoading || !query.trim()}
          className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50"
        >
          {t('common.search')}
        </button>
      </form>

      {searched && results.length > 0 && (
        <div className="flex flex-wrap gap-2" role="group" aria-label={t('circles.filterLabel')}>
          <button
            type="button"
            onClick={() => setFactsOnly(false)}
            aria-pressed={!factsOnly}
            className={`px-3 py-1 rounded-full text-sm border transition-colors ${
              !factsOnly
                ? 'bg-primary-600 text-white border-primary-600'
                : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'
            }`}
          >
            {t('circles.filterAll')} ({results.length})
          </button>
          <button
            type="button"
            onClick={() => setFactsOnly(true)}
            aria-pressed={factsOnly}
            disabled={factCount === 0}
            className={`px-3 py-1 rounded-full text-sm border transition-colors disabled:opacity-40 ${
              factsOnly
                ? 'bg-primary-600 text-white border-primary-600'
                : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'
            }`}
          >
            {t('circles.filterFactsOnly')} ({factCount})
          </button>
        </div>
      )}

      {searchQuery.isLoading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </div>
      ) : !searched ? (
        <div className="card text-center py-12">
          <Brain className="w-12 h-12 mx-auto mb-3 text-gray-300 dark:text-gray-600" aria-hidden="true" />
          <p className="text-gray-500 dark:text-gray-400">{t('circles.brainEmpty')}</p>
        </div>
      ) : displayed.length === 0 ? (
        <div className="card text-center py-12">
          <p className="text-gray-500 dark:text-gray-400">{t('circles.brainNoMatches')}</p>
        </div>
      ) : (
        <ul className="space-y-3 animate-stagger">
          {displayed.map((match) => {
            const { atom, score, snippet, rank } = match;
            const tier = atom?.tier ?? 0;
            return (
              <li
                key={atom.atom_id}
                className={`atom-row tier-ring-${tier} animate-fade-slide-in`}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-2 mb-2">
                    <Badge color={ATOM_TYPE_COLORS[atom.atom_type] || 'gray'}>
                      {t(`circles.atomType.${atom.atom_type}`, atom.atom_type)}
                    </Badge>
                    <TierBadge tier={tier} />
                    <span className="text-xs text-gray-500 dark:text-gray-400 tabular-nums">
                      #{rank} · {t('circles.score')} {score?.toFixed ? score.toFixed(3) : score}
                    </span>
                  </div>
                  <p className="text-sm text-gray-800 dark:text-gray-200 break-words">
                    {snippet || t('common.noResults')}
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
