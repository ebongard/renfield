/**
 * ConfirmDialog Component
 *
 * Accessible replacement for native confirm() dialogs.
 * Supports customizable title, message, and button labels.
 */

import { ReactElement, useCallback, useState } from 'react';
import { AlertTriangle, Loader } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import Modal from './Modal';

export type ConfirmVariant = 'danger' | 'warning' | 'info';

interface ConfirmDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title?: string | null;
  message?: string;
  confirmLabel?: string | null;
  cancelLabel?: string | null;
  variant?: ConfirmVariant;
  isLoading?: boolean;
}

interface VariantStyle {
  icon: string;
  button: string;
}

const VARIANT_STYLES: Record<ConfirmVariant, VariantStyle> = {
  danger: {
    icon: 'bg-red-600/20 text-red-500',
    button: 'bg-red-600 hover:bg-red-700 text-white',
  },
  warning: {
    icon: 'bg-yellow-600/20 text-yellow-500',
    button: 'bg-yellow-600 hover:bg-yellow-700 text-white',
  },
  info: {
    icon: 'bg-blue-600/20 text-blue-500',
    button: 'bg-blue-600 hover:bg-blue-700 text-white',
  },
};

export default function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel,
  cancelLabel,
  variant = 'danger',
  isLoading = false,
}: ConfirmDialogProps) {
  const { t } = useTranslation();

  const resolvedTitle = title || t('confirmDialog.defaultTitle');
  const resolvedConfirmLabel = confirmLabel || t('confirmDialog.confirm');
  const resolvedCancelLabel = cancelLabel || t('confirmDialog.cancel');

  const styles = VARIANT_STYLES[variant] ?? VARIANT_STYLES.danger;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      showCloseButton={false}
      closeOnOverlayClick={!isLoading}
      closeOnEscape={!isLoading}
    >
      <div className="text-center">
        <div className={`w-16 h-16 mx-auto mb-4 rounded-full flex items-center justify-center ${styles.icon}`}>
          <AlertTriangle className="w-8 h-8" aria-hidden="true" />
        </div>

        <h2 id="confirm-title" className="text-xl font-bold text-gray-900 dark:text-white mb-2">
          {resolvedTitle}
        </h2>

        {message && (
          <p className="text-gray-600 dark:text-gray-400 mb-6">
            {message}
          </p>
        )}

        <div className="flex space-x-3">
          <button
            onClick={onClose}
            disabled={isLoading}
            className="flex-1 btn btn-secondary disabled:opacity-50"
          >
            {resolvedCancelLabel}
          </button>
          <button
            onClick={onConfirm}
            disabled={isLoading}
            className={`flex-1 btn ${styles.button} disabled:opacity-50 flex items-center justify-center`}
          >
            {isLoading ? (
              <Loader className="w-4 h-4 animate-spin" aria-label={t('confirmDialog.loading')} />
            ) : (
              resolvedConfirmLabel
            )}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * Hook for easier usage of ConfirmDialog.
 *
 * Usage:
 *   const { confirm, ConfirmDialogComponent } = useConfirmDialog();
 *   const handleDelete = async () => {
 *     const ok = await confirm({ title: 'Delete?', message: 'Cannot be undone.' });
 *     if (ok) { ... }
 *   };
 *   return (<><button onClick={handleDelete}>Delete</button>{ConfirmDialogComponent}</>);
 */
export interface ConfirmOptions {
  title?: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: ConfirmVariant;
}

interface ConfirmState {
  isOpen: boolean;
  title: string | null;
  message: string;
  confirmLabel: string | null;
  cancelLabel: string | null;
  variant: ConfirmVariant;
  resolve: ((value: boolean) => void) | null;
}

export interface UseConfirmDialogResult {
  confirm: (options?: ConfirmOptions) => Promise<boolean>;
  ConfirmDialogComponent: ReactElement;
}

export function useConfirmDialog(): UseConfirmDialogResult {
  const [state, setState] = useState<ConfirmState>({
    isOpen: false,
    title: null,
    message: '',
    confirmLabel: null,
    cancelLabel: null,
    variant: 'danger',
    resolve: null,
  });

  const confirm = useCallback((options: ConfirmOptions = {}): Promise<boolean> => {
    return new Promise<boolean>((resolve) => {
      setState({
        isOpen: true,
        title: options.title ?? null,
        message: options.message ?? '',
        confirmLabel: options.confirmLabel ?? null,
        cancelLabel: options.cancelLabel ?? null,
        variant: options.variant ?? 'danger',
        resolve,
      });
    });
  }, []);

  const handleClose = useCallback(() => {
    state.resolve?.(false);
    setState((prev) => ({ ...prev, isOpen: false }));
  }, [state.resolve]);

  const handleConfirm = useCallback(() => {
    state.resolve?.(true);
    setState((prev) => ({ ...prev, isOpen: false }));
  }, [state.resolve]);

  const ConfirmDialogComponent = (
    <ConfirmDialog
      isOpen={state.isOpen}
      onClose={handleClose}
      onConfirm={handleConfirm}
      title={state.title}
      message={state.message}
      confirmLabel={state.confirmLabel}
      cancelLabel={state.cancelLabel}
      variant={state.variant}
    />
  );

  return { confirm, ConfirmDialogComponent };
}
