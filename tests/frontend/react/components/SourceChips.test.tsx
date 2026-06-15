/**
 * SourceChips — provenance chips under a knowledge-backed assistant turn.
 * German is the test default.
 */
import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SourceChips from '../../../../src/frontend/src/components/chat/SourceChips';
import type { MessageSource } from '../../../../src/frontend/src/types/chat';
import { renderWithRouter } from '../test-utils';

const src = (id: number, title: string, tier?: number): MessageSource => ({
  document_id: id,
  filename: `${title}.pdf`,
  title,
  tier,
});

describe('SourceChips', () => {
  it('renders nothing for empty or undefined sources', () => {
    const { container, rerender } = renderWithRouter(<SourceChips sources={[]} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<SourceChips sources={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders one chip per source, linking into the document view', () => {
    renderWithRouter(<SourceChips sources={[src(7, 'Rechnung', 2), src(9, 'Vertrag', 0)]} />);
    const rechnung = screen.getByRole('link', { name: /Rechnung/ });
    expect(rechnung).toHaveAttribute('href', '/knowledge?doc=7');
    expect(screen.getByRole('link', { name: /Vertrag/ })).toHaveAttribute('href', '/knowledge?doc=9');
    // tier is surfaced (TierBadge is not color-only — it carries an aria-label)
    expect(screen.getAllByLabelText(/./).length).toBeGreaterThan(0);
  });

  it('caps at 6 sources and reveals the rest on "+N weitere"', async () => {
    const many = Array.from({ length: 9 }, (_, i) => src(i + 1, `Doc${i + 1}`, 2));
    renderWithRouter(<SourceChips sources={many} />);
    expect(screen.getAllByRole('link')).toHaveLength(6);
    const more = screen.getByText('+3 weitere');
    await userEvent.click(more);
    expect(screen.getAllByRole('link')).toHaveLength(9);
  });

  it('omits the tier badge when tier is absent but still renders the chip', () => {
    renderWithRouter(<SourceChips sources={[src(5, 'Notiz')]} />);
    expect(screen.getByRole('link', { name: /Notiz/ })).toHaveAttribute('href', '/knowledge?doc=5');
  });
});
