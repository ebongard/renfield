import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, FileStack, Merge, Scissors } from 'lucide-react';
import Badge from './Badge';
import {
  fetchProposalPageBlob,
  usePdfSplitProposalsQuery,
  usePdfSplitProposalDetailQuery,
  useApprovePdfSplitProposal,
  useRejectPdfSplitProposal,
  type PdfSplitPiece,
  type PdfSplitProposal,
} from '../api/resources/pdfSplit';

/**
 * "PDF-Aufteilung prüfen" section on /brain/review (PDF-split PR2).
 *
 * An uncertain multi-document boundary proposal is decided here: the owner
 * approves the proposed page ranges (optionally after editing them) or rejects
 * to ingest the file as ONE document. Range editing keeps contiguity BY
 * CONSTRUCTION — the only operations are "merge with next piece" and "add a
 * boundary at page N" — so an invalid (gappy/overlapping) plan cannot be
 * built in the UI; the server re-validates anyway.
 *
 * Renders nothing when there are no pending proposals (mirrors
 * MergeProposalsSection so /brain/review stays uncluttered).
 */
export default function PdfSplitReviewSection({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation();
  const query = usePdfSplitProposalsQuery(enabled);
  const proposals: PdfSplitProposal[] = query.data ?? [];

  if (!enabled || proposals.length === 0) {
    return null;
  }

  return (
    <section aria-labelledby="pdf-split-heading" className="space-y-3">
      <h2
        id="pdf-split-heading"
        className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300"
      >
        <FileStack className="w-4 h-4" aria-hidden="true" />
        {t('pdfSplit.sectionTitle')}
      </h2>
      <ul className="space-y-3 animate-stagger">
        {proposals.map((p) => (
          <PdfSplitProposalCard key={p.id} proposal={p} />
        ))}
      </ul>
    </section>
  );
}

function PdfSplitProposalCard({ proposal }: { proposal: PdfSplitProposal }) {
  const { t } = useTranslation();
  const approve = useApprovePdfSplitProposal();
  const reject = useRejectPdfSplitProposal();

  const [pieces, setPieces] = useState<PdfSplitPiece[]>(proposal.documents);
  const [dirty, setDirty] = useState(false);

  // The backend REFRESHES a pending proposal in place (same row id, new
  // ranges) when the parent is re-detected; react-query refetches but the
  // mounted card would keep editing the stale plan. While the owner has not
  // edited anything, mirror the latest server plan; once dirty, keep the
  // edits (the server re-validates against the current page_count anyway).
  useEffect(() => {
    if (!dirty) {
      setPieces(proposal.documents);
    }
  }, [proposal.documents, dirty]);
  const [showEvidence, setShowEvidence] = useState(false);
  const [boundaryInput, setBoundaryInput] = useState('');

  const confidencePct = Math.round(proposal.overall_confidence * 100);
  const busy = approve.isPending || reject.isPending;

  const mergeWithNext = useCallback((index: number) => {
    setPieces((prev) => {
      if (index >= prev.length - 1) return prev;
      const merged: PdfSplitPiece = {
        ...prev[index],
        end_page: prev[index + 1].end_page,
        // keep the first piece's title — the head page carries the letterhead
      };
      return [...prev.slice(0, index), merged, ...prev.slice(index + 2)];
    });
    setDirty(true);
  }, []);

  const addBoundary = useCallback(() => {
    const page = Number.parseInt(boundaryInput, 10);
    if (!Number.isFinite(page) || page < 2 || page > proposal.page_count) return;
    setPieces((prev) => {
      const idx = prev.findIndex((p) => p.start_page < page && page <= p.end_page);
      if (idx < 0) return prev; // page already starts a piece
      const target = prev[idx];
      const head: PdfSplitPiece = { ...target, end_page: page - 1 };
      const tail: PdfSplitPiece = {
        ...target,
        start_page: page,
        title: '',
        confidence: 0,
      };
      return [...prev.slice(0, idx), head, tail, ...prev.slice(idx + 1)];
    });
    setDirty(true);
    setBoundaryInput('');
  }, [boundaryInput, proposal.page_count]);

  const setTitle = useCallback((index: number, title: string) => {
    setPieces((prev) =>
      prev.map((p, i) => (i === index ? { ...p, title } : p)),
    );
    setDirty(true);
  }, []);

  const handleApprove = useCallback(() => {
    approve.mutate({ id: proposal.id, documents: dirty ? pieces : undefined });
  }, [approve, proposal.id, dirty, pieces]);

  const handleReject = useCallback(() => {
    reject.mutate(proposal.id);
  }, [reject, proposal.id]);

  return (
    <li className="card p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-gray-900 dark:text-gray-100 truncate">
            {proposal.document_filename}
          </p>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {t('pdfSplit.summary', {
              pages: proposal.page_count,
              pieces: pieces.length,
            })}
          </p>
        </div>
        <Badge color={confidencePct >= 70 ? 'amber' : 'red'}>
          {t('pdfSplit.confidence', { pct: confidencePct })}
        </Badge>
      </div>

      <ul className="space-y-2">
        {pieces.map((piece, i) => (
          <li
            key={`${piece.start_page}-${piece.end_page}`}
            className="flex items-center gap-2 rounded-lg bg-gray-50 dark:bg-gray-800/60 px-3 py-2"
          >
            <span className="shrink-0 text-xs font-mono text-gray-600 dark:text-gray-300 w-20">
              {piece.start_page === piece.end_page
                ? t('pdfSplit.pageOne', { page: piece.start_page })
                : t('pdfSplit.pageRange', {
                    from: piece.start_page,
                    to: piece.end_page,
                  })}
            </span>
            <input
              type="text"
              className="input flex-1 min-w-0 text-sm py-1"
              value={piece.title}
              placeholder={t('pdfSplit.titlePlaceholder')}
              onChange={(e) => setTitle(i, e.target.value)}
              disabled={busy}
              aria-label={t('pdfSplit.titleAria', { index: i + 1 })}
            />
            {piece.doc_type && (
              <Badge color="blue">{piece.doc_type}</Badge>
            )}
            {i < pieces.length - 1 && (
              <button
                type="button"
                className="btn btn-ghost p-1"
                title={t('pdfSplit.mergeWithNext')}
                onClick={() => mergeWithNext(i)}
                disabled={busy}
              >
                <Merge className="w-4 h-4" aria-hidden="true" />
                <span className="sr-only">{t('pdfSplit.mergeWithNext')}</span>
              </button>
            )}
          </li>
        ))}
      </ul>

      <div className="flex items-center gap-2 text-sm">
        <Scissors className="w-4 h-4 text-gray-400" aria-hidden="true" />
        <label className="text-gray-600 dark:text-gray-300" htmlFor={`boundary-${proposal.id}`}>
          {t('pdfSplit.addBoundaryLabel')}
        </label>
        <input
          id={`boundary-${proposal.id}`}
          type="number"
          min={2}
          max={proposal.page_count}
          className="input w-20 py-1 text-sm"
          value={boundaryInput}
          onChange={(e) => setBoundaryInput(e.target.value)}
          disabled={busy}
        />
        <button
          type="button"
          className="btn btn-secondary text-sm"
          onClick={addBoundary}
          disabled={busy || boundaryInput === ''}
        >
          {t('pdfSplit.addBoundary')}
        </button>
      </div>

      <button
        type="button"
        className="flex items-center gap-1 text-sm text-gray-600 dark:text-gray-300"
        onClick={() => setShowEvidence((v) => !v)}
        aria-expanded={showEvidence}
      >
        {showEvidence ? (
          <ChevronDown className="w-4 h-4" aria-hidden="true" />
        ) : (
          <ChevronRight className="w-4 h-4" aria-hidden="true" />
        )}
        {t('pdfSplit.showEvidence')}
      </button>
      {showEvidence && <ProposalEvidence proposalId={proposal.id} />}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <button
          type="button"
          className="btn btn-primary text-sm"
          onClick={handleApprove}
          disabled={busy || pieces.length < 2}
        >
          {dirty ? t('pdfSplit.approveEdited') : t('pdfSplit.approve')}
        </button>
        <button
          type="button"
          className="btn btn-secondary text-sm"
          onClick={handleReject}
          disabled={busy}
        >
          {t('pdfSplit.treatAsSingle')}
        </button>
        {pieces.length < 2 && (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {t('pdfSplit.needTwoPieces')}
          </span>
        )}
        {(approve.isError || reject.isError) && (
          <span className="text-xs text-red-600 dark:text-red-400" role="alert">
            {approve.errorMessage ?? reject.errorMessage}
          </span>
        )}
      </div>
    </li>
  );
}

/** Per-page evidence: text snippets from detection + lazy page thumbnails. */
function ProposalEvidence({ proposalId }: { proposalId: number }) {
  const { t } = useTranslation();
  const detail = usePdfSplitProposalDetailQuery(proposalId);
  const signals = detail.data?.page_signals ?? [];
  // Thumbnails are opt-in: each one is an authenticated render request that
  // opens the parent PDF server-side — auto-firing one per page would storm
  // the backend on a large scan. Snippets alone are usually decisive.
  const [thumbsOn, setThumbsOn] = useState(false);

  if (detail.isLoading) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('common.loading')}
      </p>
    );
  }
  if (signals.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('pdfSplit.noEvidence')}
      </p>
    );
  }
  return (
    <div className="space-y-2">
      {!thumbsOn && (
        <button
          type="button"
          className="btn btn-secondary text-sm"
          onClick={() => setThumbsOn(true)}
        >
          {t('pdfSplit.loadThumbs')}
        </button>
      )}
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {signals.map((s) => (
          <li
            key={s.page}
            className="rounded-lg border border-gray-200 dark:border-gray-700 p-2 space-y-1"
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-gray-700 dark:text-gray-200">
                {t('pdfSplit.pageOne', { page: s.page })}
              </span>
              {!s.quality_ok && (
                <Badge color="red">{t('pdfSplit.unreadable')}</Badge>
              )}
            </div>
            {thumbsOn && <PageThumb proposalId={proposalId} page={s.page} />}
            <p className="text-xs text-gray-600 dark:text-gray-300 line-clamp-3 break-words">
              {s.snippet}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Authenticated thumbnail (the JWT rides the axios blob fetch, not an <img
 *  src>); the object URL is revoked on unmount. */
function PageThumb({ proposalId, page }: { proposalId: number; page: number }) {
  const { t } = useTranslation();
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    fetchProposalPageBlob(proposalId, page)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [proposalId, page]);

  if (failed) return null;
  if (!url) {
    return (
      <div className="h-32 rounded bg-gray-100 dark:bg-gray-800 animate-pulse" />
    );
  }
  return (
    <img
      src={url}
      alt={t('pdfSplit.pageThumbAlt', { page })}
      className="max-h-48 w-full rounded object-contain bg-white"
      loading="lazy"
    />
  );
}
