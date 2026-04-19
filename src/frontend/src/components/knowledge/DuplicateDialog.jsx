/**
 * DuplicateDialog — modal surface for the 409 response on duplicate upload (#388).
 *
 * The backend includes an `existing_document` payload with id, filename,
 * and uploaded_at. We render a proper dialog (not a toast) because the user
 * needs a choice: jump to the existing entry, or cancel. A11y: escape key
 * closes, the jump button receives initial focus.
 *
 * Full focus-management polish (trap, return focus on close) lands in C2.
 */
import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';

export default function DuplicateDialog({ existing, onClose, onJump }) {
  const jumpBtnRef = useRef(null);
  const { t, i18n } = useTranslation();

  useEffect(() => {
    if (jumpBtnRef.current) jumpBtnRef.current.focus();
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!existing) return null;

  const uploadedAt = existing.uploaded_at
    ? new Date(existing.uploaded_at).toLocaleString(i18n.language || 'de')
    : '';

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="duplicate-dialog-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl p-6 max-w-md w-full mx-4">
        <h2 id="duplicate-dialog-title" className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
          {t('knowledge.duplicateTitle')}
        </h2>
        <p className="text-sm text-gray-700 dark:text-gray-300 mb-6">
          {t('knowledge.duplicateBody', { filename: existing.filename, date: uploadedAt })}
        </p>
        <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="btn-secondary min-h-[44px]"
          >
            {t('common.cancel')}
          </button>
          <button
            ref={jumpBtnRef}
            type="button"
            onClick={() => {
              onJump(existing.id);
              onClose();
            }}
            className="btn-primary min-h-[44px]"
          >
            {t('knowledge.duplicateAction')}
          </button>
        </div>
      </div>
    </div>
  );
}
