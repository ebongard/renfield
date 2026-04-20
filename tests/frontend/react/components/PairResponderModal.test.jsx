import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import PairResponderModal from '../../../../src/frontend/src/components/PairResponderModal';
import { renderWithRouter } from '../test-utils.jsx';
import apiClient from '../../../../src/frontend/src/utils/axios';

// qrcode.react stubbed via vitest.config.js alias.

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    post: vi.fn(),
  },
}));

const FAKE_OFFER = {
  initiator_pubkey: 'a'.repeat(64),
  nonce: 'n'.repeat(32),
  signature: 's'.repeat(128),
  display_name: 'Asker',
  expires_at: Math.floor(Date.now() / 1000) + 600,
};

const FAKE_RESPONSE = {
  responder_pubkey: 'b'.repeat(64),
  signature: 'r'.repeat(128),
  nonce: FAKE_OFFER.nonce,
};

describe('PairResponderModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiClient.post.mockReset();
  });

  it('does not render when closed', () => {
    renderWithRouter(
      <PairResponderModal isOpen={false} onClose={() => {}} onPaired={() => {}} />,
    );
    expect(screen.queryByPlaceholderText(/initiator_pubkey/)).not.toBeInTheDocument();
  });

  it('rejects malformed JSON in offer textarea', () => {
    renderWithRouter(
      <PairResponderModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    const textarea = screen.getByPlaceholderText(/initiator_pubkey/);
    fireEvent.change(textarea, { target: { value: 'not json {' } });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    // Stay on step 1, show error
    expect(screen.getByPlaceholderText(/initiator_pubkey/)).toBeInTheDocument();
  });

  it('rejects offer missing required fields', () => {
    renderWithRouter(
      <PairResponderModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    const incomplete = { initiator_pubkey: 'x'.repeat(64) };
    fireEvent.change(screen.getByPlaceholderText(/initiator_pubkey/), {
      target: { value: JSON.stringify(incomplete) },
    });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    // Iteration order is initiator_pubkey, signature, nonce, display_name.
    // initiator_pubkey is present → the first missing key flagged is `signature`.
    expect(screen.getByText(/Feld "signature" fehlt in der Einladung/)).toBeInTheDocument();
  });

  it('rejects expired offer', () => {
    renderWithRouter(
      <PairResponderModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    const expired = { ...FAKE_OFFER, expires_at: Math.floor(Date.now() / 1000) - 60 };
    fireEvent.change(screen.getByPlaceholderText(/initiator_pubkey/), {
      target: { value: JSON.stringify(expired) },
    });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    // Still on step 1 (no tier picker present)
    expect(screen.getByPlaceholderText(/initiator_pubkey/)).toBeInTheDocument();
  });

  it('advances to tier step on valid offer and shows initiator pubkey', () => {
    renderWithRouter(
      <PairResponderModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    fireEvent.change(screen.getByPlaceholderText(/initiator_pubkey/), {
      target: { value: JSON.stringify(FAKE_OFFER) },
    });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    expect(screen.getByText(FAKE_OFFER.initiator_pubkey)).toBeInTheDocument();
  });

  // SHOULD #3 — render-time fallback when display_name is empty
  it('falls back to "unknown peer" label when display_name is empty', () => {
    renderWithRouter(
      <PairResponderModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    const emptyName = { ...FAKE_OFFER, display_name: '' };
    fireEvent.change(screen.getByPlaceholderText(/initiator_pubkey/), {
      target: { value: JSON.stringify(emptyName) },
    });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    // German fallback from locale: "Unbekannter Peer"
    expect(screen.getByText(/Unbekannter Peer/)).toBeInTheDocument();
  });

  it('posts accept with offer + tier and renders response QR', async () => {
    const onPaired = vi.fn();
    apiClient.post.mockResolvedValueOnce({ data: FAKE_RESPONSE });

    renderWithRouter(
      <PairResponderModal isOpen={true} onClose={() => {}} onPaired={onPaired} />,
    );

    fireEvent.change(screen.getByPlaceholderText(/initiator_pubkey/), {
      target: { value: JSON.stringify(FAKE_OFFER) },
    });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    fireEvent.click(screen.getByRole('button', { name: /kopplung annehmen/i }));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/federation/pair/accept',
        expect.objectContaining({ offer: FAKE_OFFER, my_tier_for_you: 2 }),
      );
      expect(screen.getByRole('img', { name: /kopplungs-antwort/i })).toBeInTheDocument();
      expect(onPaired).toHaveBeenCalledTimes(1);
    });
    // Fingerprint preview of responder pubkey is rendered
    expect(
      screen.getByText(new RegExp(FAKE_RESPONSE.responder_pubkey.slice(0, 24))),
    ).toBeInTheDocument();
  });

  it('renders response QR with accessible aria-label', async () => {
    apiClient.post.mockResolvedValueOnce({ data: FAKE_RESPONSE });

    renderWithRouter(
      <PairResponderModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    fireEvent.change(screen.getByPlaceholderText(/initiator_pubkey/), {
      target: { value: JSON.stringify(FAKE_OFFER) },
    });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));
    fireEvent.click(screen.getByRole('button', { name: /kopplung annehmen/i }));

    await waitFor(() => {
      const qrRegion = screen.getByRole('img', { name: /qr-code/i });
      expect(qrRegion).toBeInTheDocument();
      expect(qrRegion.getAttribute('aria-label')).toMatch(/antwort/i);
    });
  });
});
