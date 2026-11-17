/**
 * DocumentCards — one card per document a chat answer drew on.
 * German is the test default.
 *
 * jsdom has no layout: width, button height and the focus ring are measured in
 * a real browser, not here. These tests hold what a card SHOWS and what it
 * links to.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DocumentCards from '../../../../src/frontend/src/components/chat/DocumentCards';
import type {
  KnowledgeMessageSource,
  MessageSource,
  PaperlessMessageSource,
} from '../../../../src/frontend/src/types/chat';
import { renderWithRouter } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';

const PAPERLESS = 'https://paperless.example.org';

const kb = (id: number, title: string, extra: Partial<KnowledgeMessageSource> = {}): MessageSource => ({
  document_id: id,
  filename: `${title}.pdf`,
  title,
  ...extra,
});

const paperless = (
  id: number,
  title: string,
  extra: Partial<PaperlessMessageSource> = {},
  withLink = true,
): MessageSource => ({
  kind: 'paperless',
  paperless_document_id: id,
  title,
  ...(withLink ? { paperless_url: `${PAPERLESS}/documents/${id}/details` } : {}),
  ...extra,
});

const cards = () => screen.getAllByRole('listitem');

/** The meta line of a card as one string (each part sits in its own <bdi>). */
const metaLine = (card: HTMLElement) => card.querySelector('[data-card-meta]')?.textContent ?? null;

afterEach(async () => {
  await i18n.changeLanguage('de');
});

