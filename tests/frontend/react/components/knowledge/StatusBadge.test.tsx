/**
 * StatusBadge — C2 component tests (#388).
 *
 * Matrix item #23: queue-position sub-label renders when pending with position.
 * Plus supporting coverage for the progressbar vs role=status switch.
 */
import { describe, it, expect } from 'vitest';
import { renderWithProviders } from '../../test-utils';
import StatusBadge, {
  type DocLike,
} from '../../../../../src/frontend/src/components/knowledge/StatusBadge';

function doc(overrides: Partial<DocLike> = {}): DocLike {
  return {
    status: 'pending',
    filename: 'hello.pdf',
    stage: null,
    pages: null,
    queue_position: null,
    ...overrides,
  };
}

describe('StatusBadge', () => {
  it('matrix #23: renders queue_position sub-label when pending', () => {
    const { getByRole, getAllByText } = renderWithProviders(
      <StatusBadge doc={doc({ status: 'pending', queue_position: 3 })} />,
    );
    // Outer element is role=status (pending isn't processing→progressbar).
    expect(getByRole('status')).toBeTruthy();
    // Queue sub-label uses statusQueuePosition key → DE: "Platz 3".
    // It can appear twice: once visibly in the <span>, once in the sr-only
    // live region after the rate-limit timer elapses.
    const matches = getAllByText(/Platz 3|Position 3/);
    expect(matches.length).toBeGreaterThanOrEqual(1);
  });

  it('uses role=progressbar when processing with pages.total', () => {
    const { getByRole } = renderWithProviders(
      <StatusBadge
        doc={doc({
          status: 'processing',
          stage: 'ocr',
          pages: { current: 47, total: 120 },
        })}
      />,
    );
    const bar = getByRole('progressbar');
    expect(bar.getAttribute('aria-valuenow')).toBe('47');
    expect(bar.getAttribute('aria-valuemax')).toBe('120');
    expect(bar.getAttribute('aria-valuetext')).toMatch(/47/);
  });

  it('falls back to aria-busy when processing without page counts', () => {
    const { getByRole } = renderWithProviders(
      <StatusBadge doc={doc({ status: 'processing', stage: 'parsing' })} />,
    );
    const el = getByRole('status');
    expect(el.getAttribute('aria-busy')).toBe('true');
  });

  it('drops aria-busy for non-processing rows', () => {
    const { getByRole } = renderWithProviders(
      <StatusBadge doc={doc({ status: 'completed' })} />,
    );
    expect(getByRole('status').getAttribute('aria-busy')).toBeFalsy();
  });
});
