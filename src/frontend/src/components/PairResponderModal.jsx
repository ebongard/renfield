import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { ArrowLeft, Check, Copy, Fingerprint } from 'lucide-react';
import apiClient from '../utils/axios';
import Modal from './Modal';
import Alert from './Alert';
import TierPicker from './TierPicker';

/**
 * PairResponderModal — drives the responder side of the F2 handshake.
 *
 * Three steps:
 *   1. Paste the initiator's signed offer JSON. Validate shape locally
 *      (server does the cryptographic check on submit).
 *   2. Review the initiator's identity (display_name + pubkey) and
 *      pick a tier (my_tier_for_you) to grant them.
 *   3. POST /api/federation/pair/accept. Render the resulting signed
 *      response as a QR code + copyable JSON for the initiator to scan
 *      or paste into their modal.
 */
const STEP_PASTE_OFFER = 'paste_offer';
const STEP_PICK_TIER = 'pick_tier';
const STEP_SHOW_RESPONSE = 'show_response';

export default function PairResponderModal({ isOpen, onClose, onPaired }) {
  const { t } = useTranslation();

  const [step, setStep] = useState(STEP_PASTE_OFFER);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const [offerText, setOfferText] = useState('');
  const [parsedOffer, setParsedOffer] = useState(null);
  const [tier, setTier] = useState(2);
  const [responseData, setResponseData] = useState(null);

  const [copied, setCopied] = useState(false);

  const reset = () => {
    setStep(STEP_PASTE_OFFER);
    setLoading(false);
    setError(null);
    setOfferText('');
    setParsedOffer(null);
    setTier(2);
    setResponseData(null);
    setCopied(false);
  };

  const handleClose = () => {
    reset();
    onClose?.();
  };

  // Step 1 → parse + validate offer shape
  const handleSubmitOffer = () => {
    setError(null);
    if (!offerText.trim()) {
      setError(t('circles.pairOfferRequired'));
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(offerText);
    } catch {
      setError(t('circles.pairOfferMalformed'));
      return;
    }
    for (const key of ['initiator_pubkey', 'signature', 'nonce', 'display_name']) {
      if (typeof parsed[key] !== 'string') {
        setError(t('circles.pairOfferMissingField', { field: key }));
        return;
      }
    }
    // Expiry check — server also enforces (±60s timestamp + expires_at)
    // but a client-side hint beats a server roundtrip for the common case.
    const now = Math.floor(Date.now() / 1000);
    if (typeof parsed.expires_at === 'number' && parsed.expires_at < now) {
      setError(t('circles.pairOfferExpired'));
      return;
    }
    setParsedOffer(parsed);
    setStep(STEP_PICK_TIER);
  };

  // Step 2 → accept offer with chosen tier
  const handleAccept = async () => {
    if (!parsedOffer) return;
    try {
      setLoading(true);
      setError(null);
      const resp = await apiClient.post('/api/federation/pair/accept', {
        offer: parsedOffer,
        my_tier_for_you: tier,
      });
      setResponseData(resp.data);
      setStep(STEP_SHOW_RESPONSE);
      onPaired?.();
    } catch (err) {
      setError(err?.response?.data?.detail || t('circles.pairAcceptFailed'));
    } finally {
      setLoading(false);
    }
  };

  const handleCopyResponse = async () => {
    if (!responseData) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(responseData));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* Non-fatal */
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title={t('circles.pairAcceptTitle')}>
      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}

      {step === STEP_PASTE_OFFER && (
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.pairAcceptStep1Instruction')}
          </p>
          <div>
            <label htmlFor="pair-offer" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('circles.pairOffer')}
            </label>
            <textarea
              id="pair-offer"
              value={offerText}
              onChange={(e) => setOfferText(e.target.value)}
              rows={4}
              className="input font-mono text-xs"
              placeholder='{"initiator_pubkey":"...","nonce":"...","signature":"..."}'
            />
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={handleClose} className="btn-secondary px-4 py-2 rounded-lg">
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={handleSubmitOffer}
              className="btn-primary px-4 py-2 rounded-lg"
            >
              {t('common.continue')}
            </button>
          </div>
        </div>
      )}

      {step === STEP_PICK_TIER && parsedOffer && (
        <div className="space-y-4">
          <div>
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-1">
              {t('circles.pairAcceptStep2Instruction', {
                // Fallback to "Unknown peer" — the server's PairingOffer
                // schema has no min_length on display_name, so "" can
                // pass Pydantic. Don't render "Accept pairing with ."
                name: parsedOffer.display_name || t('circles.pairUnknownPeer'),
              })}
            </p>
            <code className="block text-xs text-gray-500 dark:text-gray-400 truncate">
              {parsedOffer.initiator_pubkey}
            </code>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              {t('circles.pairTierForThem')}
            </label>
            <TierPicker value={tier} onChange={setTier} disabled={loading} />
          </div>
          <div className="flex justify-between gap-2">
            <button
              type="button"
              onClick={() => setStep(STEP_PASTE_OFFER)}
              className="btn-secondary inline-flex items-center gap-1 px-4 py-2 rounded-lg"
            >
              <ArrowLeft className="w-4 h-4" />
              {t('common.back')}
            </button>
            <button
              type="button"
              onClick={handleAccept}
              disabled={loading}
              className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50"
            >
              {loading ? t('common.loading') : t('circles.pairAcceptButton')}
            </button>
          </div>
        </div>
      )}

      {step === STEP_SHOW_RESPONSE && responseData && (
        <div className="space-y-4">
          <div>
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-3">
              {t('circles.pairAcceptStep3Instruction')}
            </p>
            <div
              role="img"
              aria-label={t('circles.pairQrCodeResponseAria')}
              className="flex justify-center py-4 bg-white rounded-lg border border-gray-200"
            >
              <QRCodeSVG value={JSON.stringify(responseData)} size={220} level="M" />
            </div>
            <div className="mt-3 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              <Fingerprint className="w-3 h-3" aria-hidden="true" />
              <code className="tabular-nums">{responseData.responder_pubkey.slice(0, 24)}…</code>
              <button
                type="button"
                onClick={handleCopyResponse}
                className="ml-auto btn-icon btn-icon-ghost"
                title={t('circles.pairCopyJson')}
                aria-label={t('circles.pairCopyJson')}
              >
                {copied ? <Check className="w-4 h-4 text-green-600" /> : <Copy className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {t('circles.pairAcceptStep3Hint')}
          </p>
          <div className="flex justify-end">
            <button
              type="button"
              onClick={handleClose}
              className="btn-primary px-4 py-2 rounded-lg"
            >
              {t('common.done')}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
