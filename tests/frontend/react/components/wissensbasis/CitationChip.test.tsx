import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { CitationChip } from '../../../../../src/frontend/src/components/wissensbasis/CitationChip';
import { WissensbasisProvider, useWissensbasis } from '../../../../../src/frontend/src/context/WissensbasisContext';
import { renderWithRouter } from '../../test-utils';

function FocusProbe() {
  const { focusEntityId } = useWissensbasis();
  return <span data-testid="focus">{focusEntityId ?? 'NONE'}</span>;
}

describe('CitationChip', () => {
  it('renders the label and is interactive when entity is valid', async () => {
    renderWithRouter(
      <WissensbasisProvider>
        <CitationChip entity="abc-123" label="PRODUCT-A 1.3.5" entityType="release" />
        <FocusProbe />
      </WissensbasisProvider>,
    );

    const btn = screen.getByRole('button', { name: /Fokus auf PRODUCT-A 1.3.5/i });
    expect(btn).toBeInTheDocument();
    expect(screen.getByTestId('focus')).toHaveTextContent('NONE');

    await userEvent.click(btn);
    expect(screen.getByTestId('focus')).toHaveTextContent('abc-123');
  });

  it.each(['constructor', '__proto__', 'hasOwnProperty'])(
    'falls back to the default icon for type="%s" (no prototype lookup)',
    (type) => {
      renderWithRouter(
        <WissensbasisProvider>
          <CitationChip entity="abc-123" label="Crafted" entityType={type} />
        </WissensbasisProvider>,
      );
      expect(screen.getByRole('button', { name: /Crafted/ })).toBeInTheDocument();
    },
  );

  it('renders missing chips as non-interactive with strike-through', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <CitationChip entity="" label="Deleted Thing" missing />
      </WissensbasisProvider>,
    );

    // Non-interactive: rendered as <span>, no button role
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText('Deleted Thing')).toBeInTheDocument();
  });

  it('throws in dev when used outside a WissensbasisProvider (matches useAuth/useTheme)', () => {
    // Spy + suppress the React error boundary log so test output stays
    // clean. The error is the contract — we WANT to surface it in dev to
    // catch accidental misuse, while production gets the silent shim.
    //
    // try/finally so a future assertion regression doesn't leak the spy
    // into adjacent tests (Vitest has isolate:false in this suite).
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      expect(() => renderWithRouter(<CitationChip entity="abc-123" label="X" />)).toThrow(
        /useWissensbasis must be used within a WissensbasisProvider/,
      );
    } finally {
      consoleSpy.mockRestore();
    }
  });
});
