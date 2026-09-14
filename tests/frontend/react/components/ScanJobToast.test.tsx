/**
 * ScanJobToast — the live notice that a background scan finished. It must appear
 * on the window event useUserEvents dispatches, word itself by outcome, never show
 * anything it was not told, and get out of the way on its own.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

import ScanJobToast from '../../../../src/frontend/src/components/ScanJobToast';
import { SCAN_JOB_FINISHED_EVENT } from '../../../../src/frontend/src/hooks/useUserEvents';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function announce(reason?: string): void {
  act(() => {
    window.dispatchEvent(new CustomEvent(SCAN_JOB_FINISHED_EVENT, { detail: { reason } }));
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe('ScanJobToast', () => {
  it('renders nothing until a scan finishes', () => {
    render(<ScanJobToast />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it.each(['done', 'unrouted', 'failed', 'interrupted'])('words the %s outcome', (reason) => {
    render(<ScanJobToast />);
    announce(reason);
    expect(screen.getByRole('status')).toHaveTextContent(`notifications.scanJob.${reason}`);
  });

  it('treats an unexpected reason as a plain "done", never as a failure it was not told', () => {
    render(<ScanJobToast />);
    announce('something-new');
    expect(screen.getByRole('status')).toHaveTextContent('notifications.scanJob.done');
  });

  it('dismisses on click', () => {
    render(<ScanJobToast />);
    announce('done');
    fireEvent.click(screen.getByRole('button', { name: 'notifications.dismiss' }));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('dismisses itself after a while', () => {
    vi.useFakeTimers();
    render(<ScanJobToast />);
    announce('done');
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(screen.queryByRole('status')).toBeNull();
  });
});
