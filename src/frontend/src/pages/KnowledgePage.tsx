import { useState, useEffect, useRef } from 'react';
import type { ChangeEvent } from 'react';
import { useSearchParams, Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import type { AxiosError } from 'axios';
import {
  BookOpen,
  Upload,
  Trash2,
  Search,
  FileText,
  FolderOpen,
  Plus,
  RefreshCw,
  CheckCircle,
  Loader,
  Database,
  Layers,
  File,
  ArrowRightLeft,
  Landmark,
} from 'lucide-react';
import apiClient from '../utils/axios';
import { formatDateTime } from '../utils/datetime';
import { extractApiError } from '../utils/axios';
import { useConfirmDialog } from '../components/ConfirmDialog';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import type { AlertVariant } from '../components/Alert';
import Badge from '../components/Badge';
import StatusBadge from '../components/knowledge/StatusBadge';
import FaktenPanel from '../components/knowledge/FaktenPanel';
import DuplicateDialog from '../components/knowledge/DuplicateDialog';
import type { ExistingDocument } from '../components/knowledge/DuplicateDialog';
import { useDocumentPolling } from '../hooks/useDocumentPolling';
import type { KbDocument } from '../hooks/useDocumentPolling';
import type { DocPages } from '../components/knowledge/StatusBadge';
import { useInflightTabTitle } from '../hooks/useInflightTabTitle';
import {
  useKnowledgeDocumentsQuery,
  useKnowledgeBasesQuery,
  useKnowledgeStatsQuery,
  useSearchKnowledge,
  useCreateKnowledgeBase,
  useDeleteKnowledgeBase,
  useDeleteKnowledgeDocument,
  useReindexKnowledgeDocument,
  useMoveKnowledgeDocuments,
  useSetDocumentTier,
  useBulkSetDocumentTier,
  type StatusFilter,
  type SearchResultChunk,
  type DocumentRow,
} from '../api/resources/knowledge';
import { keys } from '../api/keys';
import { useFeatureFlags } from '../api/resources/brain';
import SimbaSendModal from '../components/simba/SimbaSendModal';
import TierControlPopover from '../components/knowledge/TierControlPopover';
import TierChangeToast, { TIER_UNDO_WINDOW_MS } from '../components/knowledge/TierChangeToast';
import type { CircleTier } from '../components/TierBadge';
import { useLensContext } from '../context/LensContext';

interface DuplicateErrorPayload {
  existing_document?: ExistingDocument;
  max_mb?: number;
  allowed?: string[];
  message?: string;
}

// Literal class map so Tailwind's content scanner keeps these utilities — a
// constructed `tier-ring-${n}` would be purged (same reason TierBadge uses a
// literal TIER_CLASS map). The 4px left edge is DESIGN.md's access signal.
const TIER_RING_CLASS: Record<number, string> = {
  0: 'tier-ring-0',
  1: 'tier-ring-1',
  2: 'tier-ring-2',
  3: 'tier-ring-3',
  4: 'tier-ring-4',
};

export default function KnowledgePage() {
  const { t, i18n } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();
  const queryClient = useQueryClient();
  // Embedded as the Documents lens: the workspace omnisearch (?q=) is the single
  // search input (D9). We hide our own input and run the chunk search off ?q=.
  const { embedded } = useLensContext();

  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<
    { text: string; variant: AlertVariant; link?: { to: string; label: string } } | null
  >(null);

  // Filter state
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  // Search state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResultChunk[]>([]);
  // Debounced query that drives the ranked DOCUMENT list (name+facts+content).
  // Set by the debounce effect below from the search box / omni ?q=.
  const [docSearchQuery, setDocSearchQuery] = useState('');

  // New Knowledge Base state
  const [showNewKbModal, setShowNewKbModal] = useState(false);
  const [newKbName, setNewKbName] = useState('');

  // Duplicate-upload dialog (#388)
  const [duplicate, setDuplicate] = useState<ExistingDocument | null>(null);

  // Per-row expansion state for showing raw `error_message` on failed docs.
  const [expandedErrors, setExpandedErrors] = useState<Record<number, boolean>>({});
  // Per-row expansion state for the Fakten panel (controlled so the agenda
  // deep-link can auto-expand it — D3/T6).
  const [expandedFacts, setExpandedFacts] = useState<Record<number, boolean>>({});
  const [newKbDescription, setNewKbDescription] = useState('');

  // Move / Bulk selection state
  const [selectedDocs, setSelectedDocs] = useState<Set<number>>(new Set());
  const [showMoveDropdown, setShowMoveDropdown] = useState<number | 'bulk' | null>(null);

  // Visibility (circle tier) control
  const setDocTier = useSetDocumentTier();
  const bulkSetDocTier = useBulkSetDocumentTier();
  const [tierToast, setTierToast] = useState<{ docId: number; prevTier: CircleTier; newTier: CircleTier } | null>(null);
  const tierToastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (tierToastTimer.current) clearTimeout(tierToastTimer.current);
  }, []);

  // Warning confirm gate for making a document publicly visible (federation
  // peers can then read it) — the one privacy-sensitive transition.
  const confirmPublicTier = () =>
    confirm({
      title: t('knowledge.visibility.makePublicConfirmTitle'),
      message: t('knowledge.visibility.makePublicConfirmBody'),
      confirmLabel: t('knowledge.visibility.makePublicConfirm'),
      variant: 'warning',
    });

  const showTierToast = (docId: number, prevTier: CircleTier, newTier: CircleTier) => {
    if (tierToastTimer.current) clearTimeout(tierToastTimer.current);
    setTierToast({ docId, prevTier, newTier });
    tierToastTimer.current = setTimeout(() => setTierToast(null), TIER_UNDO_WINDOW_MS);
  };

  const handleSetDocTier = (doc: DocumentRow, newTier: CircleTier) => {
    const prevTier = (doc.circle_tier ?? 0) as CircleTier;
    setDocTier
      .mutateAsync({ id: doc.id, tier: newTier })
      .then(() => showTierToast(doc.id, prevTier, newTier))
      .catch(() => {
        /* error surfaced via the mutation's errorMessage */
      });
  };

  const handleUndoTier = () => {
    if (!tierToast) return;
    const { docId, prevTier } = tierToast;
    if (tierToastTimer.current) clearTimeout(tierToastTimer.current);
    setTierToast(null);
    setDocTier.mutate({ id: docId, tier: prevTier });
  };

  const handleBulkTier = (newTier: CircleTier) => {
    if (selectedDocs.size === 0) return;
    bulkSetDocTier.mutate({ ids: [...selectedDocs], tier: newTier });
  };

  const documentsQuery = useKnowledgeDocumentsQuery({
    knowledgeBaseId: selectedKnowledgeBase,
    statusFilter,
    q: docSearchQuery,
  });
  const basesQuery = useKnowledgeBasesQuery();
  const statsQuery = useKnowledgeStatsQuery();
  const documents = documentsQuery.data ?? [];
  const knowledgeBases = basesQuery.data ?? [];
  const stats = statsQuery.data ?? null;
  const loading = documentsQuery.isLoading || basesQuery.isLoading || statsQuery.isLoading;

  const searchMutation = useSearchKnowledge();
  const createKbMutation = useCreateKnowledgeBase();
  const deleteKbMutation = useDeleteKnowledgeBase();
  const deleteDocMutation = useDeleteKnowledgeDocument();
  const reindexDocMutation = useReindexKnowledgeDocument();
  const moveDocsMutation = useMoveKnowledgeDocuments();
  // The doc-page "send to Simba" overlay target ({id, filename}); null = closed.
  const [simbaSendDoc, setSimbaSendDoc] = useState<{ id: number; filename: string } | null>(null);
  const simbaEnabled = useFeatureFlags().data?.simba_ingest_review_enabled ?? false;

  const searching = searchMutation.isPending;

  const refreshAfterUpload = async () => {
    await queryClient.invalidateQueries({ queryKey: keys.knowledge.all });
    // D5: a doc resolving (e.g. after reindex) may have changed its facts —
    // refetch any open Fakten panel. Prefix-match invalidates all facts queries.
    await queryClient.invalidateQueries({ queryKey: ['brain', 'facts'] });
  };

  // Polling for in-flight uploads (#388). Populated from 202 responses
  // below; each poll refreshes the tracked docs, and on terminal state
  // we trigger a list reload so the row updates in place.
  const { activeDocs, track: trackDocument } = useDocumentPolling({
    onResolved: async () => {
      await refreshAfterUpload();
    },
    onTimeout: (doc) => {
      // 30-min cap hit. Tell the user their poll loop gave up, but leave
      // the DB row alone — a manual refresh can still pick it up if the
      // worker does eventually finish.
      setUploadProgress({ text: t('knowledge.pollingTimeout', { filename: doc.filename }), variant: 'warning' });
      setTimeout(() => setUploadProgress(null), 8000);
    },
  });

  // Mutate the tab title while docs are still in flight so the user sees
  // a count even when the tab isn't focused.
  useInflightTabTitle(
    Object.keys(activeDocs).length,
    t('knowledge.title') + ' — Renfield',
  );

  // Last file the user tried to upload, so the 503-toast retry CTA can
  // re-submit without re-opening the file picker.
  const [pendingRetryFile, setPendingRetryFile] = useState<File | null>(null);

  // Core upload — handles both 200 (legacy inline) and 202 (worker-enabled)
  // responses. On 202 we track the doc for polling and surface a "queued,
  // processing soon" toast; on 200 we just reload.
  const uploadFile = async (file: File) => {
    setUploading(true);
    setUploadProgress({ text: t('knowledge.processing', { filename: file.name }), variant: 'info' });
    setPendingRetryFile(file);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const params = selectedKnowledgeBase
        ? { knowledge_base_id: selectedKnowledgeBase }
        : {};

      const response = await apiClient.post<DocumentRow>('/api/knowledge/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        params,
      });

      if (response.status === 202) {
        setUploadProgress({ text: t('knowledge.uploadQueued'), variant: 'success' });
        trackDocument(response.data as KbDocument);
        setPendingRetryFile(null);
        await refreshAfterUpload();
      } else {
        setUploadProgress({ text: t('knowledge.uploadSuccess'), variant: 'success' });
        setPendingRetryFile(null);
        await refreshAfterUpload();
      }

      setTimeout(() => setUploadProgress(null), 2500);
    } catch (error) {
      const axiosErr = error as AxiosError<{ detail?: DuplicateErrorPayload | string }> | undefined;
      const status = axiosErr?.response?.status;
      const detail = axiosErr?.response?.data?.detail;
      const detailObj = typeof detail === 'object' ? detail : undefined;

      if (status === 409 && detailObj?.existing_document) {
        setDuplicate(detailObj.existing_document);
        setUploadProgress(null);
        setPendingRetryFile(null);
      } else if (status === 503) {
        // 503 is transient — keep pendingRetryFile so the retry CTA shows.
        setUploadProgress({ text: t('knowledge.workerUnavailable'), variant: 'error' });
        setTimeout(() => setUploadProgress(null), 10000);
      } else if (status === 413) {
        const maxMb = detailObj?.max_mb ?? '';
        setUploadProgress({ text: t('knowledge.fileTooLarge', { maxMb }), variant: 'error' });
        setPendingRetryFile(null);
        setTimeout(() => setUploadProgress(null), 5000);
      } else if (status === 415) {
        const allowed = Array.isArray(detailObj?.allowed) ? detailObj.allowed.join(', ') : '';
        setUploadProgress({ text: t('knowledge.formatNotSupported', { allowed }), variant: 'error' });
        setPendingRetryFile(null);
        setTimeout(() => setUploadProgress(null), 6000);
      } else {
        console.error('Upload error:', error);
        const msg = typeof detail === 'string' ? detail : (detailObj?.message ?? t('knowledge.serverError'));
        setUploadProgress({ text: `${t('knowledge.errorLabel')}: ${msg}`, variant: 'error' });
        setPendingRetryFile(null);
        setTimeout(() => setUploadProgress(null), 6000);
      }
    } finally {
      setUploading(false);
    }
  };

  // <input type="file"> change handler — extracts the file, dispatches to
  // uploadFile, and resets the input so the same file can be re-selected.
  const handleUploadEvent = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) uploadFile(file);
    e.target.value = '';
  };

  // Retry the last upload that bounced with 503.
  const handleRetryUpload = () => {
    if (pendingRetryFile) {
      const file = pendingRetryFile;
      setPendingRetryFile(null);
      uploadFile(file);
    }
  };

  // Delete document
  const handleDeleteDocument = async (id: number, filename: string) => {
    const confirmed = await confirm({
      title: t('common.delete'),
      message: t('knowledge.deleteDocument', { filename }),
      variant: 'danger',
    });
    if (!confirmed) return;
    try {
      await deleteDocMutation.mutateAsync(id);
      // D5: drop the deleted doc's cached facts.
      await queryClient.invalidateQueries({ queryKey: keys.brain.facts(id) });
    } catch {
      alert(t('knowledge.deleteFailed'));
    }
  };

  // Reindex document — async (#async-reindex): the endpoint enqueues a
  // user_reindex task and returns 202 with the doc in `pending`. Track it for
  // polling so the row updates pending → processing → completed; onResolved
  // reloads the list + invalidates facts so an open Fakten panel refreshes.
  const handleReindexDocument = async (id: number) => {
    try {
      const doc = await reindexDocMutation.mutateAsync(id);
      trackDocument(doc as KbDocument);
      // Drop any stale facts immediately; onResolved refetches when done (D5).
      await queryClient.invalidateQueries({ queryKey: keys.brain.facts(id) });
    } catch {
      alert(t('knowledge.reindexFailed'));
    }
  };

  // Inbound deep link from the obligations agenda: /knowledge?doc={id}#fakten
  // (D3/T6). Reset filters so the target row is in the list regardless of the
  // active KB/status filter, then — once the (refetched) list actually
  // contains it — scroll to it and auto-expand its Fakten panel. Async-race
  // safe: we gate the scroll/expand on the query having resolved with the row
  // present, and fire exactly once via the ref so it can't loop.
  const [searchParams] = useSearchParams();
  const deepLinkDoc = searchParams.get('doc');
  const deepLinkHandled = useRef(false);

  // D9 (embedded): drive the chunk search from the workspace omnisearch `?q=`,
  // debounced. At scope=everything the cross-corpus overlay owns the query, so
  // we clear our inline results and stay out of the way.
  const omniQ = searchParams.get('q') ?? '';
  const omniScope = searchParams.get('scope');
  // Embedded with a non-empty query and not in cross-corpus mode → the
  // Documents lens owns the query and renders inline results.
  const embeddedActiveQuery = embedded && omniScope !== 'everything' && omniQ.trim().length > 0;
  const { mutateAsync: runChunkSearch } = searchMutation;
  useEffect(() => {
    if (!embedded) return;
    const query = omniQ.trim();
    setSearchQuery(query);
    if (omniScope === 'everything' || !query) {
      setSearchResults([]);
      return;
    }
    let cancelled = false; // ignore an out-of-order resolution after a newer query
    const id = setTimeout(() => {
      runChunkSearch({ query, knowledgeBaseId: selectedKnowledgeBase })
        .then((r) => { if (!cancelled) setSearchResults(r); })
        .catch(() => { /* surfaced via searchMutation.errorMessage */ });
    }, 300);
    return () => { cancelled = true; clearTimeout(id); };
  }, [embedded, omniQ, omniScope, selectedKnowledgeBase, runChunkSearch]);

  // Drive the ranked DOCUMENT list from the same query (debounced). searchQuery
  // is the source in both modes (the omni effect above mirrors ?q= into it when
  // embedded). Suppressed only when the lens defers to the cross-corpus overlay
  // (embedded + scope=everything), so the list isn't hijacked there.
  useEffect(() => {
    const suppressed = embedded && omniScope === 'everything';
    const next = suppressed ? '' : searchQuery.trim();
    const id = setTimeout(() => setDocSearchQuery(next), 300);
    return () => clearTimeout(id);
  }, [searchQuery, embedded, omniScope]);

  useEffect(() => {
    // New target id → allow handling again (e.g. agenda → doc A, back, → doc B).
    deepLinkHandled.current = false;
  }, [deepLinkDoc]);

  useEffect(() => {
    if (!deepLinkDoc || deepLinkHandled.current) return;
    const id = Number(deepLinkDoc);
    if (!Number.isInteger(id)) return;
    // Widen the view so a filtered-out target still resolves (D3).
    if (selectedKnowledgeBase !== null) setSelectedKnowledgeBase(null);
    if (statusFilter !== 'all') setStatusFilter('all');
    // Wait for the (possibly refetched) list before touching the DOM.
    if (documentsQuery.isLoading || documentsQuery.isFetching) return;
    if (!documents.some((d) => d.id === id)) return; // not reachable — fail quietly
    deepLinkHandled.current = true;
    setExpandedFacts((prev) => ({ ...prev, [id]: true }));
    requestAnimationFrame(() => {
      document.getElementById(`doc-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }, [
    deepLinkDoc,
    documents,
    documentsQuery.isLoading,
    documentsQuery.isFetching,
    selectedKnowledgeBase,
    statusFilter,
  ]);

  // Search
  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    try {
      const results = await searchMutation.mutateAsync({
        query: searchQuery,
        knowledgeBaseId: selectedKnowledgeBase,
      });
      setSearchResults(results);
    } catch {
      // errorMessage surfaces via searchMutation.errorMessage
    }
  };

  // Create Knowledge Base
  const handleCreateKnowledgeBase = async () => {
    if (!newKbName.trim()) return;
    try {
      await createKbMutation.mutateAsync({
        name: newKbName,
        description: newKbDescription || null,
      });
      setShowNewKbModal(false);
      setNewKbName('');
      setNewKbDescription('');
    } catch (err) {
      alert(extractApiError(err, t('common.error')));
    }
  };

  // Delete Knowledge Base
  const handleDeleteKnowledgeBase = async (id: number, name: string) => {
    const confirmed = await confirm({
      title: t('knowledge.deleteKnowledgeBase'),
      message: t('knowledge.deleteKnowledgeBaseConfirm', { name }),
      variant: 'danger',
    });
    if (!confirmed) return;
    try {
      await deleteKbMutation.mutateAsync(id);
      if (selectedKnowledgeBase === id) setSelectedKnowledgeBase(null);
    } catch {
      alert(t('common.error'));
    }
  };

  // Move documents
  const handleMoveDocuments = async (docIds: number[], targetKbId: number | null) => {
    if (!targetKbId || docIds.length === 0) return;
    try {
      const result = await moveDocsMutation.mutateAsync({ documentIds: docIds, targetKbId });
      const moved = result.moved_count;
      if (moved > 0) {
        setUploadProgress({ text: t('knowledge.documentsMovedSuccess', { count: moved }), variant: 'success' });
      } else {
        setUploadProgress({ text: t('knowledge.alreadyInTargetKb'), variant: 'info' });
      }
      setTimeout(() => setUploadProgress(null), 3000);
      setSelectedDocs(new Set());
      setShowMoveDropdown(null);
    } catch (err) {
      alert(extractApiError(err, t('common.error')));
    }
  };

  // Bulk selection helpers
  const toggleDocSelection = (docId: number) => {
    setSelectedDocs(prev => {
      const next = new Set(prev);
      if (next.has(docId)) next.delete(docId);
      else next.add(docId);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedDocs.size === documents.length) {
      setSelectedDocs(new Set());
    } else {
      setSelectedDocs(new Set(documents.map((d) => d.id)));
    }
  };

  // KB selector dropdown for move
  const MoveKbDropdown = ({ docIds, onClose }: { docIds: number[]; onClose: () => void }) => {
    const targetBases = knowledgeBases.filter((kb) => kb.id !== selectedKnowledgeBase);
    if (targetBases.length === 0) return null;

    return (
      <div className="absolute right-0 top-full mt-1 z-20 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg py-1 min-w-48">
        <div className="px-3 py-1.5 text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('knowledge.selectTargetKb')}
        </div>
        {targetBases.map(kb => (
          <button
            key={kb.id}
            onClick={() => { handleMoveDocuments(docIds, kb.id); onClose(); }}
            className="w-full text-left px-3 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            {kb.name}
          </button>
        ))}
      </div>
    );
  };

  // File type icon helper
  const getFileIcon = (fileType?: string) => {
    switch (fileType) {
      case 'pdf':
        return <FileText className="w-5 h-5 text-red-400" />;
      case 'docx':
      case 'doc':
        return <FileText className="w-5 h-5 text-blue-400" />;
      case 'md':
      case 'txt':
        return <File className="w-5 h-5 text-gray-400" />;
      default:
        return <File className="w-5 h-5 text-gray-400" />;
    }
  };

  const statusFilters: StatusFilter[] = ['all', 'completed', 'processing', 'pending', 'failed'];
  // Split statuses are labelled but not offered as filter chips (statusFilters
  // above) — they are rare parked states surfaced via the card badge instead.
  const statusLabels: Record<StatusFilter, string> = {
    all: t('common.all'),
    completed: t('knowledge.statusCompleted'),
    processing: t('knowledge.statusProcessing'),
    pending: t('knowledge.statusPending'),
    failed: t('knowledge.statusFailed'),
    split_pending: t('knowledge.statusSplitPending'),
    split_review: t('knowledge.statusSplitReview'),
    split_archived: t('knowledge.statusSplitArchived'),
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <PageHeader icon={BookOpen} title={t('knowledge.title')} subtitle={t('knowledge.subtitle')}>
        <button
          onClick={() => setShowNewKbModal(true)}
          className="btn-primary flex items-center gap-2"
        >
          <Plus className="w-4 h-4" />
          {t('knowledge.newKnowledgeBase')}
        </button>
      </PageHeader>

      {/* Statistics */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="card bg-linear-to-br from-blue-100 to-blue-50 dark:from-blue-900/50 dark:to-blue-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-blue-200 dark:bg-blue-600/30 rounded-lg">
                <FileText className="w-6 h-6 text-blue-600 dark:text-blue-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.document_count}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">{t('knowledge.documents')}</div>
              </div>
            </div>
          </div>
          <div className="card bg-linear-to-br from-green-100 to-green-50 dark:from-green-900/50 dark:to-green-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-green-200 dark:bg-green-600/30 rounded-lg">
                <CheckCircle className="w-6 h-6 text-green-600 dark:text-green-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.completed_documents}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">{t('knowledge.indexed')}</div>
              </div>
            </div>
          </div>
          <div className="card bg-linear-to-br from-accent-100 to-accent-50 dark:from-accent-900/50 dark:to-accent-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-accent-200 dark:bg-accent-600/30 rounded-lg">
                <Layers className="w-6 h-6 text-accent-600 dark:text-accent-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.chunk_count}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">{t('knowledge.chunks')}</div>
              </div>
            </div>
          </div>
          <div className="card bg-linear-to-br from-orange-100 to-orange-50 dark:from-orange-900/50 dark:to-orange-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-orange-200 dark:bg-orange-600/30 rounded-lg">
                <Database className="w-6 h-6 text-orange-600 dark:text-orange-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.knowledge_base_count}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">{t('knowledge.knowledgeBases')}</div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Knowledge Bases */}
      {knowledgeBases.length > 0 && (
        <div className="card">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4 flex items-center gap-2">
            <FolderOpen className="w-5 h-5 text-primary-400" />
            {t('knowledge.knowledgeBases')}
          </h2>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => setSelectedKnowledgeBase(null)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                selectedKnowledgeBase === null
                  ? 'bg-primary-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600'
              }`}
            >
              {t('common.all')}
            </button>
            {knowledgeBases.map((kb) => (
              <div key={kb.id} className="flex items-center gap-1">
                <button
                  onClick={() => setSelectedKnowledgeBase(kb.id)}
                  className={`px-4 py-2 rounded-l-lg text-sm font-medium transition-colors ${
                    selectedKnowledgeBase === kb.id
                      ? 'bg-primary-600 text-white'
                      : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600'
                  }`}
                >
                  {kb.name}
                  <span className="ml-2 text-xs opacity-70">
                    ({kb.document_count || 0})
                  </span>
                </button>
                <button
                  onClick={() => handleDeleteKnowledgeBase(kb.id, kb.name)}
                  className="px-2 py-2 rounded-r-lg bg-gray-200 text-gray-500 hover:text-red-500 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-400 dark:hover:text-red-400 dark:hover:bg-gray-600 transition-colors"
                  title={t('knowledge.deleteKnowledgeBase')}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Upload Section */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4 flex items-center gap-2">
          <Upload className="w-5 h-5 text-primary-400" />
          {t('knowledge.uploadDocument')}
        </h2>
        <div className="border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-6 text-center hover:border-primary-500 transition-colors">
          <input
            type="file"
            onChange={handleUploadEvent}
            accept=".pdf,.docx,.doc,.txt,.md,.html,.pptx,.xlsx"
            disabled={uploading}
            className="hidden"
            id="file-upload"
          />
          <label
            htmlFor="file-upload"
            className={`cursor-pointer ${uploading ? 'opacity-50' : ''}`}
          >
            <Upload className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-500 mb-4" />
            <p className="text-gray-700 dark:text-gray-300 mb-2">
              {uploading ? t('knowledge.uploadProcessing') : t('knowledge.uploadDragDrop')}
            </p>
            <p className="text-sm text-gray-500 dark:text-gray-500">
              {t('knowledge.supportedFormats')}
            </p>
          </label>
        </div>
        {uploadProgress && (
          <Alert variant={uploadProgress.variant} className="mt-4">
            <div className="flex items-center justify-between gap-3">
              <span>{uploadProgress.text}</span>
              {pendingRetryFile && uploadProgress.variant === 'error' && (
                <button
                  type="button"
                  onClick={handleRetryUpload}
                  className="btn-secondary !py-1 !px-3 text-sm min-h-[44px] sm:min-h-0"
                >
                  {t('knowledge.retry')}
                </button>
              )}
              {uploadProgress.link && (
                <Link
                  to={uploadProgress.link.to}
                  className="btn-secondary !py-1 !px-3 text-sm min-h-[44px] sm:min-h-0 whitespace-nowrap"
                >
                  {uploadProgress.link.label}
                </Link>
              )}
            </div>
          </Alert>
        )}
      </div>

      {/* Search Section. Embedded as the Wissen Documents lens, the workspace
          omnisearch is the single input (D9) — hide our own box and only show
          the inline results it produces; standalone keeps the full search UI. */}
      {(!embedded || embeddedActiveQuery || searchResults.length > 0) && (
      <div className="card">
        {/* Embedded: the omnisearch drives this — show a loading/empty status
            so a query without (yet) results isn't a silent void. */}
        {embedded && embeddedActiveQuery && searchResults.length === 0 && (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {searching ? t('common.loading') : t('knowledge.noResults', { defaultValue: t('common.noResults', { defaultValue: 'Keine Ergebnisse' }) })}
          </p>
        )}
        {!embedded && (
          <>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4 flex items-center gap-2">
          <Search className="w-5 h-5 text-primary-400" />
          {t('knowledge.searchInDocuments')}
        </h2>
        <div className="flex gap-2">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyPress={(e) => e.key === 'Enter' && handleSearch()}
            placeholder={t('knowledge.searchPlaceholder')}
            className="input flex-1"
          />
          <button
            onClick={handleSearch}
            disabled={searching || !searchQuery.trim()}
            className="btn-primary flex items-center gap-2"
          >
            {searching ? (
              <Loader className="w-4 h-4 animate-spin" />
            ) : (
              <Search className="w-4 h-4" />
            )}
            {t('common.search')}
          </button>
        </div>
          </>
        )}

        {/* Search Results */}
        {searchResults.length > 0 && (
          <div className="mt-4 space-y-3">
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
              {t('knowledge.resultsFound', { count: searchResults.length })}
            </h3>
            {searchResults.map((result, idx) => (
              <div
                key={idx}
                className="p-4 bg-gray-100 dark:bg-gray-700/50 rounded-lg border border-gray-200 dark:border-gray-600"
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                    {getFileIcon(result.document?.filename?.split('.').pop())}
                    <span>{result.document.filename}</span>
                    {result.chunk.page_number && (
                      <span className="text-gray-400 dark:text-gray-500">
                        | {t('knowledge.page')} {result.chunk.page_number}
                      </span>
                    )}
                  </div>
                  <Badge color="accent">
                    {t('knowledge.relevance', { percent: Math.round(result.similarity * 100) })}
                  </Badge>
                </div>
                <p className="text-gray-700 dark:text-gray-300 text-sm line-clamp-3">
                  {result.chunk.content}
                </p>
                {result.chunk.section_title && (
                  <p className="mt-2 text-xs text-gray-500 dark:text-gray-500">
                    {t('knowledge.section')}: {result.chunk.section_title}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
      )}

      {/* Status Filters */}
      <div className="flex space-x-2 overflow-x-auto">
        {statusFilters.map((f) => (
          <button
            key={f}
            onClick={() => setStatusFilter(f)}
            className={`px-4 py-2 rounded-lg whitespace-nowrap transition-colors ${
              statusFilter === f
                ? 'bg-primary-600 text-white'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
            }`}
          >
            {statusLabels[f] || f}
          </button>
        ))}
      </div>

      {/* Bulk Action Toolbar */}
      {selectedDocs.size > 0 && knowledgeBases.length > 0 && (
        <div className="card bg-primary-50 dark:bg-primary-900/20 border-primary-200 dark:border-primary-700">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-primary-700 dark:text-primary-300">
              {selectedDocs.size} {t('knowledge.documents').toLowerCase()}
            </span>
            <div className="flex items-center gap-2">
              <TierControlPopover
                tier={0}
                triggerLabel={t('knowledge.visibility.bulkSet')}
                onChange={handleBulkTier}
                confirmPublic={confirmPublicTier}
                busy={bulkSetDocTier.isPending}
              />
              <div className="relative">
                <button
                  onClick={() => setShowMoveDropdown(showMoveDropdown === 'bulk' ? null : 'bulk')}
                  className="btn-primary flex items-center gap-2 text-sm"
                >
                  <ArrowRightLeft className="w-4 h-4" />
                  {t('knowledge.moveDocuments', { count: selectedDocs.size })}
                </button>
                {showMoveDropdown === 'bulk' && (
                  <MoveKbDropdown
                    docIds={[...selectedDocs]}
                    onClose={() => setShowMoveDropdown(null)}
                  />
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Documents List */}
      {!loading && documents.length > 0 && (
        <p className="text-sm text-gray-500 dark:text-gray-400 -mb-1">
          {t('knowledge.visibility.hint')}
        </p>
      )}
      <div className="space-y-4">
        {loading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('knowledge.loadingDocuments')}</p>
          </div>
        ) : documents.length === 0 ? (
          <div className="card text-center py-12">
            <FileText className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('knowledge.noDocuments')}</p>
            <p className="text-sm text-gray-400 dark:text-gray-500 mt-2">
              {t('knowledge.uploadToFill')}
            </p>
          </div>
        ) : (
          <>
            {/* Select All */}
            {knowledgeBases.length > 0 && documents.length > 1 && (
              <div className="flex items-center gap-2 px-1">
                <input
                  type="checkbox"
                  checked={selectedDocs.size === documents.length}
                  onChange={toggleSelectAll}
                  className="w-4 h-4 rounded border-gray-300 dark:border-gray-600 text-primary-600 focus:ring-primary-500"
                />
                <span className="text-sm text-gray-500 dark:text-gray-400">{t('common.all')}</span>
              </div>
            )}
            {documents.map((staleDoc) => {
              // Overlay live polling data on top of the list-fetched row so
              // stage + queue_position + pages update every 2s without a
              // full list reload. Once the doc resolves (completed/failed)
              // the polling hook drops it from activeDocs and onResolved
              // triggers a list reload — so stale data is replaced with
              // the authoritative row shortly after.
              const liveOverlay = activeDocs[staleDoc.id];
              const doc: DocumentRow = liveOverlay
                ? { ...staleDoc, ...liveOverlay, pages: liveOverlay.pages as DocPages | null | undefined }
                : staleDoc;
              return (
              <div
                key={doc.id}
                id={`doc-${doc.id}`}
                className={`group card transition-shadow ${TIER_RING_CLASS[doc.circle_tier ?? 0] ?? TIER_RING_CLASS[0]}`}
              >
                <div className="flex items-start space-x-4">
                  {knowledgeBases.length > 0 && (
                    <div className="mt-2">
                      <input
                        type="checkbox"
                        checked={selectedDocs.has(doc.id)}
                        onChange={() => toggleDocSelection(doc.id)}
                        className="w-4 h-4 rounded border-gray-300 dark:border-gray-600 text-primary-600 focus:ring-primary-500"
                      />
                    </div>
                  )}
                  <div className="mt-1">{getFileIcon(doc.file_type)}</div>
                  <div className="flex-1 min-w-0">
                    <h3 className="text-base font-semibold text-gray-900 dark:text-white mb-1 truncate">
                      {doc.display_name || doc.title || doc.filename}
                    </h3>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-500 dark:text-gray-400">
                      <span>{t('common.type')}: {doc.file_type?.toUpperCase()}</span>
                      {doc.page_count && <span>{t('knowledge.pages')}: {doc.page_count}</span>}
                      <span>{t('knowledge.chunks')}: {doc.chunk_count || 0}</span>
                      {doc.file_size && (
                        <span>
                          {t('knowledge.size')}: {(doc.file_size / 1024 / 1024).toFixed(2)} MB
                        </span>
                      )}
                    </div>
                    {doc.created_at && (
                      <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                        {t('knowledge.createdAt', {
                          date: formatDateTime(doc.created_at, i18n.language),
                        })}
                      </p>
                    )}
                    {doc.status === 'failed' && doc.error_message && (
                      <div className="mt-1">
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedErrors((prev) => ({ ...prev, [doc.id]: !prev[doc.id] }))
                          }
                          className="text-xs text-red-600 dark:text-red-400 underline hover:no-underline min-h-[44px] sm:min-h-0"
                          aria-expanded={Boolean(expandedErrors[doc.id])}
                        >
                          {expandedErrors[doc.id]
                            ? t('knowledge.hideDetails')
                            : t('knowledge.showDetails')}
                        </button>
                        {expandedErrors[doc.id] && (
                          <pre className="mt-1 text-xs text-red-600 dark:text-red-400 whitespace-pre-wrap break-words font-mono">
                            {doc.error_message}
                          </pre>
                        )}
                      </div>
                    )}
                    <FaktenPanel
                      documentId={doc.id}
                      status={doc.status}
                      open={Boolean(expandedFacts[doc.id])}
                      onToggle={() =>
                        setExpandedFacts((prev) => ({ ...prev, [doc.id]: !prev[doc.id] }))
                      }
                    />
                  </div>
                  <div className="flex items-center gap-2">
                    <TierControlPopover
                      tier={doc.circle_tier ?? 0}
                      onChange={(tier) => handleSetDocTier(doc, tier)}
                      confirmPublic={confirmPublicTier}
                      busy={setDocTier.isPending}
                    />
                    <StatusBadge doc={doc} filename={doc.filename} />
                    <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                      {knowledgeBases.length > 0 && (
                        <div className="relative">
                          <button
                            onClick={() => setShowMoveDropdown(showMoveDropdown === doc.id ? null : doc.id)}
                            className="btn-icon btn-icon-ghost"
                            title={t('knowledge.moveDocument')}
                          >
                            <ArrowRightLeft className="w-4 h-4" />
                          </button>
                          {showMoveDropdown === doc.id && (
                            <MoveKbDropdown
                              docIds={[doc.id]}
                              onClose={() => setShowMoveDropdown(null)}
                            />
                          )}
                        </div>
                      )}
                      {simbaEnabled && (
                        <button
                          onClick={() => setSimbaSendDoc({ id: doc.id, filename: doc.display_name ?? doc.filename })}
                          className="btn-icon btn-icon-ghost"
                          title={t('knowledge.sendToSimba')}
                          aria-label={t('knowledge.sendToSimba')}
                        >
                          <Landmark className="w-4 h-4" />
                        </button>
                      )}
                      <button
                        onClick={() => handleReindexDocument(doc.id)}
                        className="btn-icon btn-icon-ghost"
                        title={t('knowledge.reindex')}
                      >
                        <RefreshCw className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDeleteDocument(doc.id, doc.filename)}
                        className="btn-icon btn-icon-danger"
                        title={t('common.delete')}
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                </div>
              </div>
              );
            })}
          </>
        )}
      </div>

      {ConfirmDialogComponent}
      {simbaEnabled && (
        <SimbaSendModal
          documentId={simbaSendDoc?.id ?? null}
          filename={simbaSendDoc?.filename ?? ''}
          enabled={simbaEnabled}
          onClose={() => setSimbaSendDoc(null)}
        />
      )}
      {tierToast && (
        <TierChangeToast tier={tierToast.newTier} onUndo={handleUndoTier} />
      )}
      {duplicate && (
        <DuplicateDialog
          existing={duplicate}
          onClose={() => setDuplicate(null)}
          onJump={(id) => {
            // Jump to the existing doc by scrolling its row into view and
            // briefly highlighting it. The row's anchor id matches `doc-{id}`.
            const el = document.getElementById(`doc-${id}`);
            if (el) {
              el.scrollIntoView({ behavior: 'smooth', block: 'center' });
              el.classList.add('ring-2', 'ring-primary-400');
              setTimeout(() => {
                el.classList.remove('ring-2', 'ring-primary-400');
              }, 2500);
            }
          }}
        />
      )}

      {/* New Knowledge Base Modal */}
      {showNewKbModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-xs flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-xl p-6 w-full max-w-md mx-4 border border-gray-200 dark:border-gray-700">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">
              {t('knowledge.createKnowledgeBase')}
            </h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t('common.name')} *
                </label>
                <input
                  type="text"
                  value={newKbName}
                  onChange={(e) => setNewKbName(e.target.value)}
                  placeholder={t('knowledge.knowledgeBases')}
                  className="input w-full"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t('common.description')}
                </label>
                <textarea
                  value={newKbDescription}
                  onChange={(e) => setNewKbDescription(e.target.value)}
                  placeholder="Optionale Beschreibung..."
                  rows={3}
                  className="input w-full resize-none"
                />
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button
                onClick={() => {
                  setShowNewKbModal(false);
                  setNewKbName('');
                  setNewKbDescription('');
                }}
                className="btn btn-secondary"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={handleCreateKnowledgeBase}
                disabled={!newKbName.trim()}
                className="btn-primary"
              >
                {t('common.create')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
