/**
 * Modal Component with Accessibility Features
 *
 * Features:
 * - Focus trap (focus stays within modal)
 * - Escape key to close
 * - Click outside to close (optional)
 * - Proper ARIA attributes
 * - Focus restoration on close
 */

import { KeyboardEvent, MouseEvent, ReactNode, useCallback, useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

export interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title?: ReactNode;
  children?: ReactNode;
  showCloseButton?: boolean;
  closeOnEscape?: boolean;
  closeOnOverlayClick?: boolean;
  maxWidth?: string;
  className?: string;
}

export default function Modal({
  isOpen,
  onClose,
  title,
  children,
  showCloseButton = true,
  closeOnEscape = true,
  closeOnOverlayClick = true,
  maxWidth = 'max-w-md',
  className = '',
}: ModalProps) {
  const modalRef = useRef<HTMLDivElement | null>(null);
  const previousActiveElement = useRef<HTMLElement | null>(null);

  // Store the previously focused element and focus the modal
  useEffect(() => {
    if (isOpen) {
      previousActiveElement.current = document.activeElement as HTMLElement | null;
      // Small delay to ensure modal is rendered
      setTimeout(() => {
        modalRef.current?.focus();
      }, 10);
    } else if (previousActiveElement.current) {
      // Restore focus when modal closes
      previousActiveElement.current.focus();
    }
  }, [isOpen]);

  // Handle escape key
  useEffect(() => {
    if (!isOpen || !closeOnEscape) return;

    const handleEscape = (e: globalThis.KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };

    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isOpen, closeOnEscape, onClose]);

  // Prevent body scroll when modal is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [isOpen]);

  // Focus trap - keep focus within modal
  const handleKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== 'Tab' || !modalRef.current) return;

    const focusableElements = modalRef.current.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    const firstElement = focusableElements[0];
    const lastElement = focusableElements[focusableElements.length - 1];

    if (!firstElement) return;

    if (e.shiftKey) {
      if (document.activeElement === firstElement || document.activeElement === modalRef.current) {
        e.preventDefault();
        lastElement?.focus();
      }
    } else {
      if (document.activeElement === lastElement) {
        e.preventDefault();
        firstElement?.focus();
      }
    }
  }, []);

  // Handle overlay click
  const handleOverlayClick = useCallback((e: MouseEvent<HTMLDivElement>): void => {
    if (closeOnOverlayClick && e.target === e.currentTarget) {
      onClose();
    }
  }, [closeOnOverlayClick, onClose]);

  if (!isOpen) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 overflow-y-auto bg-black/50"
      onClick={handleOverlayClick}
      aria-hidden={!isOpen}
    >
      <div className="flex min-h-full items-center justify-center p-4">
        <div
          ref={modalRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? 'modal-title' : undefined}
          tabIndex={-1}
          onKeyDown={handleKeyDown}
          onClick={(e) => e.stopPropagation()}
          className={`card ${maxWidth} w-full outline-hidden ${className}`}
        >
          {(title || showCloseButton) && (
            <div className="flex items-center justify-between mb-4 shrink-0">
              {title && (
                <h2 id="modal-title" className="text-xl font-bold text-gray-900 dark:text-white">
                  {title}
                </h2>
              )}
              {showCloseButton && (
                <button
                  onClick={onClose}
                  className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors ml-auto"
                  aria-label="Dialog schließen"
                >
                  <X className="w-5 h-5 text-gray-400" aria-hidden="true" />
                </button>
              )}
            </div>
          )}

          {children}
        </div>
      </div>
    </div>,
    document.body,
  );
}
