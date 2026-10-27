/**
 * LinkIdentityInitiatorModal — Seite A des Personen-Handschlags (F-ID-2).
 *
 * Verknüpft „ich hier" mit „ich dort". Gespiegelt nach `PairInitiatorModal`,
 * mit zwei bewussten Unterschieden:
 *
 * 🛑 **Keine Stufenauswahl.** Eine Instanz-Paarung GEWÄHRT eine Stufe; eine
 * Personenverknüpfung HEBT sie auf — ab dann läuft die Anfrage mit der eigenen
 * vollen Reichweite. Ein `TierPicker` an dieser Stelle wäre eine Lüge über das,
 * was gerade passiert.
 *
 * 🛑 **Das Angebot kommt beim Abschluss zurück.** Der Server merkt sich
 * zwischen Schritt 1 und 3 NICHTS; die Bindung an die Person steht in der
 * eigenen Signatur über dem Angebot. Darum hält dieser Dialog das Angebot im
 * Zustand und schickt es mit — geht es verloren (Fenster zu), ist der Vorgang
 * verloren und muss neu beginnen. Das ist gewollt: kein halber Link.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { Check, Copy, Fingerprint } from 'lucide-react';

import apiClient, { extractApiError } from '../utils/axios';
import Modal from './Modal';
import Alert from './Alert';

type Step = 'intro' | 'await_confirmation' | 'done';

/** Nur die Felder, die dieser Dialog selbst anfasst. Die Form prüft der Server. */
interface LinkOffer {
  initiator_pubkey: string;
  nonce: string;
  [k: string]: unknown;
}

/** Was eine Bestätigung mindestens tragen muss, bevor es sich lohnt zu senden. */
const REQUIRED_CONFIRMATION_FIELDS = ['responder_pubkey', 'signature', 'nonce'] as const;

interface Props {
  isOpen: boolean;
  peerId: number | null;
  peerName?: string;
  onClose?: () => void;
  onLinked?: () => void;
}

export default function LinkIdentityInitiatorModal({
  isOpen, peerId, peerName, onClose, onLinked,
}: Props) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>('intro');
  const [offer, setOffer] = useState<LinkOffer | null>(null);
  const [confirmationText, setConfirmationText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const reset = () => {
    setStep('intro');
    setOffer(null);
    setConfirmationText('');
    setError(null);
    setLoading(false);
    setCopied(false);
  };

  const handleClose = () => {
    reset();
    onClose?.();
  };

  const handleGenerateOffer = async () => {
    if (peerId === null) return;
    setLoading(true);
    setError(null);
    try {
      const { data } = await apiClient.post<LinkOffer>(
        '/api/federation/links/offer', { peer_id: peerId },
      );
      setOffer(data);
      setStep('await_confirmation');
    } catch (err) {
      setError(extractApiError(err, t('circles.linkOfferFailed')));
    } finally {
      setLoading(false);
    }
  };

  const handleCopyOffer = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(offer));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Zwischenablage kann verweigert sein — das JSON steht sichtbar daneben.
    }
  };

  const handleComplete = async () => {
    if (!offer) return;
    const text = confirmationText.trim();
    if (!text) {
      setError(t('circles.linkConfirmationRequired'));
      return;
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text);
    } catch {
      setError(t('circles.linkConfirmationMalformed'));
      return;
    }
    for (const field of REQUIRED_CONFIRMATION_FIELDS) {
      if (typeof parsed[field] !== 'string') {
        setError(t('circles.linkConfirmationMissingField', { field }));
        return;
      }
    }
    // 🛑 Lokale Gegenprobe VOR dem Senden: antwortet die Bestätigung überhaupt
    // auf DIESES Angebot? Spart einen Zug und eine nichtssagende 400 — der
    // Server prüft es ohnehin noch einmal, dort zählt es.
    if (parsed.nonce !== offer.nonce) {
      setError(t('circles.linkConfirmationWrongNonce'));
      return;
    }

    setLoading(true);
    setError(null);
    try {
      await apiClient.post('/api/federation/links/complete', {
        offer, confirmation: parsed,
      });
      setStep('done');
      onLinked?.();
    } catch (err) {
      setError(extractApiError(err, t('circles.linkCompleteFailed')));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title={t('circles.linkInitiateTitle')}>
      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}

      {step === 'intro' && (
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.linkInitiateStep1Explanation', { peer: peerName ?? '' })}
          </p>
          <Alert variant="warning">{t('circles.linkReachWarning')}</Alert>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={handleClose} className="btn-secondary px-4 py-2 rounded-lg">
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={handleGenerateOffer}
              disabled={loading || peerId === null}
              className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50"
            >
              {loading ? t('common.loading') : t('circles.linkGenerateOffer')}
            </button>
          </div>
        </div>
      )}

      {step === 'await_confirmation' && offer && (
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.linkInitiateStep2Instruction')}
          </p>
          {/* Der role="img"-Rahmen ist nicht Zierde: `qrcode.react` ist im Test
              auf einen Stub gelegt, der Rahmen ist dort das einzig Sichtbare. */}
          <div
            role="img"
            aria-label={t('circles.linkQrCodeOfferAria')}
            className="flex justify-center py-4 bg-white rounded-lg border border-gray-200"
          >
            <QRCodeSVG value={JSON.stringify(offer)} size={220} level="M" />
          </div>
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <Fingerprint className="w-3 h-3" aria-hidden="true" />
            <code className="tabular-nums">{offer.initiator_pubkey.slice(0, 24)}…</code>
            <button
              type="button"
              onClick={handleCopyOffer}
              className="ml-auto btn-icon btn-icon-ghost"
              title={t('circles.linkCopyJson')}
              aria-label={t('circles.linkCopyJson')}
            >
              {copied ? <Check className="w-4 h-4 text-green-600" /> : <Copy className="w-4 h-4" />}
            </button>
          </div>

          <div>
            <label
              htmlFor="link-confirmation"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              {t('circles.linkPasteConfirmation')}
            </label>
            <textarea
              id="link-confirmation"
              value={confirmationText}
              onChange={(e) => setConfirmationText(e.target.value)}
              rows={4}
              className="input font-mono text-xs"
              placeholder='{"nonce":"...","responder_pubkey":"...","signature":"..."}'
            />
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={handleClose} className="btn-secondary px-4 py-2 rounded-lg">
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={handleComplete}
              disabled={loading}
              className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50"
            >
              {loading ? t('common.loading') : t('circles.linkCompleteButton')}
            </button>
          </div>
        </div>
      )}

      {step === 'done' && (
        <div className="space-y-4">
          <Alert variant="success">{t('circles.linkSuccess')}</Alert>
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.linkSuccessHint')}
          </p>
          <div className="flex justify-end">
            <button type="button" onClick={handleClose} className="btn-primary px-4 py-2 rounded-lg">
              {t('common.close')}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
