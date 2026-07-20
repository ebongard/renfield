import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';

import NoteBodyEditor from '../../../../src/frontend/src/components/NoteBodyEditor';

/** Controlled wrapper (the component is controlled via value/onChange). */
function Harness({ titles }: { titles: string[] }) {
  const [value, setValue] = useState('');
  return (
    <NoteBodyEditor value={value} onChange={setValue} titles={titles} ariaLabel="body" />
  );
}

describe('NoteBodyEditor [[ ]] typeahead', () => {
  it('suggests matching titles after `[[` and inserts `Title]]` on click', async () => {
    const user = userEvent.setup();
    render(<Harness titles={['Roadmap', 'Rollout Plan', 'Andere']} />);
    const ta = screen.getByLabelText('body') as HTMLTextAreaElement;

    await user.click(ta);
    // userEvent v14 treats `[` as special-key syntax; `[[[[` types a literal `[[`.
    await user.type(ta, 'siehe [[[[Ro');

    // Both "Ro…" titles are offered; the unrelated one is not.
    expect(await screen.findByRole('option', { name: 'Roadmap' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Rollout Plan' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Andere' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('option', { name: 'Roadmap' }));
    expect(ta.value).toBe('siehe [[Roadmap]]');
  });

  it('shows no dropdown when the caret is not inside an open `[[`', async () => {
    const user = userEvent.setup();
    render(<Harness titles={['Roadmap']} />);
    const ta = screen.getByLabelText('body');
    await user.click(ta);
    await user.type(ta, 'kein link hier');
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });

  it('closes the dropdown after a completed link', async () => {
    const user = userEvent.setup();
    render(<Harness titles={['Roadmap']} />);
    const ta = screen.getByLabelText('body');
    await user.click(ta);
    await user.type(ta, '[[[[Road');
    expect(await screen.findByRole('option', { name: 'Roadmap' })).toBeInTheDocument();
    await user.type(ta, 'map]]');
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });
});
