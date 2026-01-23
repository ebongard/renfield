import React, { useState, useEffect, useCallback } from 'react';
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
  XCircle,
  Clock,
  Loader,
  Database,
  Layers,
  File,
  AlertCircle
} from 'lucide-react';
import apiClient from '../utils/axios';

export default function KnowledgePage() {
  // State
  const [documents, setDocuments] = useState([]);
  const [knowledgeBases, setKnowledgeBases] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(null);

  // Search state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [searching, setSearching] = useState(false);

  // Filter state
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState(null);
  const [statusFilter, setStatusFilter] = useState('all');

  // New Knowledge Base state
  const [showNewKbModal, setShowNewKbModal] = useState(false);
  const [newKbName, setNewKbName] = useState('');
  const [newKbDescription, setNewKbDescription] = useState('');

  // Load data
  const loadDocuments = useCallback(async () => {
    try {
      const params = {};
      if (selectedKnowledgeBase) params.knowledge_base_id = selectedKnowledgeBase;
      if (statusFilter !== 'all') params.status = statusFilter;

      const response = await apiClient.get('/api/knowledge/documents', { params });
      setDocuments(response.data);
    } catch (error) {
      console.error('Fehler beim Laden der Dokumente:', error);
    }
  }, [selectedKnowledgeBase, statusFilter]);

  const loadKnowledgeBases = useCallback(async () => {
    try {
      const response = await apiClient.get('/api/knowledge/bases');
      setKnowledgeBases(response.data);
    } catch (error) {
      console.error('Fehler beim Laden der Knowledge Bases:', error);
    }
  }, []);

  const loadStats = useCallback(async () => {
    try {
      const response = await apiClient.get('/api/knowledge/stats');
      setStats(response.data);
    } catch (error) {
      console.error('Fehler beim Laden der Statistiken:', error);
    }
  }, []);

  useEffect(() => {
    const loadAll = async () => {
      setLoading(true);
      await Promise.all([loadDocuments(), loadKnowledgeBases(), loadStats()]);
      setLoading(false);
    };
    loadAll();
  }, [loadDocuments, loadKnowledgeBases, loadStats]);

  // File upload handler
  const handleUpload = async (event) => {
    const file = event.target.files[0];
    if (!file) return;

    setUploading(true);
    setUploadProgress(`Verarbeite ${file.name}...`);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const params = selectedKnowledgeBase
        ? { knowledge_base_id: selectedKnowledgeBase }
        : {};

      await apiClient.post('/api/knowledge/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        params
      });

      setUploadProgress('Erfolgreich hochgeladen!');
      await loadDocuments();
      await loadStats();

      setTimeout(() => setUploadProgress(null), 2000);
    } catch (error) {
      console.error('Upload-Fehler:', error);
      setUploadProgress(`Fehler: ${error.response?.data?.detail || 'Upload fehlgeschlagen'}`);
      setTimeout(() => setUploadProgress(null), 5000);
    } finally {
      setUploading(false);
      event.target.value = '';
    }
  };

  // Delete document
  const handleDeleteDocument = async (id, filename) => {
    if (!confirm(`Dokument "${filename}" wirklich loeschen?`)) return;

    try {
      await apiClient.delete(`/api/knowledge/documents/${id}`);
      await loadDocuments();
      await loadStats();
    } catch (error) {
      console.error('Loeschfehler:', error);
      alert('Fehler beim Loeschen des Dokuments');
    }
  };

  // Reindex document
  const handleReindexDocument = async (id) => {
    try {
      await apiClient.post(`/api/knowledge/documents/${id}/reindex`);
      await loadDocuments();
      await loadStats();
    } catch (error) {
      console.error('Reindex-Fehler:', error);
      alert('Fehler beim Re-Indexieren des Dokuments');
    }
  };

  // Search
  const handleSearch = async () => {
    if (!searchQuery.trim()) return;

    setSearching(true);
    try {
      const response = await apiClient.post('/api/knowledge/search', {
        query: searchQuery,
        top_k: 5,
        knowledge_base_id: selectedKnowledgeBase
      });
      setSearchResults(response.data.results);
    } catch (error) {
      console.error('Suchfehler:', error);
    } finally {
      setSearching(false);
    }
  };

  // Create Knowledge Base
  const handleCreateKnowledgeBase = async () => {
    if (!newKbName.trim()) return;

    try {
      await apiClient.post('/api/knowledge/bases', {
        name: newKbName,
        description: newKbDescription || null
      });
      await loadKnowledgeBases();
      await loadStats();
      setShowNewKbModal(false);
      setNewKbName('');
      setNewKbDescription('');
    } catch (error) {
      console.error('Fehler beim Erstellen:', error);
      alert(error.response?.data?.detail || 'Fehler beim Erstellen der Knowledge Base');
    }
  };

  // Delete Knowledge Base
  const handleDeleteKnowledgeBase = async (id, name) => {
    if (!confirm(`Knowledge Base "${name}" und alle zugehoerigen Dokumente wirklich loeschen?`)) return;

    try {
      await apiClient.delete(`/api/knowledge/bases/${id}`);
      if (selectedKnowledgeBase === id) setSelectedKnowledgeBase(null);
      await loadKnowledgeBases();
      await loadDocuments();
      await loadStats();
    } catch (error) {
      console.error('Loeschfehler:', error);
      alert('Fehler beim Loeschen der Knowledge Base');
    }
  };

  // Status icon helper
  const getStatusIcon = (status) => {
    switch (status) {
      case 'completed':
        return <CheckCircle className="w-5 h-5 text-green-500" />;
      case 'processing':
        return <Loader className="w-5 h-5 text-blue-500 animate-spin" />;
      case 'pending':
        return <Clock className="w-5 h-5 text-yellow-500" />;
      case 'failed':
        return <XCircle className="w-5 h-5 text-red-500" />;
      default:
        return <AlertCircle className="w-5 h-5 text-gray-500" />;
    }
  };

  // File type icon helper
  const getFileIcon = (fileType) => {
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

  const statusFilters = ['all', 'completed', 'processing', 'pending', 'failed'];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2 flex items-center gap-2">
              <BookOpen className="w-7 h-7 text-primary-400" />
              Wissensdatenbank
            </h1>
            <p className="text-gray-500 dark:text-gray-400">
              Dokumente hochladen und fuer RAG-Anfragen indexieren
            </p>
          </div>
          <button
            onClick={() => setShowNewKbModal(true)}
            className="btn-primary flex items-center gap-2"
          >
            <Plus className="w-4 h-4" />
            Neue Knowledge Base
          </button>
        </div>
      </div>

      {/* Statistics */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="card bg-gradient-to-br from-blue-100 to-blue-50 dark:from-blue-900/50 dark:to-blue-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-blue-200 dark:bg-blue-600/30 rounded-lg">
                <FileText className="w-6 h-6 text-blue-600 dark:text-blue-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.document_count}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Dokumente</div>
              </div>
            </div>
          </div>
          <div className="card bg-gradient-to-br from-green-100 to-green-50 dark:from-green-900/50 dark:to-green-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-green-200 dark:bg-green-600/30 rounded-lg">
                <CheckCircle className="w-6 h-6 text-green-600 dark:text-green-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.completed_documents}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Indexiert</div>
              </div>
            </div>
          </div>
          <div className="card bg-gradient-to-br from-purple-100 to-purple-50 dark:from-purple-900/50 dark:to-purple-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-purple-200 dark:bg-purple-600/30 rounded-lg">
                <Layers className="w-6 h-6 text-purple-600 dark:text-purple-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.chunk_count}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Chunks</div>
              </div>
            </div>
          </div>
          <div className="card bg-gradient-to-br from-orange-100 to-orange-50 dark:from-orange-900/50 dark:to-orange-800/30">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-orange-200 dark:bg-orange-600/30 rounded-lg">
                <Database className="w-6 h-6 text-orange-600 dark:text-orange-400" />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.knowledge_base_count}</div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Knowledge Bases</div>
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
            Knowledge Bases
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
              Alle
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
                  title="Knowledge Base loeschen"
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
          Dokument hochladen
        </h2>
        <div className="border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-6 text-center hover:border-primary-500 transition-colors">
          <input
            type="file"
            onChange={handleUpload}
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
              {uploading ? 'Wird verarbeitet...' : 'Klicken oder Datei hierher ziehen'}
            </p>
            <p className="text-sm text-gray-500 dark:text-gray-500">
              Unterstuetzte Formate: PDF, DOCX, TXT, MD, HTML, PPTX, XLSX
            </p>
          </label>
        </div>
        {uploadProgress && (
          <div className={`mt-4 p-3 rounded-lg ${
            uploadProgress.includes('Fehler')
              ? 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300'
              : uploadProgress.includes('Erfolgreich')
              ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
              : 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300'
          }`}>
            {uploadProgress}
          </div>
        )}
      </div>

      {/* Search Section */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4 flex items-center gap-2">
          <Search className="w-5 h-5 text-primary-400" />
          In Dokumenten suchen
        </h2>
        <div className="flex gap-2">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyPress={(e) => e.key === 'Enter' && handleSearch()}
            placeholder="Suchbegriff eingeben..."
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
            Suchen
          </button>
        </div>

        {/* Search Results */}
        {searchResults.length > 0 && (
          <div className="mt-4 space-y-3">
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
              {searchResults.length} Ergebnisse gefunden
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
                        | Seite {result.chunk.page_number}
                      </span>
                    )}
                  </div>
                  <span className="text-xs px-2 py-1 bg-primary-100 text-primary-700 dark:bg-primary-600/30 dark:text-primary-300 rounded">
                    {Math.round(result.similarity * 100)}% Relevanz
                  </span>
                </div>
                <p className="text-gray-700 dark:text-gray-300 text-sm line-clamp-3">
                  {result.chunk.content}
                </p>
                {result.chunk.section_title && (
                  <p className="mt-2 text-xs text-gray-500 dark:text-gray-500">
                    Abschnitt: {result.chunk.section_title}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Status Filters */}
      <div className="flex space-x-2 overflow-x-auto">
        {statusFilters.map((f) => (
          <button
            key={f}
            onClick={() => setStatusFilter(f)}
            className={`px-4 py-2 rounded-lg capitalize whitespace-nowrap transition-colors ${
              statusFilter === f
                ? 'bg-primary-600 text-white'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
            }`}
          >
            {f === 'all' ? 'Alle' : f}
          </button>
        ))}
      </div>

      {/* Documents List */}
      <div className="space-y-4">
        {loading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">Lade Dokumente...</p>
          </div>
        ) : documents.length === 0 ? (
          <div className="card text-center py-12">
            <FileText className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">Keine Dokumente gefunden</p>
            <p className="text-sm text-gray-400 dark:text-gray-500 mt-2">
              Laden Sie ein Dokument hoch, um die Wissensdatenbank zu fuellen.
            </p>
          </div>
        ) : (
          documents.map((doc) => (
            <div key={doc.id} className="card">
              <div className="flex items-start space-x-4">
                <div className="mt-1">{getFileIcon(doc.file_type)}</div>
                <div className="flex-1 min-w-0">
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-1 truncate">
                    {doc.title || doc.filename}
                  </h3>
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-500 dark:text-gray-400">
                    <span>Typ: {doc.file_type?.toUpperCase()}</span>
                    {doc.page_count && <span>Seiten: {doc.page_count}</span>}
                    <span>Chunks: {doc.chunk_count || 0}</span>
                    {doc.file_size && (
                      <span>
                        Groesse: {(doc.file_size / 1024 / 1024).toFixed(2)} MB
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                    Erstellt: {new Date(doc.created_at).toLocaleString('de-DE')}
                  </p>
                  {doc.error_message && (
                    <p className="text-xs text-red-600 dark:text-red-400 mt-1">
                      Fehler: {doc.error_message}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <div className="flex items-center gap-2">
                    {getStatusIcon(doc.status)}
                    <span
                      className={`px-3 py-1 rounded-full text-xs font-medium ${
                        doc.status === 'completed'
                          ? 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300'
                          : doc.status === 'failed'
                          ? 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300'
                          : doc.status === 'processing'
                          ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
                          : 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-300'
                      }`}
                    >
                      {doc.status}
                    </span>
                  </div>
                  <button
                    onClick={() => handleReindexDocument(doc.id)}
                    className="p-2 text-gray-500 hover:text-primary-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-primary-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                    title="Neu indexieren"
                  >
                    <RefreshCw className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => handleDeleteDocument(doc.id, doc.filename)}
                    className="p-2 text-gray-500 hover:text-red-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-red-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                    title="Loeschen"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* New Knowledge Base Modal */}
      {showNewKbModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-xl p-6 w-full max-w-md mx-4 border border-gray-200 dark:border-gray-700">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">
              Neue Knowledge Base erstellen
            </h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Name *
                </label>
                <input
                  type="text"
                  value={newKbName}
                  onChange={(e) => setNewKbName(e.target.value)}
                  placeholder="z.B. Handbuecher"
                  className="input w-full"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Beschreibung
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
                Abbrechen
              </button>
              <button
                onClick={handleCreateKnowledgeBase}
                disabled={!newKbName.trim()}
                className="btn-primary"
              >
                Erstellen
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
