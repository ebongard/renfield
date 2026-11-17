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
import i18n from '../../../../src/frontend/src/i18n';

const PAPERLESS = 'https://paperless.example.org';

const paperless = (id: number, title: string, withLink = true): MessageSource => ({
  kind: 'paperless',
  paperless_document_id: id,
  title,
  ...(withLink ? { paperless_url: `${PAPERLESS}/documents/${id}/details` } : {}),
});

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

  it('moves the focus to the first revealed chip, not to the page', async () => {
    const many = Array.from({ length: 9 }, (_, i) => src(i + 1, `Doc${i + 1}`, 2));
    renderWithRouter(<SourceChips sources={many} />);
    screen.getByText('+3 weitere').focus();
    await userEvent.keyboard('{Enter}');
    expect(document.activeElement).toBe(screen.getByRole('link', { name: /Doc7/ }));
  });

  it('moves the focus to a revealed chip that opens nothing, without adding it to the tab order', async () => {
    const sources = [
      ...Array.from({ length: 6 }, (_, i) => src(i + 1, `Doc${i + 1}`)),
      paperless(70, 'Ohne Link', false),
      src(8, 'Doc8'),
    ];
    renderWithRouter(<SourceChips sources={sources} />);
    await userEvent.click(screen.getByText('+2 weitere'));
    const target = screen.getByText('Ohne Link').parentElement as HTMLElement;
    expect(document.activeElement).toBe(target);
    expect(target).toHaveAttribute('tabindex', '-1');
  });

  it('omits the tier badge when tier is absent but still renders the chip', () => {
    renderWithRouter(<SourceChips sources={[src(5, 'Notiz')]} />);
    expect(screen.getByRole('link', { name: /Notiz/ })).toHaveAttribute('href', '/knowledge?doc=5');
  });

  // ---- CHAT_DOCUMENT_LINKS_ENABLED: a chip opens its document -------------

  it('opens a Paperless document in a new tab, as a real anchor', () => {
    renderWithRouter(<SourceChips sources={[paperless(11, 'Rechnung')]} />);
    const link = screen.getByRole('link', { name: 'Rechnung – in Paperless öffnen (neuer Tab)' });
    expect(link.tagName).toBe('A');
    expect(link).toHaveAttribute('href', `${PAPERLESS}/documents/11/details`);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveTextContent('Rechnung');
  });

  it('opens a knowledge-base document in the document view, by its id', () => {
    renderWithRouter(<SourceChips sources={[src(7, 'Bescheid', 1)]} />);
    const link = screen.getByRole('link', { name: /Bescheid/ });
    expect(link).toHaveAttribute('href', '/knowledge?doc=7');
    // In-app navigation: same tab.
    expect(link).not.toHaveAttribute('target');
    expect(screen.getAllByRole('link')).toHaveLength(1);
  });

  it('gives a filed knowledge-base document both targets', () => {
    const filed: MessageSource = {
      ...src(7, 'Bescheid', 1),
      paperless_url: `${PAPERLESS}/documents/70/details`,
    };
    renderWithRouter(<SourceChips sources={[filed]} />);
    const links = screen.getAllByRole('link');
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute('href', '/knowledge?doc=7');
    expect(links[1]).toHaveAttribute('href', `${PAPERLESS}/documents/70/details`);
    expect(links[1]).toHaveAttribute('target', '_blank');
    expect(links[1]).toHaveAttribute('rel', 'noopener noreferrer');
    expect(links[1]).toHaveAccessibleName('Bescheid – in Paperless öffnen (neuer Tab)');
  });

  it('shows a Paperless document without a link as plain text, not as a dead control', () => {
    renderWithRouter(<SourceChips sources={[paperless(11, 'Rechnung', false)]} />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText('Rechnung')).toBeInTheDocument();
    // The reason is announced, not only shown on hover.
    expect(screen.getByText('In Paperless gefunden – kein Link verfügbar')).toBeInTheDocument();
  });

  it.each([
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    '//evil.example/x',
    '/documents/11/details',
    '',
  ])('never turns a non-http(s) target into a link: %s', (target) => {
    const kb: MessageSource = { ...src(7, 'Bescheid'), paperless_url: target };
    const pl: MessageSource = { kind: 'paperless', paperless_document_id: 11, title: 'Rechnung', paperless_url: target };
    const { container } = renderWithRouter(<SourceChips sources={[kb, pl]} />);
    // Only the in-app link of the knowledge-base chip is left.
    const hrefs = Array.from(container.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    expect(hrefs).toEqual(['/knowledge?doc=7']);
  });

  it('renders a hostile title as text', () => {
    const { container } = renderWithRouter(
      <SourceChips sources={[paperless(11, '<img src=x onerror=alert(1)>')]} />,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
  });

  it('labels an untitled Paperless document from the translations', () => {
    renderWithRouter(<SourceChips sources={[paperless(11, '')]} />);
    expect(screen.getByRole('link')).toHaveAccessibleName(
      'Paperless-Dokument 11 – in Paperless öffnen (neuer Tab)',
    );
  });

  it('keeps one chip per document when both systems use the same number', () => {
    // Knowledge-base document 7 and Paperless document 7 are different documents.
    renderWithRouter(<SourceChips sources={[src(7, 'Bescheid'), paperless(7, 'Rechnung')]} />);
    expect(screen.getByRole('link', { name: /Bescheid/ })).toHaveAttribute('href', '/knowledge?doc=7');
    expect(screen.getByRole('link', { name: /Rechnung/ })).toHaveAttribute(
      'href', `${PAPERLESS}/documents/7/details`,
    );
  });

  it('carries dark-mode classes on every new chip form', () => {
    const filed: MessageSource = { ...src(7, 'Bescheid'), paperless_url: `${PAPERLESS}/documents/70/details` };
    renderWithRouter(
      <SourceChips sources={[paperless(11, 'Rechnung'), paperless(12, 'Vertrag', false), filed]} />,
    );
    const linked = screen.getByRole('link', { name: /Rechnung/ });
    const plain = screen.getByText('Vertrag').parentElement as HTMLElement;
    const segment = screen.getByRole('link', { name: 'Bescheid – in Paperless öffnen (neuer Tab)' });
    for (const el of [linked, plain, segment]) {
      expect(el.className).toMatch(/\bbg-gray-200\b/);
      expect(el.className).toMatch(/\bdark:bg-gray-600\b/);
      expect(el.className).toMatch(/\btext-gray-700\b/);
      expect(el.className).toMatch(/\bdark:text-gray-200\b/);
    }
    // The plain one is not focusable (the ring itself: see the block below).
    expect(plain).not.toHaveAttribute('tabindex');
  });

  it('labels a knowledge-base document without title and filename from the translations', () => {
    const nameless: MessageSource = { document_id: 7, filename: '', title: '' };
    renderWithRouter(<SourceChips sources={[nameless]} />);
    const link = screen.getByRole('link');
    expect(link).toHaveTextContent('Dokument 7');
    expect(link).toHaveAttribute('title', 'Dokument 7');
    expect(i18n.t('chat.sources.untitled', { lng: 'en', id: 7 })).toBe('Document 7');
  });

  it('keeps a stored title as it is, also an old literal one', () => {
    renderWithRouter(<SourceChips sources={[{ document_id: 7, filename: 'f.pdf', title: 'Dokument 123' }]} />);
    expect(screen.getByRole('link')).toHaveTextContent('Dokument 123');
  });

  // jsdom has no layout: these tests prove that the classes are ON the right
  // elements, not what the area measures or that neighbours do not overlap.
  // That needs a browser.
  describe('hit area and focus ring (owner-approved deviations from DESIGN.md)', () => {
    const HIT_AREA = [
      'relative', 'after:absolute', 'after:top-[-3px]', 'after:bottom-[-3px]',
      'after:left-1/2', 'after:-translate-x-1/2', 'after:w-full', 'after:min-w-[44px]',
    ];
    const RING = [
      'focus-visible:outline-2', 'focus-visible:outline-offset-2',
      'focus-visible:outline-primary-500',
    ];
    const has = (el: Element, cls: string) => el.classList.contains(cls);

    function renderAll() {
      const filed: MessageSource = { ...src(7, 'Bescheid'), paperless_url: `${PAPERLESS}/documents/70/details` };
      const many = Array.from({ length: 6 }, (_, i) => src(100 + i, `Doc${i}`));
      return renderWithRouter(
        <SourceChips sources={[paperless(11, 'Rechnung'), paperless(12, 'Vertrag', false), filed, ...many]} />,
      );
    }

    it('gives every interactive element of the row the invisible hit area', () => {
      const { container } = renderAll();
      const interactive = Array.from(container.querySelectorAll('a, button'));
      // Paperless chip, KB chip + its Paperless segment, 3 more KB chips, "+N".
      expect(interactive).toHaveLength(7);
      expect(interactive.some((el) => el.tagName === 'BUTTON')).toBe(true);
      for (const el of interactive) {
        for (const cls of HIT_AREA) expect(has(el, cls), `${el.textContent}: ${cls}`).toBe(true);
      }
    });

    it('gives a chip that opens nothing no hit area', () => {
      renderAll();
      const plain = screen.getByText('Vertrag').parentElement as HTMLElement;
      expect(plain.tagName).toBe('SPAN');
      expect(plain.className).not.toMatch(/after:|\brelative\b/);
    });

    it('leaves the row spacing as it was and adds nothing that takes space', () => {
      const { container } = renderAll();
      const row = container.firstElementChild as HTMLElement;
      // The row's classes before the hit areas, plus `isolate` (z-index scope
      // of the areas, no layout effect).
      expect(Array.from(row.classList).sort()).toEqual(
        ['flex', 'flex-wrap', 'gap-1.5', 'isolate', 'items-center', 'mt-2'].sort(),
      );
      for (const el of Array.from(container.querySelectorAll('a, button'))) {
        const cls = el.getAttribute('class') ?? '';
        // Behind the visible chips; no minimum height on the element or its
        // area -- the area is 3 px (half the 6 px gap) taller on each side.
        expect(has(el, 'after:-z-10')).toBe(true);
        expect(cls).not.toMatch(/min-h-/);
      }
    });

    it('uses one focus ring colour on every focusable element of the row', () => {
      const { container } = renderAll();
      for (const el of Array.from(container.querySelectorAll('a, button'))) {
        for (const cls of RING) expect(has(el, cls), `${el.textContent}: ${cls}`).toBe(true);
      }
      for (const el of Array.from(container.querySelectorAll('*'))) {
        const cls = el.getAttribute('class') ?? '';
        expect(cls).not.toMatch(/(ring|outline)-accent-/);
        expect(cls).not.toMatch(/focus-visible:ring-/);
        // Tailwind v4: `outline-none` zeroes the outline style the ring needs.
        expect(cls).not.toMatch(/outline-none/);
      }
    });
  });

  it('has every new string in all four languages, none of them the key', () => {
    for (const lng of ['de', 'en', 'it', 'fr']) {
      for (const key of ['paperlessUntitled', 'untitled', 'openInPaperless', 'noLink']) {
        const text = i18n.t(`chat.sources.${key}`, { lng, id: 3, title: 'T' });
        expect(text, `${lng}:${key}`).not.toContain('chat.sources');
        expect(text.length, `${lng}:${key}`).toBeGreaterThan(3);
      }
    }
    expect(i18n.t('chat.sources.openInPaperless', { lng: 'en', title: 'T' })).toBe(
      'T – open in Paperless (new tab)',
    );
  });
});
