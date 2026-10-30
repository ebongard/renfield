/**
 * LinkIdentityResponderModal -- side B of the person handshake (F-ID-2).
 *
 * This is where the person consents: "the `querier_ref` in this offer is me,
 * on THIS instance." It is the step that writes something, and at the same
 * time the more consequential of the two -- from now on the other instance
 * can ask questions that are answered with one's own full reach.
 *
 * 🛑 That is why the warning stands BEFORE the button, not after it, and why
 * the dialog names the far side as soon as the offer has been read in.
 * Whoever consents here has to know to whom.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { Check, Copy, Fingerprint } from 'lucide-react';

import apiClient, { extractApiError } from '../utils/axios';
import Modal from './Modal';
import Alert from './Alert';

type Step = 'paste_offer' | 'confirm' | 'show_confirmation';

interface ParsedOffer {
  initiator_pubkey: string;
  nonce: string;
  expires_at?: number;
  [k: string]: unknown;
}

const REQUIRED_OFFER_FIELDS = [
  'initiator_pubkey', 'responder_pubkey', 'querier_ref', 'signature', 'nonce',
] as const;

interface Props {
  isOpen: boolean;
  onClose?: () => void;
  onLinked?: () => void;
}

export default function LinkIdentityResponderModal({ isOpen, onClose, onLinked }: Props) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>('paste_offer');
  const [offerText, setOfferText] = useState('');
  const [parsedOffer, setParsedOffer] = useState<ParsedOffer | null>(null);
  const [confirmation, setConfirmation] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const reset = () => {
    setStep('paste_offer');
    setOfferText('');
    setParsedOffer(null);
    setConfirmation(null);
    setError(null);
    setLoading(false);
    setCopied(false);
  };

  const handleClose = () => {
    reset();
    onClose?.();
  };

  const handleSubmitOffer = () => {
    const text = offerText.trim();
    if (!text) {
      setError(t('circles.linkOfferRequired'));
      return;
    }
    let parsed: ParsedOffer;
    try {
      parsed = JSON.parse(text);
    } catch {
      setError(t('circles.linkOfferMalformed'));
      return;
    }
    for (const field of REQUIRED_OFFER_FIELDS) {
      if (typeof parsed[field] !== 'string') {
        setError(t('circles.linkOfferMissingField', { field }));
        return;
      }
    }
    // 🛑 Catch an expired offer here instead of letting the server send back
    // a meaningless 400: "expired" is the only failure the person can fix
    // themselves (request a new one).
    if (typeof parsed.expires_at === 'number' && parsed.expires_at < Date.now() / 1000) {
      setError(t('circles.linkOfferExpired'));
      return;
    }
    setParsedOffer(parsed);
    setError(null);
    setStep('confirm');
  };

  const handleAccept = async () => {
    if (!parsedOffer) return;
    setLoading(true);
    setError(null);
    try {
      const { data } = await apiClient.post<Record<string, unknown>>(
        '/api/federation/links/accept', { offer: parsedOffer },
      );
      setConfirmation(data);
      setStep('show_confirmation');
      onLinked?.();
    } catch (err) {
      setError(extractApiError(err, t('circles.linkAcceptFailed')));
    } finally {
      setLoading(false);
    }
  };

  const handleCopyConfirmation = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(confirmation));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // See the initiator: the JSON is visible right next to it.
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title={t('circles.linkAcceptTitle')}>
      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}

      {step === 'paste_offer' && (
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.linkAcceptStep1Instruction')}
          </p>
          <div>
            <label
              htmlFor="link-offer"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              {t('circles.linkPasteOffer')}
            </label>
            <textarea
              id="link-offer"
              value={offerText}
              onChange={(e) => setOfferText(e.target.value)}
              rows={5}
              className="input font-mono text-xs"
              placeholder='{"initiator_pubkey":"...","querier_ref":"...","signature":"..."}'
            />
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={handleClose} className="btn-secondary px-4 py-2 rounded-lg">
              {t('common.cancel')}
            </button>
            <button type="button" onClick={handleSubmitOffer} className="btn-primary px-4 py-2 rounded-lg">
              {t('circles.linkReadOffer')}
            </button>
          </div>
        </div>
      )}

      {step === 'confirm' && parsedOffer && (
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.linkAcceptStep2Instruction')}
          </p>
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <Fingerprint className="w-3 h-3" aria-hidden="true" />
            <code className="tabular-nums">{parsedOffer.initiator_pubkey.slice(0, 24)}…</code>
          </div>
          {/* Die Warnung steht VOR dem Knopf. Dieser Klick ist die Zustimmung. */}
          <Alert variant="warning">{t('circles.linkReachWarning')}</Alert>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setStep('paste_offer')}
              className="btn-secondary px-4 py-2 rounded-lg"
            >
              {t('common.back')}
            </button>
            <button
              type="button"
              onClick={handleAccept}
              disabled={loading}
              className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50"
            >
              {loading ? t('common.loading') : t('circles.linkAcceptButton')}
            </button>
          </div>
        </div>
      )}

      {step === 'show_confirmation' && confirmation && (
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.linkAcceptStep3Instruction')}
          </p>
          <div
            role="img"
            aria-label={t('circles.linkQrCodeConfirmationAria')}
            className="flex justify-center py-4 bg-white rounded-lg border border-gray-200"
          >
            <QRCodeSVG value={JSON.stringify(confirmation)} size={220} level="M" />
          </div>
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <button
              type="button"
              onClick={handleCopyConfirmation}
              className="ml-auto btn-icon btn-icon-ghost"
              title={t('circles.linkCopyJson')}
              aria-label={t('circles.linkCopyJson')}
            >
              {copied ? <Check className="w-4 h-4 text-green-600" /> : <Copy className="w-4 h-4" />}
            </button>
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {t('circles.linkAcceptStep3Hint')}
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
