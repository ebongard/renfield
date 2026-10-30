/**
 * LinkIdentityInitiatorModal -- side A of the person handshake (F-ID-2).
 *
 * 🛑 The test that carries more here than the others is the one on the offer
 * body being SENT ALONG. The server remembers nothing between step 1 and step
 * 3; the binding to the person lives in its own signature over the offer. If
 * this dialog sent only the confirmation at completion, the handshake could not
 * be brought to an end -- and would fail with a meaningless 400, because the
 * route layer is deliberately not an oracle.
 *
 * `qrcode.react` is aliased to a stub via Vitest; what is checked is therefore
 * always the `role="img"` wrapper, never the SVG.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import LinkIdentityInitiatorModal from '../../../../src/frontend/src/components/LinkIdentityInitiatorModal';
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

const BESTAETIGUNG = {
  nonce: ANGEBOT.nonce,
  querier_ref: ANGEBOT.querier_ref,
  responder_pubkey: 'b'.repeat(64),
  responder_user_id: 9,
  accepted_at: Math.floor(Date.now() / 1000),
  signature: 'r'.repeat(128),
};

const post = vi.mocked(apiClient.post);

async function bisZurBestaetigung() {
  post.mockResolvedValueOnce({ data: ANGEBOT });
  renderWithRouter(
    <LinkIdentityInitiatorModal isOpen peerId={7} peerName="x-idra" />,
  );
  fireEvent.click(screen.getByRole('button', { name: /angebot erzeugen/i }));
  await screen.findByRole('img', { name: /angebot/i });
}

describe('LinkIdentityInitiatorModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockReset();
  });

  it('shows nothing while it is closed', () => {
    renderWithRouter(<LinkIdentityInitiatorModal isOpen={false} peerId={7} />);
    expect(screen.queryByRole('button', { name: /angebot erzeugen/i })).not.toBeInTheDocument();
  });

  it('warns BEFORE generating what a link means', () => {
    renderWithRouter(<LinkIdentityInitiatorModal isOpen peerId={7} peerName="x-idra" />);
    // The reach is the whole point -- it must not appear only afterwards.
    expect(screen.getByText(/vollen eigenen Reichweite/i)).toBeInTheDocument();
  });

  it('generates the offer and shows the QR frame', async () => {
    await bisZurBestaetigung();
    expect(post).toHaveBeenCalledWith('/api/federation/links/offer', { peer_id: 7 });
  });

  it('🛑 sends the OFFER along at completion, not just the confirmation', async () => {
    await bisZurBestaetigung();
    post.mockResolvedValueOnce({ data: { id: 1 } });

    fireEvent.change(screen.getByLabelText(/bestätigung der anderen instanz/i), {
      target: { value: JSON.stringify(BESTAETIGUNG) },
    });
    fireEvent.click(screen.getByRole('button', { name: /verknüpfung abschließen/i }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const [pfad, rumpf] = post.mock.calls[1];
    expect(pfad).toBe('/api/federation/links/complete');
    // The server holds NO state -- without the offer it cannot check its own
    // signature and the handshake ends in a bare 400.
    expect(rumpf).toMatchObject({ offer: ANGEBOT, confirmation: BESTAETIGUNG });
  });

  it('rejects a confirmation for the wrong offer WITHOUT sending', async () => {
    await bisZurBestaetigung();

    fireEvent.change(screen.getByLabelText(/bestätigung der anderen instanz/i), {
      target: { value: JSON.stringify({ ...BESTAETIGUNG, nonce: 'x'.repeat(32) }) },
    });
    fireEvent.click(screen.getByRole('button', { name: /verknüpfung abschließen/i }));

    expect(await screen.findByText(/anderen Angebot/i)).toBeInTheDocument();
    // 🛑 No second round trip: the local check saves the person a meaningless
    // 400 from the deliberately oracle-free route layer.
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('names the missing field', async () => {
    await bisZurBestaetigung();
    const { signature: _weg, ...ohne } = BESTAETIGUNG;

    fireEvent.change(screen.getByLabelText(/bestätigung der anderen instanz/i), {
      target: { value: JSON.stringify(ohne) },
    });
    fireEvent.click(screen.getByRole('button', { name: /verknüpfung abschließen/i }));

    expect(await screen.findByText(/signature/i)).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
  });
});
