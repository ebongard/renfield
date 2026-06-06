import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { X, ArrowRight } from 'lucide-react';
import type { AtomMatch, DocumentFact, FactSource } from '../../api/resources/brain';
import { usePatchAtomTier, useResetFactTier } from '../../api/resources/brain';
import { useUpdateKgEntityTier } from '../../api/resources/knowledgeGraph';
import { useMemoriesBySubjectQuery } from '../../api/resources/memories';
import TierPicker from '../TierPicker';
import type { CircleTier } from '../TierBadge';
import FaktenPanel from '../knowledge/FaktenPanel';
import ObligationRow from '../ObligationRow';
import FactProvenance from '../FactProvenance';

interface WissenDetailDrawerProps {
  atom: AtomMatch | null;
  onClose: () => void;
}

const FOCUSABLE =
  'a[href],button:not([disabled]),input,select,textarea,[tabindex]:not([tabindex="-1"])';

function num(v: unknown): number | null {
  return typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : null;
}
function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v);
}

/** Build a DocumentFact from a document_fact atom payload for ObligationRow. */
function factFromPayload(p: Record<string, unknown>, tier: number): DocumentFact {
  return {
    id: num(p.fact_id) ?? 0,
    document_id: num(p.document_id) ?? 0,
    atom_id: null,
    category: str(p.category) || 'obligation',
    kind: str(p.kind),
    value: str(p.value),
    normalized_value: (p.normalized_value as string) ?? null,
    excerpt: (p.excerpt as string) ?? null,
    obligation_date: (p.obligation_date as string) ?? null,
    amount_value: num(p.amount_value),
    amount_currency: (p.amount_currency as string) ?? null,
    legal_gate: Boolean(p.legal_gate),
    payment_method: (p.payment_method as string) ?? null,
    confidence: num(p.confidence),
    source: (p.source as FactSource) ?? null,
    circle_tier: tier,
  };
}

/**
 * Universal atom detail drawer (PR3). A right slide-over, opened from any
 * search result / lens row, that renders per-type content from the atom's
 * payload (already on the wire) and lets the owner re-tier it. Lives in
 * WissenLayout so it persists across lens switches (D8). Modeled on
 * WissensbasisSidePanel: backdrop, Esc, scroll-lock, focus trap + restore.
 *
 * Tier edit spans TWO id-spaces (the critical branch): atom-backed types
 * (kb_document / document_fact / conversation_memory) patch by atom UUID via
 * usePatchAtomTier; kg_node patches by KG integer id via useUpdateKgEntityTier
 * (its atom_id is the synthetic "kg_node:<id>", which the atom endpoint can't
 * resolve). kg_edge has no tier control.
 */
