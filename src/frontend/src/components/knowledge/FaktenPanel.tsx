import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { ChevronDown, ChevronUp, Loader2 } from 'lucide-react';

import Alert from '../Alert';
import FactProvenance from '../FactProvenance';
import ObligationRow from '../ObligationRow';
import TierBadge from '../TierBadge';
import {
  useFactsForDocumentQuery,
  useFeatureFlags,
  type DocumentFact,
} from '../../api/resources/brain';
import type { DocStatus } from '../../api/resources/knowledge';

/**
 * Per-document "Fakten" card (Surface 1). Inline-expand inside the /knowledge
 * document card. Controlled open state so the agenda deep-link (?doc=&#fakten)
 * can auto-expand it (D3/D6/T6). Facts fetch lazily — only while open (D-IA-1),
 * so the list view never fans out a fetch per document.
 *
 * Empty states (D-STATE-1 / D11):
 *   still processing            → "Fakten werden extrahiert …"
 *   completed, 0 facts, flag on → "Keine Fakten gefunden."
 *   extraction flag OFF         → "Fakten-Extraktion ist deaktiviert."
 */
interface FaktenPanelProps {
  documentId: number;
  status: DocStatus;
  open: boolean;
  onToggle: () => void;
}

// Render order; obligations last (the action-bearing group). Empty groups are
// skipped — the absence is the signal (D-STATE-3), no empty headers.
const GROUP_ORDER: DocumentFact['category'][] = ['universal', 'identifier', 'obligation'];

function humanizeKind(kind: string): string {
  return kind.charAt(0).toUpperCase() + kind.slice(1).replace(/_/g, ' ');
}

export default function FaktenPanel({ documentId, status, open, onToggle }: FaktenPanelProps) {
  const { t } = useTranslation();
  const now = new Date();

  const factsQuery = useFactsForDocumentQuery(documentId, open);
  const flagsQuery = useFeatureFlags();
  const facts = factsQuery.data ?? [];
  const panelId = `fakten-${documentId}`;

  const grouped = GROUP_ORDER.map((category) => ({
    category,
    items: facts.filter((f) => f.category === category),
  })).filter((g) => g.items.length > 0);

  const extractionDisabled = flagsQuery.data?.schicht_a_extraction_enabled === false;
  const stillProcessing = status === 'pending' || status === 'processing';

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={panelId}
        className="inline-flex items-center gap-1 text-xs font-medium text-primary-600 dark:text-primary-400 hover:underline min-h-[44px] sm:min-h-0"
      >
        {open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        {t('knowledge.facts.toggle')}
      </button>

      {open && (
        <div
          id={panelId}
          className="mt-2 pt-3 border-t border-gray-200 dark:border-gray-700 animate-fade-slide-in"
        >
          {stillProcessing ? (
            <p className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
              <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" />
              {t('knowledge.facts.extracting')}
            </p>
          ) : factsQuery.isLoading ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">{t('common.loading')}</p>
          ) : factsQuery.errorMessage ? (
            <Alert variant="error">{factsQuery.errorMessage}</Alert>
          ) : facts.length === 0 ? (
            extractionDisabled ? (
              <div>
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  {t('knowledge.facts.disabled')}
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                  {t('knowledge.facts.disabledHint')}
                </p>
              </div>
            ) : (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {t('knowledge.facts.empty')}
              </p>
            )
          ) : (
            <div className="space-y-4">
              {grouped.map(({ category, items }) => (
                <div key={category} className="fact-group">
                  <span className="fact-group-label">
                    {t(`knowledge.facts.group.${category}`, { defaultValue: category })}
                  </span>
                  {items.map((fact) =>
                    category === 'obligation' ? (
                      <Link
                        key={fact.id}
                        to={`/brain/fristen#frist-${fact.id}`}
                        className="block rounded-sm hover:bg-gray-50 dark:hover:bg-gray-700/40 px-1 -mx-1"
                      >
                        <ObligationRow fact={fact} now={now} />
                      </Link>
                    ) : (
                      <div
                        key={fact.id}
                        className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-sm"
                      >
                        <span className="text-gray-500 dark:text-gray-400 min-w-[7rem]">
                          {humanizeKind(fact.kind)}
                        </span>
                        <span className="font-medium text-gray-900 dark:text-white break-all">
                          {fact.value}
                        </span>
                        <FactProvenance
                          source={fact.source}
                          confidence={fact.confidence}
                          className="ml-auto"
                        />
                        <TierBadge tier={fact.circle_tier} />
                        {fact.tier_overridden && (
                          <span
                            className="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500"
                            title={t('circles.tierOverridden')}
                          >
                            {t('circles.tierOverriddenShort')}
                          </span>
                        )}
                      </div>
                    ),
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
