import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import TierControlPopover from '../../../../src/frontend/src/components/knowledge/TierControlPopover';
import { renderWithProviders } from '../test-utils';

// i18n is loaded in 'de': tier labels are Privat/Vertraut/Haushalt/Erweitert/Öffentlich.

describe('TierControlPopover', () => {
  it('opens the tier picker only after the trigger is clicked', () => {
    renderWithProviders(<TierControlPopover tier={2} onChange={() => {}} />);
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button')); // the trigger badge
    expect(screen.getByRole('radiogroup')).toBeInTheDocument();
  });

  it('commits a non-public tier immediately, no confirmation', () => {
    const onChange = vi.fn();
    const confirmPublic = vi.fn();
    renderWithProviders(
      <TierControlPopover tier={0} onChange={onChange} confirmPublic={confirmPublic} />,
    );
    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByRole('radio', { name: /Haushalt/i }));
    expect(onChange).toHaveBeenCalledWith(2);
    expect(confirmPublic).not.toHaveBeenCalled();
  });

  it('gates → public behind confirmPublic and commits when confirmed', async () => {
    const onChange = vi.fn();
    const confirmPublic = vi.fn().mockResolvedValue(true);
    renderWithProviders(
      <TierControlPopover tier={0} onChange={onChange} confirmPublic={confirmPublic} />,
    );
    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByRole('radio', { name: /Öffentlich/i }));
    expect(confirmPublic).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(4));
  });

  it('does NOT commit → public when the confirmation is declined', async () => {
    const onChange = vi.fn();
    const confirmPublic = vi.fn().mockResolvedValue(false);
    renderWithProviders(
      <TierControlPopover tier={0} onChange={onChange} confirmPublic={confirmPublic} />,
    );
    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByRole('radio', { name: /Öffentlich/i }));
    await waitFor(() => expect(confirmPublic).toHaveBeenCalledTimes(1));
    expect(onChange).not.toHaveBeenCalled();
  });
});
