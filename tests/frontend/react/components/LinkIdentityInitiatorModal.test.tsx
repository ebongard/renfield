/**
 * LinkIdentityInitiatorModal — Seite A des Personen-Handschlags (F-ID-2).
 *
 * 🛑 Der Test, der hier mehr trägt als die übrigen, ist der auf den
 * MITGESCHICKTEN Angebotsrumpf. Der Server merkt sich zwischen Schritt 1 und 3
 * nichts; die Bindung an die Person steht in seiner eigenen Signatur über dem
 * Angebot. Schickte dieser Dialog beim Abschluss nur die Bestätigung, wäre der
 * Handschlag nicht zu Ende zu bringen — und zwar mit einer nichtssagenden 400,
 * weil die Routenschicht absichtlich kein Orakel ist.
 *
 * `qrcode.react` ist per Vitest-Alias auf einen Stub gelegt; geprüft wird
 * darum immer der `role="img"`-Rahmen, nie das SVG.
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

  it('zeigt nichts, solange er geschlossen ist', () => {
    renderWithRouter(<LinkIdentityInitiatorModal isOpen={false} peerId={7} />);
    expect(screen.queryByRole('button', { name: /angebot erzeugen/i })).not.toBeInTheDocument();
  });

  it('warnt VOR dem Erzeugen, was eine Verknuepfung bedeutet', () => {
    renderWithRouter(<LinkIdentityInitiatorModal isOpen peerId={7} peerName="x-idra" />);
    // Die Reichweite ist der ganze Punkt — sie darf nicht erst hinterher stehen.
    expect(screen.getByText(/vollen eigenen Reichweite/i)).toBeInTheDocument();
  });

  it('erzeugt das Angebot und zeigt den QR-Rahmen', async () => {
    await bisZurBestaetigung();
    expect(post).toHaveBeenCalledWith('/api/federation/links/offer', { peer_id: 7 });
  });

  it('🛑 schickt beim Abschluss das ANGEBOT mit, nicht nur die Bestaetigung', async () => {
    await bisZurBestaetigung();
    post.mockResolvedValueOnce({ data: { id: 1 } });

    fireEvent.change(screen.getByLabelText(/bestätigung der anderen instanz/i), {
      target: { value: JSON.stringify(BESTAETIGUNG) },
    });
    fireEvent.click(screen.getByRole('button', { name: /verknüpfung abschließen/i }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const [pfad, rumpf] = post.mock.calls[1];
    expect(pfad).toBe('/api/federation/links/complete');
    // Der Server haelt KEINEN Zustand — ohne das Angebot kann er seine eigene
    // Signatur nicht pruefen und der Handschlag endet in einer blanken 400.
    expect(rumpf).toMatchObject({ offer: ANGEBOT, confirmation: BESTAETIGUNG });
  });

  it('weist eine Bestaetigung zum falschen Angebot ab, OHNE zu senden', async () => {
    await bisZurBestaetigung();

    fireEvent.change(screen.getByLabelText(/bestätigung der anderen instanz/i), {
      target: { value: JSON.stringify({ ...BESTAETIGUNG, nonce: 'x'.repeat(32) }) },
    });
    fireEvent.click(screen.getByRole('button', { name: /verknüpfung abschließen/i }));

    expect(await screen.findByText(/anderen Angebot/i)).toBeInTheDocument();
    // 🛑 Kein zweiter Zug: die lokale Pruefung spart der Person eine
    // nichtssagende 400 von der absichtlich orakelfreien Routenschicht.
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('nennt das fehlende Feld beim Namen', async () => {
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