export default function WissenDetailDrawer({ atom, onClose }: WissenDetailDrawerProps) {
  const { t } = useTranslation();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const patchAtomTier = usePatchAtomTier();
  const updateKgTier = useUpdateKgEntityTier();
  const resetFactTier = useResetFactTier();

  const open = atom !== null;

  // Scroll-lock + Esc + focus management while open.
  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    document.body.style.overflow = 'hidden';
    closeRef.current?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === 'Tab' && panelRef.current) {
        const nodes = panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE);
        if (nodes.length === 0) return;
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = '';
      restoreRef.current?.focus();
    };
  }, [open, onClose]);

  if (!atom) return null;

  const { atom_type, payload = {}, tier } = atom.atom;
  const currentTier = (typeof tier === 'number' ? tier : 0) as CircleTier;

  const handleTier = (next: CircleTier) => {
    if (atom_type === 'kg_node') {
      const id = num(payload.entity_id);
      if (id != null) updateKgTier.mutate({ id, circleTier: next });
    } else if (atom_type === 'kg_edge') {
      // relations have no tier control
    } else {
      patchAtomTier.mutate({ atomId: atom.atom.atom_id, policy: { tier: next } });
    }
  };

  const tierEditable = atom_type !== 'kg_edge';

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/40 transition-opacity motion-reduce:transition-none"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={t('lens.detail.title')}
        className="fixed right-0 top-0 z-50 h-full w-full sm:w-[28rem] bg-white dark:bg-gray-800 shadow-xl overflow-y-auto p-6 space-y-4 motion-safe:animate-slide-in-right"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-2xl font-display text-gray-900 dark:text-white">
            {t('lens.detail.title')}
          </h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label={t('common.close')}
            className="min-w-11 min-h-11 flex items-center justify-center rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          >
            <X className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>

        {/* Per-type content */}
        {atom_type === 'kb_document' && (
          <DocContent payload={payload} />
        )}
        {atom_type === 'document_fact' && (
          <div className="space-y-2">
            <ObligationRow fact={factFromPayload(payload, currentTier)} now={new Date()} />
            <FactProvenance source={(payload.source as FactSource) ?? null} confidence={num(payload.confidence)} />
            {num(payload.document_id) != null && (
              <LensLink to={`/wissen/dokumente?doc=${num(payload.document_id)}`} label={t('lens.detail.openInDocs')} />
            )}
          </div>
        )}
        {atom_type === 'conversation_memory' && (
          <div className="space-y-1">
            <p className="text-gray-700 dark:text-gray-200">{str(payload.content) || atom.snippet}</p>
            {payload.subject_name != null && (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {t('memory.subjectLabel', { name: str(payload.subject_name) })}
              </p>
            )}
            {payload.category != null && (
              <p className="text-xs text-gray-500 dark:text-gray-400">{str(payload.category)}</p>
            )}
          </div>
        )}
        {atom_type === 'kg_node' && (
          <div className="space-y-1">
            <p className="text-lg text-gray-900 dark:text-white">{str(payload.name) || atom.snippet}</p>
            {payload.entity_type != null && (
              <p className="text-xs text-gray-500 dark:text-gray-400">{str(payload.entity_type)}</p>
            )}
            {num(payload.entity_id) != null && (
              <LensLink to={`/wissen/graph?focus=${num(payload.entity_id)}`} label={t('lens.detail.openInGraph')} />
            )}
            {num(payload.entity_id) != null && (
              <EntityMemories entityId={num(payload.entity_id) as number} />
            )}
          </div>
        )}
        {atom_type === 'kg_edge' && (
          <div className="space-y-1">
            <p className="text-gray-700 dark:text-gray-200">
              {str(payload.subject_name)} <span className="text-gray-400">{str(payload.predicate)}</span> {str(payload.object_name)}
            </p>
            <LensLink to="/wissen/graph" label={t('lens.detail.openInGraph')} />
          </div>
        )}

        {/* Tier control */}
        {tierEditable && (
          <div className="pt-2 border-t border-gray-100 dark:border-gray-700/60">
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{t('lens.detail.tier')}</p>
            <TierPicker value={currentTier} onChange={handleTier} variant="compact" />
            {atom_type === 'document_fact' && Boolean(payload.tier_overridden) && num(payload.fact_id) != null && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {t('circles.tierOverridden')}{' '}
                <button
                  type="button"
                  className="underline hover:text-primary-600 dark:hover:text-primary-400"
                  onClick={() => resetFactTier.mutate(num(payload.fact_id) as number)}
                >
                  {t('circles.resetTier')}
                </button>
              </p>
            )}
          </div>
        )}
      </div>
    </>
  );
}

function DocContent({ payload }: { payload: Record<string, unknown> }) {
  const title = str(payload.document_title) || str(payload.document_filename);
  const docId = num(payload.document_id);
  const { t } = useTranslation();
  // Real toggle so the FaktenPanel's expander isn't a dead control for screen
  // readers; defaults open (the drawer's purpose is to show the facts).
  const [factsOpen, setFactsOpen] = useState(true);
  return (
    <div className="space-y-2">
      {title && <p className="text-lg text-gray-900 dark:text-white break-words">{title}</p>}
      {docId != null && (
        <>
          <FaktenPanel
            documentId={docId}
            status="completed"
            open={factsOpen}
            onToggle={() => setFactsOpen((o) => !o)}
          />
          <LensLink to={`/wissen/dokumente?doc=${docId}`} label={t('lens.detail.openInDocs')} />
        </>
      )}
    </div>
  );
}

/** Phase 3c: "Erinnerungen über diesen Knoten" — memories linked to a KG entity. */
function EntityMemories({ entityId }: { entityId: number }) {
  const { t } = useTranslation();
  const { data, isLoading } = useMemoriesBySubjectQuery(entityId);
  const memories = data?.memories ?? [];
  if (isLoading || memories.length === 0) return null;
  return (
    <div className="pt-2 mt-2 border-t border-gray-100 dark:border-gray-700/60">
      <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
        {t('memory.aboutThisEntity')}
      </p>
      <ul className="space-y-1">
        {memories.map((m) => (
          <li key={m.id} className="text-sm text-gray-700 dark:text-gray-200 line-clamp-2">
            {m.content}
          </li>
        ))}
      </ul>
    </div>
  );
}

function LensLink({ to, label }: { to: string; label: string }) {
  return (
    <Link
      to={to}
      className="text-sm text-primary-600 dark:text-primary-400 inline-flex items-center gap-1 min-h-11"
    >
      {label}
      <ArrowRight className="w-4 h-4" aria-hidden="true" />
    </Link>
  );
}
