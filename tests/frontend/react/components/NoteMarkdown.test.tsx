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

  it('opens external links in a new tab safely', () => {
    render(<NoteMarkdown body={'[ext](https://example.com)'} />);
    const a = screen.getByRole('link', { name: 'ext' });
    expect(a).toHaveAttribute('target', '_blank');
    expect(a).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });
});
