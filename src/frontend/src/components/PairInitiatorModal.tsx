import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { ArrowLeft, Check, Copy, Fingerprint } from 'lucide-react';
import apiClient from '../utils/axios';
import { extractApiError } from '../utils/axios';
import Modal from './Modal';
import Alert from './Alert';
import TierPicker from './TierPicker';
import type { CircleTier } from './TierBadge';

/**
 * PairInitiatorModal — drives the asker side of the F2 handshake.
 *
 * Three steps:
 *   1. Generate a signed offer (POST /api/federation/pair/offer) →
 *      render it as a QR code + copyable JSON. The other device's
 *      responder modal scans or pastes this.
 *   2. Paste the responder's signed response JSON.
 *   3. Pick a tier for the responder (their_tier_for_me) and complete
 *      the handshake (POST /api/federation/pair/complete).
 *
 * Step 2 → 3 validation: response JSON must parse as an object with
 * `responder_pubkey` + `signature` + `nonce` echoing the one we
 * offered. Field name `signature` (not `responder_signature`) matches
 * the server's PairingResponse schema. The pair-anchor check happens
 * server-side; we validate shape locally for quick failure.
 */
type Step = 'offer' | 'await_response' | 'pick_tier' | 'done';

interface OfferData {
  initiator_pubkey: string;
  signature: string;
  nonce: string;
}

interface ResponseData {
  responder_pubkey: string;
  signature: string;
  nonce: string;
  responder_display_name?: string;
}

interface PairInitiatorModalProps {
  isOpen: boolean;
  onClose?: () => void;
  onPaired?: () => void;
}

const REQUIRED_RESPONSE_FIELDS = ['responder_pubkey', 'signature', 'nonce'] as const;

export default function PairInitiatorModal({ isOpen, onClose, onPaired }: PairInitiatorModalProps) {
  const { t } = useTranslation();

  const [step, setStep] = useState<Step>('offer');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [offer, setOffer] = useState<OfferData | null>(null);
  const [responseText, setResponseText] = useState('');
  const [parsedResponse, setParsedResponse] = useState<ResponseData | null>(null);
  const [tier, setTier] = useState<CircleTier>(2);

  const [copied, setCopied] = useState(false);

  const reset = () => {
    setStep('offer');
    setLoading(false);
    setError(null);
    setOffer(null);
    setResponseText('');
    setParsedResponse(null);
    setTier(2);
    setCopied(false);
  };

  const handleClose = () => {
    reset();
    onClose?.();
  };

  // Step 1 → generate offer
  const handleGenerateOffer = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await apiClient.post<OfferData>('/api/federation/pair/offer', {});
      setOffer(response.data);
      setStep('await_response');
    } catch (err) {
      setError(extractApiError(err, t('circles.pairOfferFailed')));
    } finally {
      setLoading(false);
    }
  };

  const handleCopyOffer = async () => {
    if (!offer) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(offer));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Non-fatal — user can still read QR.
    }
  };

  // Step 2 → parse + validate pasted response
  const handleSubmitResponse = () => {
    setError(null);
    if (!responseText.trim()) {
      setError(t('circles.pairResponseRequired'));
      return;
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(responseText) as Record<string, unknown>;
    } catch {
      setError(t('circles.pairResponseMalformed'));
      return;
    }
    // Minimal shape check — server does the cryptographic verification.
    // Server's PairingResponse schema (services/pairing_service.py) names
    // the signature field `signature`, NOT `responder_signature`. Getting
    // this wrong would reject every legitimate response client-side.
    for (const key of REQUIRED_RESPONSE_FIELDS) {
      if (typeof parsed[key] !== 'string') {
        setError(t('circles.pairResponseMissingField', { field: key }));
        return;
      }
    }
    if (parsed.nonce !== offer?.nonce) {
      setError(t('circles.pairResponseWrongNonce'));
      return;
    }
    setParsedResponse(parsed as unknown as ResponseData);
    setStep('pick_tier');
  };

  // Step 3 → complete handshake with tier
  const handleComplete = async () => {
    if (!parsedResponse) return;
    try {
      setLoading(true);
      setError(null);
      await apiClient.post('/api/federation/pair/complete', {
        response: parsedResponse,
        their_tier_for_me: tier,
      });
      setStep('done');
      onPaired?.();
    } catch (err) {
      setError(extractApiError(err, t('circles.pairCompleteFailed')));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title={t('circles.pairInitiateTitle')}>
      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}

      {step === 'offer' && (
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('circles.pairInitiateStep1Explanation')}
          </p>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={handleClose} className="btn-secondary px-4 py-2 rounded-lg">
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={handleGenerateOffer}
              disabled={loading}
              className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50"
            >
              {loading ? t('common.loading') : t('circles.pairGenerateOffer')}
            </button>
          </div>
        </div>
      )}

      {step === 'await_response' && offer && (
        <div className="space-y-4">
          <div>
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-3">
              {t('circles.pairInitiateStep2Instruction')}
            </p>
            <div
              role="img"
              aria-label={t('circles.pairQrCodeOfferAria')}
              className="flex justify-center py-4 bg-white rounded-lg border border-gray-200"
            >
              <QRCodeSVG value={JSON.stringify(offer)} size={220} level="M" />
            </div>
            <div className="mt-3 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              <Fingerprint className="w-3 h-3" aria-hidden="true" />
              <code className="tabular-nums">{offer.initiator_pubkey.slice(0, 24)}…</code>
              <button
                type="button"
                onClick={handleCopyOffer}
                className="ml-auto btn-icon btn-icon-ghost"
                title={t('circles.pairCopyJson')}
                aria-label={t('circles.pairCopyJson')}
              >
                {copied ? <Check className="w-4 h-4 text-green-600" /> : <Copy className="w-4 h-4" />}
              </button>
            </div>
          </div>

          <div>
            <label htmlFor="pair-response" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('circles.pairPasteResponse')}
            </label>
            <textarea
              id="pair-response"
              value={responseText}
              onChange={(e) => setResponseText(e.target.value)}
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
              onClick={handleSubmitResponse}
              className="btn-primary px-4 py-2 rounded-lg"
            >
              {t('common.continue')}
            </button>
          </div>
        </div>
      )}

      {step === 'pick_tier' && parsedResponse && (
        <div className="space-y-4">
          <div>
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-1">
              {t('circles.pairInitiateStep3Instruction', {
                name: parsedResponse.responder_display_name || t('circles.pairUnknownPeer'),
              })}
            </p>
            <code className="block text-xs text-gray-500 dark:text-gray-400 truncate">
              {parsedResponse.responder_pubkey}
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
              onClick={() => setStep('await_response')}
              className="btn-secondary inline-flex items-center gap-1 px-4 py-2 rounded-lg"
            >
              <ArrowLeft className="w-4 h-4" />
              {t('common.back')}
            </button>
            <button
              type="button"
              onClick={handleComplete}
              disabled={loading}
              className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50"
            >
              {loading ? t('common.loading') : t('circles.pairCompleteButton')}
            </button>
          </div>
        </div>
      )}

      {step === 'done' && (
        <div className="space-y-4 text-center py-4">
          <Check className="w-12 h-12 mx-auto text-green-600" aria-hidden="true" />
          <p className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('circles.pairSuccess')}
          </p>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('circles.pairSuccessHint')}
          </p>
          <div className="flex justify-center">
            <button
              type="button"
              onClick={handleClose}
              className="btn-primary px-4 py-2 rounded-lg"
            >
              {t('common.close')}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