describe('DocumentCards', () => {
  it('renders nothing for empty or undefined sources', () => {
    const { container, rerender } = renderWithRouter(<DocumentCards sources={[]} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<DocumentCards sources={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows a knowledge-base document with title, tier, date and pages, and opens it in the app', () => {
    renderWithRouter(
      <DocumentCards
        sources={[kb(7, 'Steuerbescheid 2024', { tier: 0, document_date: '2025-03-14', page_count: 3 })]}
      />,
    );
    const [card] = cards();
    expect(within(card).getByText('Steuerbescheid 2024')).toBeInTheDocument();
    // TierBadge: colour + symbol + label, announced.
    expect(within(card).getByLabelText(/Stufe|Tier/i)).toBeInTheDocument();
    expect(metaLine(card)).toBe('14.03.2025 · 3 S.');

    const links = within(card).getAllByRole('link');
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveTextContent('Öffnen');
    expect(links[0]).toHaveAccessibleName('Steuerbescheid 2024 öffnen');
    expect(links[0]).toHaveAttribute('href', '/knowledge?doc=7');
    // In-app navigation: same tab.
    expect(links[0]).not.toHaveAttribute('target');
  });

  it('gives a filed knowledge-base document ONE card with both buttons', () => {
    renderWithRouter(
      <DocumentCards
        sources={[kb(7, 'Bescheid', { tier: 1, paperless_url: `${PAPERLESS}/documents/70/details` })]}
      />,
    );
    expect(cards()).toHaveLength(1);
    const [open, external] = screen.getAllByRole('link');
    expect(open).toHaveAttribute('href', '/knowledge?doc=7');
    expect(external).toHaveAttribute('href', `${PAPERLESS}/documents/70/details`);
    expect(external).toHaveAttribute('target', '_blank');
    expect(external).toHaveAttribute('rel', 'noopener noreferrer');
    expect(external).toHaveTextContent('In Paperless öffnen');
    expect(external).toHaveAccessibleName('Bescheid – in Paperless öffnen (neuer Tab)');
    // The external-link icon is visible, and decoration only.
    const icon = external.querySelector('svg');
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute('aria-hidden', 'true');
  });

  it('shows a Paperless document with date, type, correspondent and pages and one button', () => {
    renderWithRouter(
      <DocumentCards
        sources={[
          paperless(11, 'Rechnung Stadtwerke', {
            created: '2025-02-02', document_type: 'Rechnung', correspondent: 'Stadtwerke', page_count: 2,
          }),
        ]}
      />,
    );
    const [card] = cards();
    expect(metaLine(card)).toBe('02.02.2025 · Rechnung · Stadtwerke · 2 S.');
    const links = within(card).getAllByRole('link');
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute('href', `${PAPERLESS}/documents/11/details`);
    expect(links[0]).toHaveAttribute('target', '_blank');
    expect(links[0]).toHaveAttribute('rel', 'noopener noreferrer');
    expect(links[0]).toHaveAccessibleName('Rechnung Stadtwerke – in Paperless öffnen (neuer Tab)');
    // No tier: no badge, and the neutral row instead of a tier stripe.
    expect(within(card).queryByLabelText(/Stufe|Tier/i)).not.toBeInTheDocument();
    expect(card.className).not.toMatch(/tier-ring-/);
  });

  it('joins only the parts of the meta line that exist', () => {
    renderWithRouter(
      <DocumentCards
        sources={[
          paperless(11, 'Nur Typ', { document_type: 'Vertrag' }),
          paperless(12, 'Datum und Absender', { created: '2025-01-05', correspondent: 'Amt' }),
          kb(7, 'Nur Seiten', { page_count: 1 }),
        ]}
      />,
    );
    const [typeOnly, dateAndSender, pagesOnly] = cards();
    expect(metaLine(typeOnly)).toBe('Vertrag');
    expect(metaLine(dateAndSender)).toBe('05.01.2025 · Amt');
    expect(metaLine(pagesOnly)).toBe('1 S.');
    expect(document.body.textContent).not.toMatch(/· ·|·\s*$/);
  });

  it('renders a source stored before cards existed as a card with its title only', () => {
    renderWithRouter(<DocumentCards sources={[kb(7, 'Alt', { tier: 2 }), paperless(11, 'Auch alt')]} />);
    const [first, second] = cards();
    expect(within(first).getByText('Alt')).toBeInTheDocument();
    expect(within(second).getByText('Auch alt')).toBeInTheDocument();
    for (const card of [first, second]) {
      expect(card.querySelector('[data-card-meta]')).toBeNull();
      expect(card.textContent).not.toContain('·');
    }
  });

  it('leaves out a field whose value is not what the server writes', () => {
    const odd = {
      document_id: 7, filename: 'f.pdf', title: 'Seltsam',
      document_date: '14.03.2025', page_count: 0,
    } as MessageSource;
    const odd2 = {
      kind: 'paperless', paperless_document_id: 11, title: 'Auch seltsam',
      created: '2025-02-02T10:00:00Z', correspondent: '   ', document_type: 7, page_count: 2.5,
    } as unknown as MessageSource;
    renderWithRouter(<DocumentCards sources={[odd, odd2]} />);
    for (const card of cards()) {
      expect(card.querySelector('[data-card-meta]')).toBeNull();
    }
  });

  it('shows a Paperless document without a link as a card that is not a control', () => {
    renderWithRouter(
      <DocumentCards sources={[paperless(11, 'Vertrag alt', { created: '2024-06-01' }, false)]} />,
    );
    const [card] = cards();
    expect(within(card).getByText('Vertrag alt')).toBeInTheDocument();
    expect(metaLine(card)).toBe('01.06.2024');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    // Nothing in it can take the focus.
    expect(card.querySelector('[tabindex], a, button')).toBeNull();
    // The reason is said, not left to a hover.
    expect(within(card).getByText('In Paperless gefunden – kein Link verfügbar')).toBeInTheDocument();
  });

  it('labels an untitled document from the translations', () => {
    renderWithRouter(
      <DocumentCards
        sources={[{ document_id: 7, filename: '', title: '' }, paperless(11, '')]}
      />,
    );
    const [first, second] = cards();
    expect(within(first).getByText('Dokument 7')).toBeInTheDocument();
    expect(within(first).getByRole('link')).toHaveAccessibleName('Dokument 7 öffnen');
    expect(within(second).getByText('Paperless-Dokument 11')).toBeInTheDocument();
  });

  it('falls back to the filename when a knowledge-base document has no title', () => {
    renderWithRouter(<DocumentCards sources={[{ document_id: 7, filename: 'scan.pdf', title: '' }]} />);
    expect(screen.getByText('scan.pdf')).toBeInTheDocument();
  });

  it('shows three cards and reveals the rest on "+N weitere"', async () => {
    const many = Array.from({ length: 5 }, (_, i) => kb(i + 1, `Doc${i + 1}`, { tier: 2 }));
    renderWithRouter(<DocumentCards sources={many} />);
    expect(cards()).toHaveLength(3);
    const more = screen.getByRole('button', { name: '+2 weitere' });
    await userEvent.click(more);
    expect(cards()).toHaveLength(5);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('moves the focus to the first revealed card\'s button, not to the page', async () => {
    const many = Array.from({ length: 5 }, (_, i) => kb(i + 1, `Doc${i + 1}`, { tier: 2 }));
    renderWithRouter(<DocumentCards sources={many} />);
    const more = screen.getByRole('button', { name: '+2 weitere' });
    more.focus();
    await userEvent.keyboard('{Enter}');
    expect(document.activeElement).toBe(screen.getByRole('link', { name: 'Doc4 öffnen' }));
  });

  it('moves the focus to the revealed card itself when it has no button', async () => {
    const sources = [
      ...Array.from({ length: 3 }, (_, i) => kb(i + 1, `Doc${i + 1}`)),
      paperless(40, 'Ohne Link', {}, false),
      kb(5, 'Doc5'),
    ];
    renderWithRouter(<DocumentCards sources={sources} />);
    await userEvent.click(screen.getByRole('button', { name: '+2 weitere' }));
    const target = cards()[3];
    expect(document.activeElement).toBe(target);
    // Reachable by script only -- it does not join the tab order.
    expect(target).toHaveAttribute('tabindex', '-1');
    // The cards shown from the start are untouched.
    for (const card of cards().slice(0, 3)) expect(card).not.toHaveAttribute('tabindex');
  });

  it('puts each part of the meta line in its own <bdi>', () => {
    renderWithRouter(
      <DocumentCards
        sources={[paperless(11, 'Rechnung', { created: '2025-02-02', correspondent: 'Amt', page_count: 2 })]}
      />,
    );
    const parts = Array.from(cards()[0].querySelectorAll('[data-card-meta] bdi')).map((b) => b.textContent);
    expect(parts).toEqual(['02.02.2025', 'Amt', '2 S.']);
  });

  it('shows no "+N" control for three documents', () => {
    const three = Array.from({ length: 3 }, (_, i) => kb(i + 1, `Doc${i + 1}`));
    renderWithRouter(<DocumentCards sources={three} />);
    expect(cards()).toHaveLength(3);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it.each([
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    '//evil.example/x',
    '/documents/11/details',
    '',
  ])('never turns a non-http(s) target into a button: %s', (target) => {
    const { container } = renderWithRouter(
      <DocumentCards
        sources={[kb(7, 'Bescheid', { paperless_url: target }), paperless(11, 'Rechnung', { paperless_url: target })]}
      />,
    );
    const hrefs = Array.from(container.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    expect(hrefs).toEqual(['/knowledge?doc=7']);
  });

  it.each(['7; drop', '1e3', '-4', '0', '7.5', ''])(
    'builds no in-app link from an id that is not a positive integer: %s',
    (id) => {
      const { container } = renderWithRouter(
        <DocumentCards sources={[{ document_id: id, filename: 'f.pdf', title: 'Bescheid' }]} />,
      );
      expect(container.querySelector('a')).toBeNull();
      expect(screen.getByText('Bescheid')).toBeInTheDocument();
    },
  );

  it('renders hostile text as text, in the title and in the meta line', () => {
    const evil = '<img src=x onerror=alert(1)>';
    const { container } = renderWithRouter(
      <DocumentCards sources={[paperless(11, evil, { correspondent: evil })]} />,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getAllByText(evil)).toHaveLength(2);
  });

  it('keeps one card per document when both systems use the same number', () => {
    renderWithRouter(<DocumentCards sources={[kb(7, 'Bescheid'), paperless(7, 'Rechnung')]} />);
    expect(cards()).toHaveLength(2);
    expect(screen.getByRole('link', { name: /Bescheid/ })).toHaveAttribute('href', '/knowledge?doc=7');
    expect(screen.getByRole('link', { name: /Rechnung/ })).toHaveAttribute(
      'href', `${PAPERLESS}/documents/7/details`,
    );
  });

  it('uses the list-row pattern, the tier stripe and the button classes of DESIGN.md', () => {
    renderWithRouter(
      <DocumentCards
        sources={[
          kb(7, 'Bescheid', { tier: 3, paperless_url: `${PAPERLESS}/documents/70/details`, page_count: 2 }),
          paperless(11, 'Rechnung'),
          kb(9, 'Falsche Stufe', { tier: 9 }),
        ]}
      />,
    );
    const [tiered, neutral, badTier] = cards();
    expect(tiered).toHaveClass('atom-row', 'tier-ring-3');
    for (const card of [neutral, badTier]) {
      expect(card).toHaveClass('atom-row');
      expect(card.className).not.toMatch(/tier-ring-/);
    }
    for (const link of screen.getAllByRole('link')) {
      expect(link).toHaveClass('btn-secondary');
      // The focus ring is the one the button class defines -- nothing here
      // removes or recolours it.
      expect(link.className).not.toMatch(/outline-none|focus:|focus-visible:|ring-/);
    }
    // No competing card class for the same concept (DESIGN.md).
    expect(document.querySelector('.atom-card, .card')).toBeNull();
  });

  it('carries a dark variant wherever it sets a colour itself', () => {
    const { container } = renderWithRouter(
      <DocumentCards
        sources={[
          kb(7, 'Bescheid', { tier: 1, document_date: '2025-03-14' }),
          paperless(11, 'Rechnung', {}, false),
        ]}
      />,
    );
    for (const el of Array.from(container.querySelectorAll('*'))) {
      const cls = el.getAttribute('class') ?? '';
      const light = cls.split(/\s+/).filter((c) => /^(text|bg|border)-(gray|primary|accent|white|black)/.test(c));
      if (light.length === 0) continue;
      expect(cls, cls).toMatch(/\bdark:/);
    }
  });

  it('formats date and page count in the language of the app', async () => {
    await i18n.changeLanguage('en');
    renderWithRouter(
      <DocumentCards sources={[kb(7, 'Notice', { document_date: '2025-03-14', page_count: 3 })]} />,
    );
    expect(metaLine(cards()[0])).toBe('03/14/2025 · 3 pages');
    expect(screen.getByRole('link')).toHaveAccessibleName('Open Notice');
  });

  it('has every new card key in each of the four locales itself, not by fallback', () => {
    // `fallbackLng` is 'de': `t()` would find a key missing in en/fr/it in the
    // German bundle. `exists` without a fallback asks the locale alone.
    const keys = ['open', 'openDocument', 'inPaperless', 'pages_one', 'pages_other'];
    for (const lng of ['de', 'en', 'fr', 'it']) {
      for (const key of keys) {
        expect(i18n.exists(`chat.sources.${key}`, { lng, fallbackLng: [] }), `${lng}: ${key}`).toBe(true);
      }
    }
    // French distinguishes a `many` form (frenchLocale.test.ts).
    expect(i18n.exists('chat.sources.pages_many', { lng: 'fr', fallbackLng: [] })).toBe(true);
  });

  it('has every card string in all four languages, none of them the key', () => {
    for (const lng of ['de', 'en', 'it', 'fr']) {
      for (const key of ['open', 'openDocument', 'inPaperless', 'pages', 'more', 'untitled', 'noLink']) {
        for (const count of [1, 3]) {
          const text = i18n.t(`chat.sources.${key}`, { lng, id: 3, title: 'T', count });
          expect(text, `${lng}:${key}`).not.toContain('chat.sources');
          expect(text.length, `${lng}:${key}`).toBeGreaterThan(2);
        }
      }
    }
    expect(i18n.t('chat.sources.pages', { lng: 'en', count: 1 })).toBe('1 page');
    expect(i18n.t('chat.sources.pages', { lng: 'de', count: 3 })).toBe('3 S.');
  });
});
