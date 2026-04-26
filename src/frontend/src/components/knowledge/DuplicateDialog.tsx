/**
 * DuplicateDialog — modal surface for the 409 response on duplicate upload (#388).
 *
 * The backend includes an `existing_document` payload with id, filename,
 * and uploaded_at. We render a proper dialog (not a toast) because the user
 * needs a choice: jump to the existing entry, or cancel.
 *
 * C2 additions:
 *   - Focus trap: Tab and Shift+Tab cycle within the dialog only.
 *   - Focus return: whichever element owned focus when the dialog opened
 *     gets focus back when it closes (typically the upload input).
 *   - Initial focus on the jump button stays C1 behaviour.
 *   - Escape closes — unchanged.
 */
import { MouseEvent, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';

export interface ExistingDocument {
  id: number | string;
  filename: string;
  uploaded_at?: string | null;
}

interface DuplicateDialogProps {
  existing: ExistingDocument | null;
  onClose: () => void;
  onJump: (id: ExistingDocument['id']) => void;
}

// Query that matches anything keyboard-focusable. We use this to scope the
// Tab/Shift+Tab cycle to elements living inside the dialog.
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export default function DuplicateDialog({ existing, onClose, onJump }: DuplicateDialogProps) {
  const jumpBtnRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const { t, i18n } = useTranslation();

  useEffect(() => {
    // Remember who owned focus so we can restore it on close.
    lastFocusedRef.current = document.activeElement as HTMLElement | null;
    if (jumpBtnRef.current) jumpBtnRef.current.focus();

    const onKey = (e: globalThis.KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      if (!dialogRef.current) return;

      // Focus trap: keep Tab/Shift+Tab within the dialog.
      const focusables = dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      const prev = lastFocusedRef.current;
      if (prev && typeof prev.focus === 'function') {
        try {
          prev.focus();
        } catch {
          /* element gone; nothing to do */
        }
      }
    };
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
      onClick={(e: MouseEvent<HTMLDivElement>) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="bg-white dark:bg-gray-900 rounded-lg shadow-xl p-6 max-w-md w-full mx-4"
      >
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
