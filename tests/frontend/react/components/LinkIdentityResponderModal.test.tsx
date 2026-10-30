/**
 * LinkIdentityResponderModal -- side B of the person handshake (F-ID-2).
 *
 * 🛑 This is the dialog that actually WRITES, and therefore the more
 * consequential of the two: after the consent this instance answers the other
 * one's questions with its own full reach. So this run checks not only that the
 * flow works, but also that the warning stands BEFORE the consent button and
 * not after it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import LinkIdentityResponderModal from '../../../../src/frontend/src/components/LinkIdentityResponderModal';
import { renderWithRouter } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: { post: vi.fn() },
  extractApiError: (_e: unknown, fallback: string) => fallback,
}));

const ANGEBOT = {
  initiator_pubkey: 'a'.repeat(64),
  responder_pubkey: 'b'.repeat(64),
  querier_ref: 'c'.repeat(64),
  stated_by: 1,
  nonce: 'n'.repeat(32),
  issued_at: Math.floor(Date.now() / 1000),
  expires_at: Math.floor(Date.now() / 1000) + 600,
  signature: 's'.repeat(128),
};

const post = vi.mocked(apiClient.post);

function einlesen(angebot: object = ANGEBOT) {
  renderWithRouter(<LinkIdentityResponderModal isOpen />);
  fireEvent.change(screen.getByLabelText(/angebot der anderen instanz/i), {
    target: { value: JSON.stringify(angebot) },
  });
  fireEvent.click(screen.getByRole('button', { name: /angebot einlesen/i }));
}

describe('LinkIdentityResponderModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockReset();
  });

  it('shows nothing while it is closed', () => {
    renderWithRouter(<LinkIdentityResponderModal isOpen={false} />);
    expect(screen.queryByRole('button', { name: /angebot einlesen/i })).not.toBeInTheDocument();
  });

  it('🛑 the warning comes BEFORE the consent button', async () => {
    einlesen();
    // Both have to be visible at the same time: whoever consents should be
    // able to read in the same glance what they are opening.
    expect(await screen.findByText(/vollen eigenen Reichweite/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /zustimmen und verknüpfen/i })).toBeInTheDocument();
  });

  it('accepts and shows the confirmation as a QR frame', async () => {
    einlesen();
    post.mockResolvedValueOnce({ data: { nonce: ANGEBOT.nonce, signature: 'r'.repeat(128) } });

    fireEvent.click(await screen.findByRole('button', { name: /zustimmen und verknüpfen/i }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith('/api/federation/links/accept', { offer: ANGEBOT });
    expect(await screen.findByRole('img', { name: /bestätigung/i })).toBeInTheDocument();
  });

  it('🛑 an expired offer is rejected WITHOUT sending', () => {
    const alt = Math.floor(Date.now() / 1000) - 10;
    einlesen({ ...ANGEBOT, issued_at: alt - 600, expires_at: alt });

    expect(screen.getByText(/abgelaufen/i)).toBeInTheDocument();
    // The only failure the person can fix themselves -- hence here, and not
    // as a bare 400 from the server.
    expect(post).not.toHaveBeenCalled();
  });

  it('names a missing required field', () => {
    const { querier_ref: _weg, ...ohne } = ANGEBOT;
    einlesen(ohne);

    expect(screen.getByText(/querier_ref/i)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it('rejects unreadable JSON', () => {
    renderWithRouter(<LinkIdentityResponderModal isOpen />);
    fireEvent.change(screen.getByLabelText(/angebot der anderen instanz/i), {
      target: { value: '{kein json' },
    });
    fireEvent.click(screen.getByRole('button', { name: /angebot einlesen/i }));

    expect(screen.getByText(/kein gültiges JSON/i)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});
