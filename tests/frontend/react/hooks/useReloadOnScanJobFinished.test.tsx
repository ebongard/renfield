/**
 * useReloadOnScanJobFinished — a finished background scan reloads the open chat,
 * but never underneath a streaming turn (the reload would clobber it). An event
 * that lands mid-turn must reload exactly once, when the turn ends.
 */
import { describe, it, expect, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import { useReloadOnScanJobFinished } from '../../../../src/frontend/src/hooks/useReloadOnScanJobFinished';
import { SCAN_JOB_FINISHED_EVENT } from '../../../../src/frontend/src/hooks/useUserEvents';

function finish(): void {
  act(() => {
    window.dispatchEvent(new CustomEvent(SCAN_JOB_FINISHED_EVENT, { detail: { reason: 'done' } }));
  });
}

describe('useReloadOnScanJobFinished', () => {
  it('reloads right away when no turn is streaming', () => {
    const reload = vi.fn().mockResolvedValue(undefined);
    renderHook(() => useReloadOnScanJobFinished(false, reload));
    finish();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('defers the reload until the streaming turn ends, then reloads once', () => {
    const reload = vi.fn().mockResolvedValue(undefined);
    const { rerender } = renderHook(
      ({ loading }) => useReloadOnScanJobFinished(loading, reload),
      { initialProps: { loading: true } },
    );

    finish();
    finish(); // two events mid-turn still mean ONE reload afterwards
    expect(reload).not.toHaveBeenCalled();

    rerender({ loading: false });
    expect(reload).toHaveBeenCalledTimes(1);

    rerender({ loading: true });
    rerender({ loading: false });
    expect(reload).toHaveBeenCalledTimes(1); // nothing pending any more
  });

  it('stops listening on unmount', () => {
    const reload = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderHook(() => useReloadOnScanJobFinished(false, reload));
    unmount();
    finish();
    expect(reload).not.toHaveBeenCalled();
  });
});
