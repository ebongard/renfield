import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Brain,
  Plus,
  Trash2,
  Edit3,
  Eye,
  Calendar,
} from 'lucide-react';
import Modal from '../components/Modal';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import type { BadgeColor } from '../components/Badge';
import { useConfirmDialog } from '../components/ConfirmDialog';
import { extractApiError } from '../utils/axios';
import {
  useMemoriesQuery,
  useCreateMemory,
  useUpdateMemory,
  useDeleteMemory,
  type Memory,
  type MemoryCategory,
} from '../api/resources/memories';

const CATEGORIES: MemoryCategory[] = ['preference', 'fact', 'instruction', 'context'];

const CATEGORY_BADGE_COLORS: Record<MemoryCategory, BadgeColor> = {
  preference: 'purple',
  fact: 'blue',
  instruction: 'amber',
  context: 'green',
};

export default function MemoryPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [activeCategory, setActiveCategory] = useState<MemoryCategory | null>(null);

  const memoriesQuery = useMemoriesQuery(activeCategory);
  const memories = memoriesQuery.data?.memories ?? [];
  const total = memoriesQuery.data?.total ?? 0;

  const createMemory = useCreateMemory();
  const updateMemory = useUpdateMemory();
  const deleteMemory = useDeleteMemory();

  const [success, setSuccess] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);

  // Modal state
  const [showModal, setShowModal] = useState(false);
  const [editingMemory, setEditingMemory] = useState<Memory | null>(null);
  const [formContent, setFormContent] = useState('');
  const [formCategory, setFormCategory] = useState<MemoryCategory>('fact');
  const [formImportance, setFormImportance] = useState(0.5);

  const error = memoriesQuery.errorMessage ?? mutationError;

  // Auto-clear messages
  useEffect(() => {
    if (mutationError || success) {
      const timer = setTimeout(() => {
        setMutationError(null);
        setSuccess(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [mutationError, success]);

  const openCreateModal = () => {
    setEditingMemory(null);
    setFormContent('');
    setFormCategory('fact');
    setFormImportance(0.5);
    setShowModal(true);
  };

  const openEditModal = (memory: Memory) => {
    setEditingMemory(memory);
    setFormContent(memory.content);
    setFormCategory(memory.category);
    setFormImportance(memory.importance);
    setShowModal(true);
  };

  const handleSave = async () => {
    const input = {
      content: formContent,
      category: formCategory,
      importance: formImportance,
    };
    try {
      if (editingMemory) {
        await updateMemory.mutateAsync({ id: editingMemory.id, input });
        setSuccess(t('memory.updated'));
      } else {
        await createMemory.mutateAsync(input);
        setSuccess(t('memory.created'));
      }
      setShowModal(false);
    } catch (err) {
      setMutationError(extractApiError(err, t('common.error')));
    }
  };

  const handleDelete = async (memory: Memory) => {
    const confirmed = await confirm({
      title: t('memory.deleteTitle'),
      message: t('memory.deleteConfirm'),
      confirmLabel: t('common.delete'),
      cancelLabel: t('common.cancel'),
      variant: 'danger',
    });
    if (!confirmed) return;

    try {
      await deleteMemory.mutateAsync(memory.id);
      setSuccess(t('memory.deleted'));
    } catch (err) {
      setMutationError(extractApiError(err, t('common.error')));
    }
  };

  const formatDate = (dateStr?: string): string => {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString();
  };

  const ImportanceDots = ({ value }: { value: number }) => {
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
      <PageHeader icon={Brain} title={t('memory.title')} subtitle={t('memory.subtitle')}>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {t('memory.count', { count: total })}
        </span>
        <button onClick={openCreateModal} className="btn-primary flex items-center space-x-2">
          <Plus className="w-4 h-4" />
          <span className="hidden sm:inline">{t('memory.addMemory')}</span>
        </button>
      </PageHeader>

      {error && <Alert variant="error">{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

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

      {memoriesQuery.isLoading ? (
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
              className="group card hover:shadow-md transition-shadow"
            >
              <div className="flex items-start justify-between mb-2">
                <Badge color={CATEGORY_BADGE_COLORS[memory.category] || 'gray'}>
                  {t(`memory.categories.${memory.category}`)}
                </Badge>
                <div className="flex items-center space-x-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                  <button
                    onClick={() => openEditModal(memory)}
                    className="btn-icon btn-icon-ghost"
                    title={t('common.edit')}
                  >
                    <Edit3 className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => handleDelete(memory)}
                    className="btn-icon btn-icon-danger"
                    title={t('common.delete')}
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>

              <p className="text-sm text-gray-800 dark:text-gray-200 mb-3 line-clamp-3">
                {memory.content}
              </p>

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

      <Modal
        isOpen={showModal}
        onClose={() => setShowModal(false)}
        title={editingMemory ? t('memory.editMemory') : t('memory.addMemory')}
      >
        <div className="space-y-4">
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

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('common.type')}
            </label>
            <select
              value={formCategory}
              onChange={(e) => setFormCategory(e.target.value as MemoryCategory)}
              className="input w-full"
            >
              {CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>
                  {t(`memory.categories.${cat}`)}
                </option>
              ))}
            </select>
          </div>

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

          <div className="flex justify-end space-x-3 pt-2">
            <button
              onClick={() => setShowModal(false)}
              className="btn-secondary"
            >
              {t('common.cancel')}
            </button>
            <button
              onClick={handleSave}
              disabled={!formContent.trim() || createMemory.isPending || updateMemory.isPending}
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
