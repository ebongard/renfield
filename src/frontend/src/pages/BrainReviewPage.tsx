import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Inbox, Calendar } from 'lucide-react';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import type { BadgeColor } from '../components/Badge';
import TierPicker from '../components/TierPicker';
import type { CircleTier } from '../components/TierBadge';
import {
  useAtomsForReviewQuery,
  usePatchAtomTier,
  type AtomType,
  type ReviewAtom,
} from '../api/resources/brain';
import { extractApiError } from '../utils/axios';

const ATOM_TYPE_COLORS: Record<AtomType, BadgeColor> = {
  kb_document: 'blue',
  kg_node: 'amber',
  kg_edge: 'purple',
  conversation_memory: 'teal',
};

const DAY_OPTIONS = [1, 3, 7, 14, 30];

export default function BrainReviewPage() {
  const { t, i18n } = useTranslation();

  const [days, setDays] = useState(7);
  const reviewQuery = useAtomsForReviewQuery(days);
  const atoms = reviewQuery.data ?? [];

  const patchTier = usePatchAtomTier();

  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [savingIds, setSavingIds] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [success]);

  const queryError = reviewQuery.errorMessage;
  const displayError = error ?? queryError;

  const handleTierChange = async (atom: ReviewAtom, newTier: CircleTier) => {
    if ((atom.tier ?? 0) === newTier) return;
    setSavingIds((prev) => new Set(prev).add(atom.atom_id));
    try {
      await patchTier.mutateAsync({
        atomId: atom.atom_id,
        policy: { ...(atom.policy || {}), tier: newTier },
      });
      setSuccess(t('circles.reviewTierChanged'));
    } catch (err) {
      setError(extractApiError(err, t('circles.couldNotSave')));
    } finally {
      setSavingIds((prev) => {
        const next = new Set(prev);
        next.delete(atom.atom_id);
        return next;
      });
    }
  };

  const formatDate = (iso?: string): string => {
    if (!iso) return '';
    try {
      const locale = i18n.language === 'de' ? 'de-DE' : 'en-US';
      return new Date(iso).toLocaleString(locale, {
        dateStyle: 'medium',
        timeStyle: 'short',
      });
    } catch {
      return iso;
    }
  };

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      <PageHeader
        icon={Inbox}
        title={t('circles.reviewTitle')}
        subtitle={t('circles.reviewSubtitle', { days })}
      />

      {displayError && <Alert variant="error" onClose={() => setError(null)}>{displayError}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

      <div className="flex items-center gap-3">
        <Calendar className="w-4 h-4 text-gray-500" aria-hidden="true" />
        <label htmlFor="days-picker" className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('circles.reviewDaysLabel')}:
        </label>
        <div id="days-picker" className="flex gap-1" role="group">
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDays(d)}
              className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                days === d
                  ? 'bg-primary-600 text-white'
                  : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
              aria-pressed={days === d}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      {reviewQuery.isLoading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </div>
      ) : atoms.length === 0 ? (
        <div className="card text-center py-12">
          <Inbox className="w-12 h-12 mx-auto mb-3 text-gray-300 dark:text-gray-600" aria-hidden="true" />
          <p className="text-gray-500 dark:text-gray-400">{t('circles.reviewEmpty')}</p>
        </div>
      ) : (
        <ul className="space-y-3 animate-stagger">
          {atoms.map((atom) => {
            const tier = atom.tier ?? 0;
            const saving = savingIds.has(atom.atom_id);
            return (
              <li
                key={atom.atom_id}
                className={`atom-row tier-ring-${tier} animate-fade-slide-in flex-col sm:flex-row`}
              >
                <div className="flex-1 min-w-0 space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge color={ATOM_TYPE_COLORS[atom.atom_type] || 'gray'}>
                      {t(`circles.atomType.${atom.atom_type}`, { defaultValue: atom.atom_type })}
                    </Badge>
                    <span className="text-xs text-gray-500 dark:text-gray-400">
                      {t('circles.capturedAt')}: {formatDate(atom.created_at)}
                    </span>
                  </div>
                  {atom.title && (
                    <p className="font-medium text-gray-900 dark:text-white truncate">
                      {atom.title}
                    </p>
                  )}
                  {atom.preview && (
                    <p className="text-sm text-gray-600 dark:text-gray-300 line-clamp-2">
                      {atom.preview}
                    </p>
                  )}
                  <code
                    className="block text-[10px] text-gray-400 dark:text-gray-500 truncate"
                    title={atom.atom_id}
                  >
                    {atom.atom_id.slice(0, 8)}…
                  </code>
                </div>
                <div className="sm:ml-4 sm:flex-shrink-0">
                  <TierPicker
                    value={tier}
                    onChange={(t2) => handleTierChange(atom, t2)}
                    disabled={saving}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
