import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

import type { Skill } from '../api/resources/skills';

interface SkillEditModalProps {
  skill: Skill | null;
  isOpen: boolean;
  onClose: () => void;
  onSave: (input: {
    id: number;
    title: string;
    body_md: string;
    trigger_examples: string[];
    tool_sequence: string[];
  }) => Promise<unknown>;
  isPending?: boolean;
  errorMessage?: string | null;
}

/**
 * Modal editor for a single skill. Used by both the admin Skills Inbox
 * (admin tweaks a draft before approving) and the owner BrainSkillsPage
 * (owner refines their own approved skill).
 *
 * Triggers and tool_sequence are entered as one-per-line textareas — the
 * pattern matches the existing MemoryPage editor, and avoids the
 * round-trip-pain of array-of-inputs for what's usually a 2-5 entry list.
 */
export default function SkillEditModal({
  skill,
  isOpen,
  onClose,
  onSave,
  isPending = false,
  errorMessage = null,
}: SkillEditModalProps) {
  const { t } = useTranslation();
  const [title, setTitle] = useState('');
  const [bodyMd, setBodyMd] = useState('');
  const [triggersText, setTriggersText] = useState('');
  const [toolsText, setToolsText] = useState('');

  useEffect(() => {
    if (skill) {
      setTitle(skill.title);
      setBodyMd(skill.body_md);
      setTriggersText((skill.trigger_examples ?? []).join('\n'));
      setToolsText((skill.tool_sequence ?? []).join('\n'));
    }
  }, [skill]);

  if (!isOpen || !skill) return null;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    const triggers = triggersText
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    const tools = toolsText
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    await onSave({
      id: skill.id,
      title: title.trim(),
      body_md: bodyMd,
      trigger_examples: triggers,
      tool_sequence: tools,
    });
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="skill-edit-title"
      onClick={onClose}
    >
      <div
        className="card max-w-2xl w-full max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
        data-testid="skill-edit-modal"
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 id="skill-edit-title" className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('selfLearning.skills.modal.title')}
          </h2>
          <button
            type="button"
            className="p-1 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
            onClick={onClose}
            aria-label={t('selfLearning.skills.modal.close')}
          >
            <X className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSave} className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          <label className="block">
            <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('selfLearning.skills.modal.titleField')}
            </span>
            <input
              type="text"
              className="input w-full"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={255}
              required
              data-testid="modal-title-input"
            />
          </label>

          <label className="block">
            <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('selfLearning.skills.modal.body')}
            </span>
            <textarea
              className="input w-full font-mono text-sm"
              value={bodyMd}
              onChange={(e) => setBodyMd(e.target.value)}
              rows={8}
              maxLength={8000}
              required
              data-testid="modal-body-input"
            />
          </label>

          <label className="block">
            <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('selfLearning.skills.modal.triggers')}
            </span>
            <textarea
              className="input w-full text-sm"
              value={triggersText}
              onChange={(e) => setTriggersText(e.target.value)}
              rows={4}
              placeholder={t('selfLearning.skills.modal.triggersPlaceholder')}
              data-testid="modal-triggers-input"
            />
          </label>

          <label className="block">
            <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('selfLearning.skills.modal.tools')}
            </span>
            <textarea
              className="input w-full text-sm font-mono"
              value={toolsText}
              onChange={(e) => setToolsText(e.target.value)}
              rows={3}
              placeholder={t('selfLearning.skills.modal.toolsPlaceholder')}
              data-testid="modal-tools-input"
            />
          </label>

          {errorMessage && (
            <div className="text-sm text-rose-600 dark:text-rose-300" role="alert">
              {errorMessage}
            </div>
          )}
        </form>

        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-200 dark:border-gray-700">
          <button
            type="button"
            className="btn-secondary px-4 py-2"
            onClick={onClose}
            disabled={isPending}
          >
            {t('selfLearning.skills.modal.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary px-4 py-2"
            onClick={handleSave}
            disabled={isPending || !title.trim() || !bodyMd.trim()}
            data-testid="modal-save-button"
          >
            {isPending ? t('selfLearning.skills.modal.saving') : t('selfLearning.skills.modal.save')}
          </button>
        </div>
      </div>
    </div>
  );
}
