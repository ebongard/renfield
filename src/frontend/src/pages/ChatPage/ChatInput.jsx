import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Send, Mic, MicOff, BookOpen, ChevronDown, Paperclip, X, FileText, Loader } from 'lucide-react';
import apiClient from '../../utils/axios';
import AudioVisualizer from './AudioVisualizer';
import { useChatContext } from './context/ChatContext';

export default function ChatInput() {
  const { t } = useTranslation();
  const {
    input, setInput, sendMessage, loading, recording, toggleRecording,
    audioLevel, silenceTimeRemaining,
    useRag, toggleRag, selectedKnowledgeBase, setSelectedKnowledgeBase,
    attachments, uploading, uploadDocument, removeAttachment, uploadStates,
  } = useChatContext();

  const fileInputRef = useRef(null);

  const [knowledgeBases, setKnowledgeBases] = useState([]);
  const [showRagSettings, setShowRagSettings] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    setIsDragOver(false);
    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      uploadDocument(Array.from(files));
    }
  }, [uploadDocument]);

  // Load knowledge bases when RAG is enabled
  useEffect(() => {
    if (useRag && knowledgeBases.length === 0) {
      loadKnowledgeBases();
    }
  }, [useRag]);

  const loadKnowledgeBases = async () => {
    try {
      const response = await apiClient.get('/api/knowledge/bases');
      setKnowledgeBases(response.data);
    } catch (error) {
      console.error('Error loading Knowledge Bases:', error);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage?.(input, false);
    }
  };

  const handleFileChange = (e) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      uploadDocument(Array.from(files));
    }
    // Reset input so same file can be re-selected
    e.target.value = '';
  };

  const handleSelectKb = (kbId) => {
    setSelectedKnowledgeBase?.(kbId);
    setShowRagSettings(false);
  };

  return (
    <div
      className={`card mx-4 mb-4 md:mx-0 md:mb-0 ${isDragOver ? 'ring-2 ring-primary-500' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* RAG Toggle */}
      <div className="flex items-center justify-between mb-3 pb-3 border-b border-gray-200 dark:border-gray-700">
        <div className="flex items-center space-x-3">
          <button
            onClick={toggleRag}
            className={`flex items-center space-x-2 px-3 py-1.5 rounded-lg text-sm transition-colors ${
              useRag
                ? 'bg-primary-100 text-primary-700 border border-primary-300 dark:bg-primary-600/30 dark:text-primary-300 dark:border-primary-500/50'
                : 'bg-gray-200 text-gray-600 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-400 dark:hover:bg-gray-600'
            }`}
            title={useRag ? t('rag.disableKnowledge') : t('rag.enableKnowledge')}
          >
            <BookOpen className="w-4 h-4" />
            <span>{t('rag.knowledge')}</span>
          </button>

          {useRag && (
            <div className="relative">
              <button
                onClick={() => setShowRagSettings(!showRagSettings)}
                className="flex items-center space-x-1 px-3 py-1.5 bg-gray-200 dark:bg-gray-700 rounded-lg text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors"
              >
                <span>
                  {selectedKnowledgeBase
                    ? knowledgeBases.find(kb => kb.id === selectedKnowledgeBase)?.name || t('common.all')
                    : t('rag.allDocuments')}
                </span>
                <ChevronDown className={`w-4 h-4 transition-transform ${showRagSettings ? 'rotate-180' : ''}`} />
              </button>

              {showRagSettings && (
                <div className="absolute bottom-full left-0 mb-2 w-48 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 shadow-lg z-10">
                  <div className="p-2">
                    <button
                      onClick={() => handleSelectKb(null)}
                      className={`w-full text-left px-3 py-2 rounded text-sm ${
                        selectedKnowledgeBase === null
                          ? 'bg-primary-100 text-primary-700 dark:bg-primary-600/30 dark:text-primary-300'
                          : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
                      }`}
                    >
                      {t('rag.allDocuments')}
                    </button>
                    {knowledgeBases.map(kb => (
                      <button
                        key={kb.id}
                        onClick={() => handleSelectKb(kb.id)}
                        className={`w-full text-left px-3 py-2 rounded text-sm ${
                          selectedKnowledgeBase === kb.id
                            ? 'bg-primary-100 text-primary-700 dark:bg-primary-600/30 dark:text-primary-300'
                            : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
                        }`}
                      >
                        {kb.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {useRag && (
          <span className="text-xs text-gray-500">
            {t('rag.searchesInDocuments')}
          </span>
        )}
      </div>

      {/* Drop Zone Indicator */}
      {isDragOver && (
        <div className="flex items-center justify-center py-3 mb-3 border-2 border-dashed border-primary-400 rounded-lg bg-primary-50/50 dark:bg-primary-900/30">
          <Paperclip className="w-4 h-4 text-primary-500 mr-2" />
          <span className="text-sm text-primary-600 dark:text-primary-400">{t('chat.dropFileHere')}</span>
        </div>
      )}

      {/* Upload Progress Indicators */}
      {Object.entries(uploadStates || {}).filter(([, s]) => s.uploading).length > 0 && (
        <div className="flex flex-col gap-1.5 mb-3 pb-3 border-b border-gray-200 dark:border-gray-700">
          {Object.entries(uploadStates).filter(([, s]) => s.uploading).map(([key, state]) => (
            <div key={key} className="flex items-center gap-2 text-gray-600 dark:text-gray-300">
              <Loader className="w-3.5 h-3.5 animate-spin flex-shrink-0" aria-hidden="true" />
              <span className="text-xs truncate max-w-[120px]">{state.name}</span>
              <div className="flex-1 h-1.5 bg-gray-300 dark:bg-gray-600 rounded-full overflow-hidden">
                <div
                  className="h-full bg-primary-500 rounded-full transition-all duration-200"
                  style={{ width: `${state.progress}%` }}
                  role="progressbar"
                  aria-valuenow={state.progress}
                  aria-valuemin={0}
                  aria-valuemax={100}
                />
              </div>
              <span className="text-xs tabular-nums w-8 text-right">{state.progress}%</span>
            </div>
          ))}
        </div>
      )}

      {/* Pending Attachments */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-3 pb-3 border-b border-gray-200 dark:border-gray-700">
          {attachments.map(att => (
            <div
              key={att.id}
              className="flex items-center space-x-1 px-2 py-1 bg-gray-100 dark:bg-gray-700 rounded text-sm text-gray-700 dark:text-gray-300"
            >
              <FileText className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
              <span className="truncate max-w-[120px]">{att.filename}</span>
              <button
                onClick={() => removeAttachment(att.id)}
                className="ml-1 text-gray-400 hover:text-red-500 dark:hover:text-red-400"
                aria-label={t('chat.removeAttachment')}
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Audio Waveform Visualizer during recording */}
      {recording && (
        <AudioVisualizer
          audioLevel={audioLevel}
          silenceTimeRemaining={silenceTimeRemaining}
        />
      )}

      {/* Input Area */}
      <div className="flex items-center space-x-2">
        <label htmlFor="chat-input" className="sr-only">{t('chat.placeholder')}</label>
        <input
          id="chat-input"
          type="text"
          value={input}
          onChange={(e) => setInput?.(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={t('chat.placeholder')}
          className="input flex-1"
          disabled={loading || recording}
          aria-describedby={loading ? 'chat-loading-hint' : undefined}
        />
        {loading && <span id="chat-loading-hint" className="sr-only">{t('chat.processingMessage')}</span>}

        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          accept=".pdf,.docx,.doc,.txt,.md,.html,.pptx,.xlsx,.png,.jpg,.jpeg"
          onChange={handleFileChange}
          multiple
        />

        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={loading || recording || uploading}
          className="p-3 rounded-lg transition-colors bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300 disabled:opacity-50"
          aria-label={t('chat.attachFile')}
        >
          {uploading
            ? <Loader className="w-5 h-5 animate-spin" aria-hidden="true" />
            : <Paperclip className="w-5 h-5" aria-hidden="true" />
          }
        </button>

        <button
          onClick={toggleRecording}
          className={`p-3 rounded-lg transition-colors ${
            recording
              ? 'bg-red-600 hover:bg-red-700 text-white animate-pulse'
              : 'bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300'
          }`}
          disabled={loading}
          aria-label={recording ? t('voice.stopRecording') : t('voice.startRecording')}
          aria-pressed={recording}
        >
          {recording ? <MicOff className="w-5 h-5" aria-hidden="true" /> : <Mic className="w-5 h-5" aria-hidden="true" />}
        </button>

        <button
          onClick={() => sendMessage?.(input, false)}
          disabled={loading || !input.trim()}
          className="btn btn-primary"
          aria-label={t('chat.sendMessage')}
        >
          <Send className="w-5 h-5" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
