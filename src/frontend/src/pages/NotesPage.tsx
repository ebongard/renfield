import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import {
  NotebookPen, Loader, XCircle, Plus, Pencil, Trash2, Check, X,
} from 'lucide-react';

import PageHeader from '../components/PageHeader';
import TierBadge from '../components/TierBadge';
import { formatDateTime } from '../utils/datetime';
import {
  useNotesQuery, useCreateNote, useUpdateNote, useDeleteNote, type Note,
} from '../api/resources/notes';

/** One note card: read view + inline edit + inline-confirm delete. */
function NoteCard({ note }: { note: Note }) {
  const { t } = useTranslation();
  const update = useUpdateNote();
  const del = useDeleteNote();
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState(note.body);

  const save = async () => {
    if (!title.trim() || update.isPending) return;
    try {
      await update.mutateAsync({ id: note.id, title: title.trim(), body });
      setEditing(false);
    } catch {
      // surfaced via update.errorMessage
    }
  };

  const remove = async () => {
    if (del.isPending) return;
    try {
      await del.mutateAsync(note.id);
    } catch {
      setConfirmDelete(false);
    }
  };

  return (
    <div className="card">
      {editing ? (
        <div className="space-y-2">
          <input
            className="input font-semibold"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            aria-label={t('notes.titlePlaceholder')}
            maxLength={255}
          />
          <textarea
            className="input font-mono text-sm"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            aria-label={t('notes.bodyPlaceholder')}
            rows={6}
          />
          {update.errorMessage && (
            <p className="text-sm text-red-600 dark:text-red-400">{update.errorMessage}</p>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn-primary inline-flex items-center gap-1.5"
              onClick={save}
              disabled={!title.trim() || update.isPending}
            >
              {update.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              {t('notes.save')}
            </button>
            <button
              type="button"
              className="btn-secondary inline-flex items-center gap-1.5"
              onClick={() => { setEditing(false); setTitle(note.title); setBody(note.body); }}
            >
              <X className="w-4 h-4" />
              {t('notes.cancel')}
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h3 className="text-base font-semibold text-gray-900 dark:text-white truncate">
                {note.title}
              </h3>
              {note.body && (
                <p className="text-sm text-gray-500 dark:text-gray-400 mt-1 whitespace-pre-wrap line-clamp-4">
                  {note.body}
                </p>
              )}
              <p className="text-xs text-gray-400 mt-2">
                {t('notes.updated')}: {formatDateTime(note.updated_at)}
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <TierBadge tier={note.circle_tier} />
              <button
                type="button"
                className="p-1 rounded text-gray-400 hover:text-primary-600 dark:hover:text-primary-400 hover:bg-gray-100 dark:hover:bg-gray-700"
                onClick={() => setEditing(true)}
                aria-label={t('notes.edit')}
                title={t('notes.edit')}
              >
                <Pencil className="w-4 h-4" />
              </button>
              {confirmDelete ? (
                <span className="inline-flex items-center gap-1">
                  <button
                    type="button"
                    className="p-1 rounded text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30"
                    onClick={remove}
                    disabled={del.isPending}
                    aria-label={t('notes.confirmDelete')}
                    title={t('notes.confirmDelete')}
                  >
                    {del.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                  </button>
                  <button
                    type="button"
                    className="p-1 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
                    onClick={() => setConfirmDelete(false)}
                    aria-label={t('notes.cancel')}
                    title={t('notes.cancel')}
                  >
                    <X className="w-4 h-4" />
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  className="p-1 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700"
                  onClick={() => setConfirmDelete(true)}
                  aria-label={t('notes.delete')}
                  title={t('notes.delete')}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              )}
            </div>
          </div>
          {del.errorMessage && (
            <p className="mt-2 text-sm text-red-600 dark:text-red-400">{del.errorMessage}</p>
          )}
        </>
      )}
    </div>
  );
}

export default function NotesPage() {
  const { t } = useTranslation();
  const notesQuery = useNotesQuery();
  const create = useCreateNote();
  const notes = notesQuery.data ?? [];

  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim() || create.isPending) return;
    try {
      await create.mutateAsync({ title: title.trim(), body: body.trim() || undefined });
      setTitle('');
      setBody('');
    } catch {
      // surfaced via create.errorMessage
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader icon={NotebookPen} title={t('notes.title')} subtitle={t('notes.subtitle')} />

      <form onSubmit={handleSubmit} className="card space-y-3">
        <h2 className="text-base font-semibold text-gray-900 dark:text-white">
          {t('notes.createTitle')}
        </h2>
        <input
          className="input"
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t('notes.titlePlaceholder')}
          aria-label={t('notes.titlePlaceholder')}
          maxLength={255}
        />
        <textarea
          className="input font-mono text-sm"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder={t('notes.bodyPlaceholder')}
          aria-label={t('notes.bodyPlaceholder')}
          rows={4}
        />
        {create.errorMessage && (
          <p className="text-sm text-red-600 dark:text-red-400">{create.errorMessage}</p>
        )}
        <button
          type="submit"
          className="btn-primary inline-flex items-center gap-2"
          disabled={!title.trim() || create.isPending}
        >
          {create.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
          {create.isPending ? t('notes.creating') : t('notes.create')}
        </button>
      </form>

      <div className="space-y-4">
        {notesQuery.isLoading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('notes.loading')}</p>
          </div>
        ) : notesQuery.errorMessage ? (
          <div className="card text-center py-12">
            <XCircle className="w-12 h-12 mx-auto text-red-500 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{notesQuery.errorMessage}</p>
          </div>
        ) : notes.length === 0 ? (
          <div className="card text-center py-12">
            <NotebookPen className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{t('notes.empty')}</p>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('notes.emptyDesc')}</p>
          </div>
        ) : (
          notes.map((n) => <NoteCard key={n.id} note={n} />)
        )}
      </div>
    </div>
  );
}
