/**
 * LinkIdentityInitiatorModal -- side A of the person handshake (F-ID-2).
 *
 * Links "me here" with "me there". Mirrored on `PairInitiatorModal`,
 * with two deliberate differences:
 *
 * **No tier picker.** An instance pairing GRANTS a tier; a person link
 * LIFTS it -- from then on the request runs with the person's own full reach.
 * A `TierPicker` here would be a lie about what is happening.
 *
 * **The offer comes back at completion.** The server remembers NOTHING
 * between step 1 and step 3; the binding to the person lives in our own
 * signature over the offer. So this dialog keeps the offer in state and sends
 * it along -- lose it (window closed) and the whole flow is lost and has to
 * start over. That is intended: no half link.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { Check, Copy, Fingerprint } from 'lucide-react';

import apiClient, { extractApiError } from '../utils/axios';
import Modal from './Modal';
import Alert from './Alert';

type Step = 'intro' | 'await_confirmation' | 'done';

/** Only the fields this dialog touches itself. The server validates the shape. */
interface LinkOffer {
  initiator_pubkey: string;
  nonce: string;
  [k: string]: unknown;
}

/** The minimum a confirmation has to carry before sending is worthwhile. */
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
      // The clipboard may be refused -- the JSON is visible next to it.
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
    // Local cross-check BEFORE sending: does the confirmation even answer
    // THIS offer? Saves a round trip and a meaningless 400 -- the
    // server checks it again anyway, and there is where it counts.
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
          {/* The role="img" wrapper is not decoration: `qrcode.react` is
              stubbed in tests, where the wrapper is the only visible thing. */}
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
