/**
 * ScanJobToast — the live notice that a background scan finished. It must appear
 * on the window event useUserEvents dispatches, word itself by outcome, never show
 * anything it was not told, and get out of the way on its own.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

import ScanJobToast from '../../../../src/frontend/src/components/ScanJobToast';
import { SCAN_JOB_FINISHED_EVENT } from '../../../../src/frontend/src/hooks/useUserEvents';
import {
  rememberTabConversation,
  resetTabConversationsForTests,
} from '../../../../src/frontend/src/utils/tabConversations';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function announce(reason?: string, sessionId?: string): void {
  act(() => {
    window.dispatchEvent(
      new CustomEvent(SCAN_JOB_FINISHED_EVENT, {
        detail: { reason, ...(sessionId ? { sessionId } : {}) },
      }),
    );
  });
}

afterEach(() => {
  vi.useRealTimers();
  window.sessionStorage.clear();
  resetTabConversationsForTests();
});

describe('ScanJobToast — only in the tab that asked', () => {
  it('shows in the tab that wrote into the scan’s conversation', () => {
    rememberTabConversation('session-mine');
    render(<ScanJobToast />);
    announce('done', 'session-mine');
    expect(screen.getByRole('status')).toHaveTextContent('notifications.scanJob.done');
  });

  it('stays quiet in every other tab (auth-off: all tabs get the event)', () => {
    rememberTabConversation('session-mine');
    render(<ScanJobToast />);
    announce('done', 'session-of-another-tab');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('survives a reload of the same tab (per-tab session storage)', () => {
    rememberTabConversation('session-mine');
    resetTabConversationsForTests(); // the in-memory copy is gone, storage is not
    render(<ScanJobToast />);
    announce('failed', 'session-mine');
    expect(screen.getByRole('status')).toHaveTextContent('notifications.scanJob.failed');
  });
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

  it('shows nothing for a reason it does not know, rather than claiming success', () => {
    render(<ScanJobToast />);
    announce('something-new');
    expect(screen.queryByRole('status')).toBeNull();
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
