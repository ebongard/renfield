/**
 * DuplicateDialog — C2 component tests (#388).
 *
 * Focus trap + return focus on close are the C2 additions.
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent } from '@testing-library/react';
import { renderWithProviders } from '../../test-utils';
import DuplicateDialog, {
  type ExistingDocument,
} from '../../../../../src/frontend/src/components/knowledge/DuplicateDialog';

const existing: ExistingDocument = {
  id: 42,
  filename: 'report.pdf',
  uploaded_at: '2026-04-01T12:00:00Z',
};

describe('DuplicateDialog', () => {
  it('focuses the jump button on open', () => {
    const { getByRole } = renderWithProviders(
      <DuplicateDialog existing={existing} onClose={vi.fn()} onJump={vi.fn()} />,
    );
    const dialog = getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    // Primary button text per DE i18n: "Zum vorhandenen Eintrag springen"
    const jumpBtn = dialog.querySelector<HTMLButtonElement>('.btn-primary');
    expect(document.activeElement).toBe(jumpBtn);
  });

  it('returns focus to the previously focused element on close', () => {
    const trigger = document.createElement('button');
    trigger.textContent = 'upload trigger';
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    const onClose = vi.fn<() => void>();
    const { unmount } = renderWithProviders(
      <DuplicateDialog existing={existing} onClose={onClose} onJump={vi.fn()} />,
    );
    // Dialog stole focus.
    expect(document.activeElement).not.toBe(trigger);

    // Close → focus returns to the trigger (useEffect cleanup runs on unmount).
    unmount();
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it('Escape closes the dialog', () => {
    const onClose = vi.fn<() => void>();
    renderWithProviders(
      <DuplicateDialog existing={existing} onClose={onClose} onJump={vi.fn()} />,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });

  it('tab cycles within the dialog (focus trap)', () => {
    const { getByRole } = renderWithProviders(
      <DuplicateDialog existing={existing} onClose={vi.fn()} onJump={vi.fn()} />,
    );
    const dialog = getByRole('dialog');
    const cancelBtn = dialog.querySelector<HTMLButtonElement>('.btn-secondary');
    const jumpBtn = dialog.querySelector<HTMLButtonElement>('.btn-primary');

    // Focus starts on jumpBtn (last focusable in DOM order).
    expect(document.activeElement).toBe(jumpBtn);

    // Tab forward on last focusable should wrap to first (cancel).
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(cancelBtn);

    // Shift+Tab on first should wrap to last (jump).
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(jumpBtn);
  });
});
