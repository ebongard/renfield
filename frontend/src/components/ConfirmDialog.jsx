/**
 * ConfirmDialog Component
 *
 * Accessible replacement for native confirm() dialogs.
 * Supports customizable title, message, and button labels.
 */

import React from 'react';
import Modal from './Modal';
import { AlertTriangle, Loader } from 'lucide-react';

export default function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title = 'Bestätigung',
  message,
  confirmLabel = 'Bestätigen',
  cancelLabel = 'Abbrechen',
  variant = 'danger', // 'danger' | 'warning' | 'info'
  isLoading = false,
}) {
  const handleConfirm = () => {
    onConfirm();
  };

  const variantStyles = {
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

  const styles = variantStyles[variant] || variantStyles.danger;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      showCloseButton={false}
      closeOnOverlayClick={!isLoading}
      closeOnEscape={!isLoading}
    >
      <div className="text-center">
        {/* Icon */}
        <div className={`w-16 h-16 mx-auto mb-4 rounded-full flex items-center justify-center ${styles.icon}`}>
          <AlertTriangle className="w-8 h-8" aria-hidden="true" />
        </div>

        {/* Title */}
        <h2 id="confirm-title" className="text-xl font-bold text-white mb-2">
          {title}
        </h2>

        {/* Message */}
        {message && (
          <p className="text-gray-400 mb-6">
            {message}
          </p>
        )}

        {/* Buttons */}
        <div className="flex space-x-3">
          <button
            onClick={onClose}
            disabled={isLoading}
            className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            onClick={handleConfirm}
            disabled={isLoading}
            className={`flex-1 btn ${styles.button} disabled:opacity-50 flex items-center justify-center`}
          >
            {isLoading ? (
              <Loader className="w-4 h-4 animate-spin" aria-label="Wird ausgeführt..." />
            ) : (
              confirmLabel
            )}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * Hook for easier usage of ConfirmDialog
 *
 * Usage:
 * const { confirm, ConfirmDialogComponent } = useConfirmDialog();
 *
 * const handleDelete = async () => {
 *   const confirmed = await confirm({
 *     title: 'Löschen?',
 *     message: 'Diese Aktion kann nicht rückgängig gemacht werden.',
 *   });
 *   if (confirmed) {
 *     // do delete
 *   }
 * };
 *
 * return (
 *   <>
 *     <button onClick={handleDelete}>Löschen</button>
 *     {ConfirmDialogComponent}
 *   </>
 * );
 */
import { useState, useCallback } from 'react';

export function useConfirmDialog() {
  const [state, setState] = useState({
    isOpen: false,
    title: 'Bestätigung',
    message: '',
    confirmLabel: 'Bestätigen',
    cancelLabel: 'Abbrechen',
    variant: 'danger',
    resolve: null,
  });

  const confirm = useCallback((options = {}) => {
    return new Promise((resolve) => {
      setState({
        isOpen: true,
        title: options.title || 'Bestätigung',
        message: options.message || '',
        confirmLabel: options.confirmLabel || 'Bestätigen',
        cancelLabel: options.cancelLabel || 'Abbrechen',
        variant: options.variant || 'danger',
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
