import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Brain,
  Plus,
  Trash2,
  Edit3,
  Eye,
  Calendar,
  X
} from 'lucide-react';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import { useConfirmDialog } from '../components/ConfirmDialog';

const CATEGORIES = ['preference', 'fact', 'instruction', 'context'];

const CATEGORY_COLORS = {
  preference: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
  fact: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  instruction: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  context: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
};

export default function MemoryPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [memories, setMemories] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [activeCategory, setActiveCategory] = useState(null);

  // Modal state
  const [showModal, setShowModal] = useState(false);
  const [editingMemory, setEditingMemory] = useState(null);
  const [formContent, setFormContent] = useState('');
  const [formCategory, setFormCategory] = useState('fact');
  const [formImportance, setFormImportance] = useState(0.5);

  // Load memories
  const loadMemories = async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      if (activeCategory) params.set('category', activeCategory);
      params.set('limit', '100');

      const response = await apiClient.get(`/api/memory?${params}`);
      setMemories(response.data.memories);
      setTotal(response.data.total);
      setError(null);
    } catch (err) {
      setError(t('memory.couldNotLoad'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadMemories();
  }, [activeCategory]);

  // Auto-clear messages
  useEffect(() => {
    if (error || success) {
      const timer = setTimeout(() => {
        setError(null);
        setSuccess(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [error, success]);

  // Open create modal
  const openCreateModal = () => {
    setEditingMemory(null);
    setFormContent('');
    setFormCategory('fact');
    setFormImportance(0.5);
    setShowModal(true);
  };

  // Open edit modal
  const openEditModal = (memory) => {
    setEditingMemory(memory);
    setFormContent(memory.content);
    setFormCategory(memory.category);
    setFormImportance(memory.importance);
    setShowModal(true);
  };

  // Save (create or update)
  const handleSave = async () => {
    try {
      if (editingMemory) {
        await apiClient.patch(`/api/memory/${editingMemory.id}`, {
          content: formContent,
          category: formCategory,
          importance: formImportance,
        });
        setSuccess(t('memory.updated'));
      } else {
        await apiClient.post('/api/memory', {
          content: formContent,
          category: formCategory,
          importance: formImportance,
        });
        setSuccess(t('memory.created'));
      }
      setShowModal(false);
      loadMemories();
    } catch (err) {
      setError(err.response?.data?.detail || t('common.error'));
    }
  };

  // Delete
  const handleDelete = async (memory) => {
    const confirmed = await confirm({
      title: t('memory.deleteTitle'),
      message: t('memory.deleteConfirm'),
      confirmLabel: t('common.delete'),
      cancelLabel: t('common.cancel'),
      variant: 'danger',
    });
    if (!confirmed) return;

    try {
      await apiClient.delete(`/api/memory/${memory.id}`);
      setSuccess(t('memory.deleted'));
      loadMemories();
    } catch (err) {
      setError(err.response?.data?.detail || t('common.error'));
    }
  };

  // Format date
  const formatDate = (dateStr) => {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString();
  };

  // Importance dots
  const ImportanceDots = ({ value }) => {
    const filled = Math.round(value * 5);
    return (
      <div className="flex space-x-0.5" title={`${Math.round(value * 100)}%`}>
        {[1, 2, 3, 4, 5].map((i) => (
          <div
            key={i}
            className={`w-1.5 h-1.5 rounded-full ${
              i <= filled
                ? 'bg-primary-500 dark:bg-primary-400'
                : 'bg-gray-200 dark:bg-gray-600'
            }`}
          />
        ))}
      </div>
    );
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <Brain className="w-6 h-6 text-primary-600 dark:text-primary-400" />
            <div>
              <h1 className="text-xl font-bold text-gray-900 dark:text-white">
                {t('memory.title')}
              </h1>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                {t('memory.subtitle')}
              </p>
            </div>
          </div>
          <div className="flex items-center space-x-3">
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {t('memory.count', { count: total })}
            </span>
            <button onClick={openCreateModal} className="btn-primary flex items-center space-x-2">
              <Plus className="w-4 h-4" />
              <span className="hidden sm:inline">{t('memory.addMemory')}</span>
            </button>
          </div>
        </div>
      </div>

      {/* Status messages */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-3 text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}
      {success && (
        <div className="bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg p-3 text-sm text-green-700 dark:text-green-300">
          {success}
        </div>
      )}

      {/* Category filter */}
      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => setActiveCategory(null)}
          className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
            !activeCategory
              ? 'bg-primary-600 text-white'
              : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
          }`}
        >
          {t('memory.categories.all')}
        </button>
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            onClick={() => setActiveCategory(activeCategory === cat ? null : cat)}
            className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
              activeCategory === cat
                ? 'bg-primary-600 text-white'
                : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
            }`}
          >
            {t(`memory.categories.${cat}`)}
          </button>
        ))}
      </div>

      {/* Content */}
      {loading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </div>
      ) : memories.length === 0 ? (
        <div className="card text-center py-12">
          <Brain className="w-12 h-12 mx-auto mb-3 text-gray-300 dark:text-gray-600" />
          <p className="text-gray-500 dark:text-gray-400">{t('memory.noMemories')}</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {memories.map((memory) => (
            <div
              key={memory.id}
              className="card hover:shadow-md transition-shadow"
            >
              {/* Category badge + actions */}
              <div className="flex items-start justify-between mb-2">
                <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${CATEGORY_COLORS[memory.category] || 'bg-gray-100 text-gray-700'}`}>
                  {t(`memory.categories.${memory.category}`)}
                </span>
                <div className="flex items-center space-x-1">
                  <button
                    onClick={() => openEditModal(memory)}
                    className="p-1 rounded text-gray-400 hover:text-primary-600 dark:hover:text-primary-400 transition-colors"
                    title={t('common.edit')}
                  >
                    <Edit3 className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => handleDelete(memory)}
                    className="p-1 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                    title={t('common.delete')}
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>

              {/* Content */}
              <p className="text-sm text-gray-800 dark:text-gray-200 mb-3 line-clamp-3">
                {memory.content}
              </p>

              {/* Footer */}
              <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
                <div className="flex items-center space-x-3">
                  <ImportanceDots value={memory.importance} />
                  <span className="flex items-center space-x-1">
                    <Eye className="w-3 h-3" />
                    <span>{memory.access_count}</span>
                  </span>
                </div>
                <span className="flex items-center space-x-1">
                  <Calendar className="w-3 h-3" />
                  <span>{formatDate(memory.created_at)}</span>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create / Edit Modal */}
      <Modal
        isOpen={showModal}
        onClose={() => setShowModal(false)}
        title={editingMemory ? t('memory.editMemory') : t('memory.addMemory')}
      >
        <div className="space-y-4">
          {/* Content */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('memory.content')}
            </label>
            <textarea
              value={formContent}
              onChange={(e) => setFormContent(e.target.value)}
              className="input w-full h-24 resize-none"
              maxLength={2000}
              placeholder={t('memory.content')}
            />
          </div>

          {/* Category */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('common.type')}
            </label>
            <select
              value={formCategory}
              onChange={(e) => setFormCategory(e.target.value)}
              className="input w-full"
            >
              {CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>
                  {t(`memory.categories.${cat}`)}
                </option>
              ))}
            </select>
          </div>

          {/* Importance */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('memory.importance')}: {Math.round(formImportance * 100)}%
            </label>
            <input
              type="range"
              min="0.1"
              max="1.0"
              step="0.1"
              value={formImportance}
              onChange={(e) => setFormImportance(parseFloat(e.target.value))}
              className="w-full"
            />
          </div>

          {/* Actions */}
          <div className="flex justify-end space-x-3 pt-2">
            <button
              onClick={() => setShowModal(false)}
              className="btn-secondary"
            >
              {t('common.cancel')}
            </button>
            <button
              onClick={handleSave}
              disabled={!formContent.trim()}
              className="btn-primary disabled:opacity-50"
            >
              {t('common.save')}
            </button>
          </div>
        </div>
      </Modal>

      {ConfirmDialogComponent}
    </div>
  );
}
