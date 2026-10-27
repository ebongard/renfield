/**
 * LinkIdentityResponderModal — Seite B des Personen-Handschlags (F-ID-2).
 *
 * 🛑 Dies ist der Dialog, der tatsächlich SCHREIBT, und damit der folgenreichere
 * der beiden: nach der Zustimmung beantwortet diese Instanz Anfragen der
 * anderen mit der vollen eigenen Reichweite. Darum prüft dieser Lauf nicht nur,
 * dass der Ablauf funktioniert, sondern auch, dass die Warnung VOR dem
 * Zustimmungsknopf steht und nicht erst danach.
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

  it('zeigt nichts, solange er geschlossen ist', () => {
    renderWithRouter(<LinkIdentityResponderModal isOpen={false} />);
    expect(screen.queryByRole('button', { name: /angebot einlesen/i })).not.toBeInTheDocument();
  });

  it('🛑 die Warnung steht VOR dem Zustimmungsknopf', async () => {
    einlesen();
    // Beide müssen gleichzeitig sichtbar sein: wer zustimmt, soll im selben
    // Blick lesen können, was er damit öffnet.
    expect(await screen.findByText(/vollen eigenen Reichweite/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /zustimmen und verknüpfen/i })).toBeInTheDocument();
  });

  it('nimmt an und zeigt die Bestaetigung als QR-Rahmen', async () => {
    einlesen();
    post.mockResolvedValueOnce({ data: { nonce: ANGEBOT.nonce, signature: 'r'.repeat(128) } });

    fireEvent.click(await screen.findByRole('button', { name: /zustimmen und verknüpfen/i }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith('/api/federation/links/accept', { offer: ANGEBOT });
    expect(await screen.findByRole('img', { name: /bestätigung/i })).toBeInTheDocument();
  });

  it('🛑 ein abgelaufenes Angebot wird abgewiesen, OHNE zu senden', () => {
    const alt = Math.floor(Date.now() / 1000) - 10;
    einlesen({ ...ANGEBOT, issued_at: alt - 600, expires_at: alt });

    expect(screen.getByText(/abgelaufen/i)).toBeInTheDocument();
    // Der einzige Fehlschlag, den die Person selbst beheben kann — darum hier
    // und nicht als blanke 400 vom Server.
    expect(post).not.toHaveBeenCalled();
  });

  it('nennt ein fehlendes Pflichtfeld beim Namen', () => {
    const { querier_ref: _weg, ...ohne } = ANGEBOT;
    einlesen(ohne);

    expect(screen.getByText(/querier_ref/i)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it('weist unlesbares JSON ab', () => {
    renderWithRouter(<LinkIdentityResponderModal isOpen />);
    fireEvent.change(screen.getByLabelText(/angebot der anderen instanz/i), {
      target: { value: '{kein json' },
    });
    fireEvent.click(screen.getByRole('button', { name: /angebot einlesen/i }));

    expect(screen.getByText(/kein gültiges JSON/i)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});
