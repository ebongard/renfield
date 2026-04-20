import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import PairInitiatorModal from '../../../../src/frontend/src/components/PairInitiatorModal';
import { renderWithRouter } from '../test-utils.jsx';
import apiClient from '../../../../src/frontend/src/utils/axios';

// qrcode.react is stubbed to a no-op via vitest.config.js alias; assertions
// use the role="img" aria-label container around the QR.

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

describe('PairInitiatorModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiClient.post.mockReset();
  });

  it('does not render when closed', () => {
    renderWithRouter(
      <PairInitiatorModal isOpen={false} onClose={() => {}} onPaired={() => {}} />,
    );
    expect(screen.queryByRole('button', { name: /einladung erzeugen/i })).not.toBeInTheDocument();
  });

  it('generates offer and shows QR container on step 1 submit', async () => {
    apiClient.post.mockResolvedValueOnce({ data: FAKE_OFFER });

    renderWithRouter(
      <PairInitiatorModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    const btn = screen.getByRole('button', { name: /einladung erzeugen/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith('/api/federation/pair/offer', {});
      // aria-labeled QR container — "QR-Code mit signierter Kopplungs-Einladung"
      expect(screen.getByRole('img', { name: /kopplungs-einladung/i })).toBeInTheDocument();
    });
    // Fingerprint preview of the pubkey
    expect(screen.getByText(new RegExp(FAKE_OFFER.initiator_pubkey.slice(0, 24)))).toBeInTheDocument();
  });

  // Regression guard for BLOCKING #1 — the server's PairingResponse schema
  // calls the signature field `signature`, NOT `responder_signature`. If the
  // field list ever regresses to `responder_signature`, this test must fail.
  it('accepts response JSON that uses signature (not responder_signature)', async () => {
    apiClient.post.mockResolvedValueOnce({ data: FAKE_OFFER });

    renderWithRouter(
      <PairInitiatorModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /einladung erzeugen/i }));
    await waitFor(() => expect(screen.getByRole('img', { name: /kopplungs-einladung/i })).toBeInTheDocument());

    const validResponse = {
      responder_pubkey: 'b'.repeat(64),
      signature: 'r'.repeat(128),
      nonce: FAKE_OFFER.nonce,
    };
    const textarea = screen.getByPlaceholderText(/responder_pubkey/);
    fireEvent.change(textarea, { target: { value: JSON.stringify(validResponse) } });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    // Should advance to tier-picker, not show an error
    await waitFor(() => {
      expect(screen.getByText(validResponse.responder_pubkey)).toBeInTheDocument();
    });
  });

  it('rejects response JSON missing the signature field', async () => {
    apiClient.post.mockResolvedValueOnce({ data: FAKE_OFFER });

    renderWithRouter(
      <PairInitiatorModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /einladung erzeugen/i }));
    await waitFor(() => expect(screen.getByRole('img', { name: /kopplungs-einladung/i })).toBeInTheDocument());

    const missingSig = {
      responder_pubkey: 'b'.repeat(64),
      nonce: FAKE_OFFER.nonce,
    };
    const textarea = screen.getByPlaceholderText(/responder_pubkey/);
    fireEvent.change(textarea, { target: { value: JSON.stringify(missingSig) } });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    await waitFor(() => {
      expect(screen.getByText(/signature/)).toBeInTheDocument();
    });
  });

  it('rejects response with wrong nonce', async () => {
    apiClient.post.mockResolvedValueOnce({ data: FAKE_OFFER });

    renderWithRouter(
      <PairInitiatorModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /einladung erzeugen/i }));
    await waitFor(() => expect(screen.getByRole('img', { name: /kopplungs-einladung/i })).toBeInTheDocument());

    const wrongNonce = {
      responder_pubkey: 'b'.repeat(64),
      signature: 'r'.repeat(128),
      nonce: 'WRONG-NONCE',
    };
    const textarea = screen.getByPlaceholderText(/responder_pubkey/);
    fireEvent.change(textarea, { target: { value: JSON.stringify(wrongNonce) } });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    // Assert the specific wrong-nonce error copy + user stays on step 2
    // (tier picker from step 3 must NOT be present).
    expect(screen.getByText(/nicht zu dieser einladung/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /kopplung abschließen/i })).not.toBeInTheDocument();
    expect(apiClient.post).toHaveBeenCalledTimes(1); // still only the offer call
  });

  it('calls onPaired on successful complete', async () => {
    apiClient.post
      .mockResolvedValueOnce({ data: FAKE_OFFER })       // /offer
      .mockResolvedValueOnce({ data: { ok: true } });    // /complete
    const onPaired = vi.fn();

    renderWithRouter(
      <PairInitiatorModal isOpen={true} onClose={() => {}} onPaired={onPaired} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /einladung erzeugen/i }));
    await waitFor(() => expect(screen.getByRole('img', { name: /kopplungs-einladung/i })).toBeInTheDocument());

    const validResponse = {
      responder_pubkey: 'b'.repeat(64),
      signature: 'r'.repeat(128),
      nonce: FAKE_OFFER.nonce,
    };
    fireEvent.change(screen.getByPlaceholderText(/responder_pubkey/), {
      target: { value: JSON.stringify(validResponse) },
    });
    fireEvent.click(screen.getByRole('button', { name: /weiter|continue/i }));

    await waitFor(() =>
      expect(screen.getByText(validResponse.responder_pubkey)).toBeInTheDocument(),
    );

    // Complete handshake
    fireEvent.click(screen.getByRole('button', { name: /kopplung abschließen/i }));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenLastCalledWith(
        '/api/federation/pair/complete',
        expect.objectContaining({ response: validResponse, their_tier_for_me: 2 }),
      );
      expect(onPaired).toHaveBeenCalledTimes(1);
    });
  });

  it('renders QR with accessible aria-label', async () => {
    apiClient.post.mockResolvedValueOnce({ data: FAKE_OFFER });

    renderWithRouter(
      <PairInitiatorModal isOpen={true} onClose={() => {}} onPaired={() => {}} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /einladung erzeugen/i }));

    await waitFor(() => {
      const qrRegion = screen.getByRole('img', { name: /qr-code/i });
      expect(qrRegion).toBeInTheDocument();
      expect(qrRegion.getAttribute('aria-label')).toMatch(/einladung/i);
    });
  });
});
