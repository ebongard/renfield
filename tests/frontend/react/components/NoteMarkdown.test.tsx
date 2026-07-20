import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import NoteMarkdown from '../../../../src/frontend/src/components/NoteMarkdown';

describe('NoteMarkdown', () => {
  it('renders markdown (bold, heading, list) as real elements', () => {
    render(<NoteMarkdown body={'# Titel\n\n**fett** und normal\n\n- eins\n- zwei'} />);
    expect(screen.getByRole('heading', { name: 'Titel' })).toBeInTheDocument();
    expect(screen.getByText('fett').tagName).toBe('STRONG');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('renders a [[wikilink]] as a button and calls onWikilink with the title', async () => {
    const onWikilink = vi.fn();
    render(<NoteMarkdown body={'siehe [[Beta]] dazu'} onWikilink={onWikilink} />);
    const link = screen.getByRole('button', { name: 'Beta' });
    await userEvent.click(link);
    expect(onWikilink).toHaveBeenCalledWith('Beta');
  });

  it('leaves [[wikilinks]] inside code spans verbatim (no chip)', () => {
    render(<NoteMarkdown body={'`[[NotALink]]`'} />);
    expect(screen.queryByRole('button', { name: 'NotALink' })).not.toBeInTheDocument();
    expect(screen.getByText('[[NotALink]]')).toBeInTheDocument();
  });

  it('does not turn a [[wikilink]] inside a real markdown link into a nested link', () => {
    render(<NoteMarkdown body={'[siehe [[Inner]] text](https://example.com)'} />);
    // The whole thing is ONE external link; no wikilink button is minted inside it.
    expect(screen.queryByRole('button', { name: /Inner/ })).not.toBeInTheDocument();
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.com');
  });

  it('neutralizes a javascript: URL (no executable href, text kept)', () => {
    // eslint-disable-next-line no-script-url
    const { container } = render(<NoteMarkdown body={'[x](javascript:alert(1))'} />);
    // react-markdown sanitizes the URL: NO anchor carries a javascript: href
    // (it either drops the href or the anchor). The link text is still shown.
    container.querySelectorAll('a[href]').forEach((a) =>
      expect(a.getAttribute('href') ?? '').not.toMatch(/^javascript:/i));
    expect(container.textContent).toContain('x');
  });

  it('renders raw HTML in a note body inert (escaped to text, no live element)', () => {
    const { container } = render(
      <NoteMarkdown body={'hi <img src=x onerror="alert(1)"> there'} />,
    );
    // react-markdown (no rehype-raw) escapes raw HTML → it becomes literal text,
    // NOT a live element with a handler. No <img>, nothing carrying onerror.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
    expect(container.textContent).toContain('<img'); // shown verbatim as text
  });

  it('opens external links in a new tab safely', () => {
    render(<NoteMarkdown body={'[ext](https://example.com)'} />);
    const a = screen.getByRole('link', { name: 'ext' });
    expect(a).toHaveAttribute('target', '_blank');
    expect(a).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });
});
