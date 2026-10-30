/**
 * TierPicker: hook order, keyboard operation, both variants.
 *
 * 🛑 The most important test here is `it('survives a switch from pills to
 * compact')`. It pins the defect that left the component latently broken until
 * 2026-09-27: `useRef` and `useEffect` stood BEHIND the early `return` for
 * `variant === 'compact'`. React tracks hooks by CALL ORDER -- an instance that
 * switches variant then calls two fewer hooks and React throws "Rendered fewer
 * hooks than expected".
 *
 * It stayed latent only because all four call sites set the variant as a
 * literal. That is exactly why this test is needed: a defect that no existing
 * call site triggers would otherwise disappear again as soon as somebody binds
 * the variant to state.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import TierPicker from '../../../../src/frontend/src/components/TierPicker';

describe('TierPicker', () => {
  it('renders all five tiers as pills', () => {
    render(<TierPicker value={2} onChange={vi.fn()} />);
    expect(screen.getAllByRole('radio')).toHaveLength(5);
  });

  it('renders a select in the compact variant', () => {
    render(<TierPicker value={1} onChange={vi.fn()} variant="compact" />);
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select.value).toBe('1');
    expect(select.querySelectorAll('option')).toHaveLength(5);
  });

  it('survives a switch from pills to compact', () => {
    // The actual regression guard. Before the fix the re-render throws
    // "Rendered fewer hooks than expected", because the compact variant
    // returned before `useRef`/`useEffect`.
    const { rerender } = render(<TierPicker value={2} onChange={vi.fn()} />);
    expect(screen.getAllByRole('radio')).toHaveLength(5);

    expect(() =>
      rerender(<TierPicker value={2} onChange={vi.fn()} variant="compact" />),
    ).not.toThrow();
    expect(screen.getByRole('combobox')).toBeInTheDocument();

    // And back -- the other direction calls TWO hooks MORE, which React
    // notices just as well.
    expect(() =>
      rerender(<TierPicker value={3} onChange={vi.fn()} variant="pills" />),
    ).not.toThrow();
    expect(screen.getAllByRole('radio')).toHaveLength(5);
  });

  it('moves the selection with the arrow keys', () => {
    const onChange = vi.fn();
    render(<TierPicker value={2} onChange={onChange} />);
    fireEvent.keyDown(screen.getAllByRole('radio')[2], { key: 'ArrowRight' });
    expect(onChange).toHaveBeenCalledWith(3);
  });

  it('does not move when disabled', () => {
    const onChange = vi.fn();
    render(<TierPicker value={2} onChange={onChange} disabled />);
    fireEvent.keyDown(screen.getAllByRole('radio')[2], { key: 'ArrowRight' });
    expect(onChange).not.toHaveBeenCalled();
  });

  it('clamps at both ends of the ladder', () => {
    const onChange = vi.fn();
    const { rerender } = render(<TierPicker value={0} onChange={onChange} />);
    fireEvent.keyDown(screen.getAllByRole('radio')[0], { key: 'ArrowLeft' });
    expect(onChange).toHaveBeenCalledWith(0);

    onChange.mockClear();
    rerender(<TierPicker value={4} onChange={onChange} />);
    fireEvent.keyDown(screen.getAllByRole('radio')[4], { key: 'ArrowRight' });
    expect(onChange).toHaveBeenCalledWith(4);
  });
});
