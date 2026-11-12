/**
 * The date picker of the Paperless review follows the interface language.
 *
 * The month caption is the visible proof: it is rendered by date-fns from the
 * locale object the popover picks, so a language missing from that mapping
 * shows English month names inside an otherwise translated page.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { CalendarPopover } from '../../../../src/frontend/src/components/paperless/reviewControls';
import i18n from '../../../../src/frontend/src/i18n';

function renderCalendar(locale: string) {
  return render(
    <CalendarPopover
      value="2026-03-15"
      ariaLabel="date"
      locale={locale}
      t={i18n.getFixedT(locale)}
      onCommit={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
}

describe('CalendarPopover locale', () => {
  it.each([
    ['de', 'März 2026'],
    ['it', 'marzo 2026'],
    ['fr', 'mars 2026'],
    ['fr-CA', 'mars 2026'],
    ['en', 'March 2026'],
  ])('shows the month caption for %s as "%s"', async (locale, caption) => {
    renderCalendar(locale);
    expect(await screen.findByText(caption)).toBeInTheDocument();
  });

  it('falls back to English for a language without a calendar locale', async () => {
    renderCalendar('pt');
    expect(await screen.findByText('March 2026')).toBeInTheDocument();
  });
});
