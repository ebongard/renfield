/**
 * Block-level markdown in TextBlocks. The synthesizer (and multi-line tool
 * results) emit `### headings`, `-`/`*` bullet lists, and blank-line
 * paragraph breaks. Before this, a plain <p> collapsed the newlines and the
 * inline parser left `###`/`*` as literal text, so orchestrated answers
 * rendered as a run-on wall (Reva Q4 "R27.4 + Jira", Q3 "deploy date").
 * These assert headings/lists/paragraphs become real elements, that inline
 * bold still works inside them, and that single-line TextBlocks stay on the
 * cheap inline <p> path.
 */
import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';

import AdaptiveCardRenderer from '../../../../src/frontend/src/components/AdaptiveCardRenderer';
import { renderWithRouter } from '../test-utils';

describe('AdaptiveCardRenderer — block markdown in TextBlocks', () => {
  it('renders headings, bullet lists and paragraphs from multi-line markdown', () => {
    const card = {
      body: [
        {
          type: 'TextBlock' as const,
          text:
            'Basierend auf den Daten:\n' +
            '### Betroffene Anwendungen\n' +
            '- ERP-Core\n' +
            '* OrderMgmt\n' +
            '\n' +
            'Status: **Backlog**',
        },
      ],
    };
    const { container } = renderWithRouter(<AdaptiveCardRenderer card={card} />);

    // Both bullet markers (`-` and `*`) become list items.
    const items = screen.getAllByRole('listitem');
    expect(items.map((li) => li.textContent)).toEqual(['ERP-Core', 'OrderMgmt']);
    expect(container.querySelectorAll('ul')).toHaveLength(1);

    // Heading text survives; the `###` marker does not leak as literal text.
    expect(screen.getByText('Betroffene Anwendungen')).toBeInTheDocument();
    expect(screen.queryByText(/###/)).toBeNull();

    // Inline bold still parses inside a paragraph → <strong>, no literal `**`.
    const strong = screen.getByText('Backlog');
    expect(strong.tagName).toBe('STRONG');
    expect(screen.queryByText(/\*\*/)).toBeNull();
  });

  it('keeps single-line text on the inline <p> path (no list)', () => {
    const card = {
      body: [{ type: 'TextBlock' as const, text: 'Release Details' }],
    };
    const { container } = renderWithRouter(<AdaptiveCardRenderer card={card} />);

    expect(screen.getByText('Release Details')).toBeInTheDocument();
    expect(container.querySelectorAll('ul')).toHaveLength(0);
    expect(container.querySelectorAll('li')).toHaveLength(0);
  });
});
