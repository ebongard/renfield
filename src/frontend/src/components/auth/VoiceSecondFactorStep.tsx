/**
 * The second step of the login: record the voice and confirm.
 *
 * Appears when `/auth/login` throws `SecondFactorRequired` -- the password was
 * correct, the login is not finished. This screen records and sends ticket +
 * recording to `POST /api/auth/voice`.
 *
 * WHAT IS DELIBERATELY NOT HERE
 * ------------------------------
 * The server answers EVERY failure identically
 * (`{"success": false, "message": "Voice authentication failed"}`) -- no field
 * distinguishes "ticket expired" from "voice does not match" from "account
 * locked". That is the oracle protection, and this screen must not undermine
 * it: it does NOT pass the server message through and does not guess which
 * case it was.
 *
 * Only things that belong to the USER are distinguishable: a denied
 * microphone, a missing device, a browser that cannot record. Those are not
 * the server's secrets, and somebody who does not know they denied the
 * microphone cannot allow it.
 *
 * AND WHAT DOES NOT EXIST: a way around. There is no "sign in without voice"
 * button, not even after several failed attempts -- a fallback an attacker can
 * trigger themselves cancels the factor. After the third attempt the screen
 * names the intended path: an administrator turns the factor off for this
 * account.
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Check, Mic, Square } from 'lucide-react';

import apiClient from '../../utils/axios';
import {
  MAX_DURATION_MS,
  MIN_DURATION_MS,
  useVoiceFactorRecording,
} from '../../hooks/useVoiceFactorRecording';

interface Props {
  /** The one-shot ticket from `/auth/login`. */
  ticket: string;
  /** Both factors passed -- the caller fetches the user and navigates. */
  onVerified: () => void | Promise<void>;
  /** Cancel: back to the password form. The ticket expires on its own. */
  onCancel: () => void;
}

/** From this many failed attempts on, the recovery path is named. */
const HINT_AFTER_ATTEMPTS = 3;

export default function VoiceSecondFactorStep({ ticket, onVerified, onCancel }: Props) {
  const { t } = useTranslation();
  const rec = useVoiceFactorRecording();
  const [submitting, setSubmitting] = useState(false);
  const [refused, setRefused] = useState(false);
  const [attempts, setAttempts] = useState(0);
  const recordButtonRef = useRef<HTMLButtonElement | null>(null);

  // Focus the record button as soon as the step appears: somebody signing in
  // with the keyboard should not have to hunt for it.
  useEffect(() => {
    recordButtonRef.current?.focus();
  }, []);

  const submit = async () => {
    if (!rec.blob) return;
    setSubmitting(true);
    setRefused(false);
    try {
      const form = new FormData();
      form.append('ticket', ticket);
      form.append('audio_file', rec.blob, 'second-factor.webm');
      // The header MUST be set. `apiClient` carries
      // `Content-Type: application/json` as its default (`utils/axios.ts`),
      // and axios only strips that for FormData when it is NOT set
      // explicitly. Without this line the body is multipart while the header
      // claims JSON, and FastAPI answers 422 -- the recording is never
      // checked.
      // All four other uploads in the project do the same
      // (`api/resources/speakers.ts`, `useAudioRecording`, `useDocumentUpload`).
      const { data } = await apiClient.post('/api/auth/voice', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      if (data?.success) {
        await onVerified();
        return;
      }
      // `data.message` is NOT displayed. It is deliberately uninformative,
      // and passing it through would only add noise -- or, if the server ever
      // makes it more forthcoming, an oracle in
      // the UI.
      setRefused(true);
      setAttempts((n) => n + 1);
    } catch {
      setRefused(true);
      setAttempts((n) => n + 1);
    } finally {
      setSubmitting(false);
      rec.reset();
    }
  };

  const deviceError = rec.error;
  const showRecoveryHint = attempts >= HINT_AFTER_ATTEMPTS;
  const progress = Math.min(1, rec.elapsedMs / MAX_DURATION_MS);

  return (
    <div className="card p-6 space-y-5" aria-labelledby="v2f-title">
      <div className="space-y-1">
        <h2 id="v2f-title" className="text-lg font-bold text-gray-900 dark:text-gray-100">
          {t('auth.voiceFactor.title')}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('auth.voiceFactor.intro', { seconds: Math.round(MIN_DURATION_MS / 1000) })}
        </p>
      </div>

      {/* Level meter. Without a motion preference just a static bar --
          `motion-reduce` covers exactly that (DESIGN.md §Motion). */}
      <div
        className="h-2 w-full rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden"
        role="presentation"
      >
        <div
          className="h-full rounded-full bg-primary-500 transition-[width] duration-100 motion-reduce:transition-none"
          style={{ width: `${(rec.recording ? Math.max(progress, rec.level * 0.15) : progress) * 100}%` }}
        />
      </div>

      {/* State for screen readers. `polite`, so it does not interrupt input
          (DESIGN.md §Accessibility). */}
      <p aria-live="polite" className="sr-only">
        {rec.recording
          ? t('auth.voiceFactor.srRecording')
          : rec.blob
            ? t('auth.voiceFactor.srCaptured')
            : ''}
      </p>

      {/* Both notices use the same visual language as `.merge-notice`
          (DESIGN.md: warm = attention, symbol + text so colour is never the
          only signal) -- but NOT that class: it is explicitly defined there as
          a merge notice, and DESIGN.md requires a class name to carry ONE
          concept. */}
      {deviceError && (
        <div
          className="flex items-start gap-2 rounded-sm px-2 py-1 text-xs font-medium bg-cream text-primary-700 border border-primary-200 dark:bg-gray-800 dark:text-primary-300 dark:border-primary-700"
          role="alert"
        >
          <AlertTriangle className="w-4 h-4 shrink-0" aria-hidden="true" />
          <span>{t(`auth.voiceFactor.deviceError.${deviceError}`)}</span>
        </div>
      )}

      {refused && !deviceError && (
        <div className="flex items-start gap-2 rounded-sm px-2 py-1 text-xs font-medium bg-cream text-primary-700 border border-primary-200 dark:bg-gray-800 dark:text-primary-300 dark:border-primary-700" role="alert">
          <AlertTriangle className="w-4 h-4 shrink-0" aria-hidden="true" />
          <span>
            {t('auth.voiceFactor.refused')}
            {showRecoveryHint && ` ${t('auth.voiceFactor.recoveryHint')}`}
          </span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        {!rec.recording && !rec.blob && (
          <button
            ref={recordButtonRef}
            type="button"
            onClick={() => void rec.start()}
            disabled={submitting}
            className="btn-primary inline-flex items-center gap-2 px-4 min-h-11"
          >
            <Mic className="w-4 h-4" aria-hidden="true" />
            {t('auth.voiceFactor.record')}
          </button>
        )}

        {rec.recording && (
          <button
            type="button"
            onClick={rec.stop}
            disabled={!rec.longEnough}
            className="btn-primary inline-flex items-center gap-2 px-4 min-h-11"
            // Before the minimum duration the button is disabled -- with a
            // reason, otherwise it just looks broken.
            title={rec.longEnough ? undefined : t('auth.voiceFactor.keepTalking')}
          >
            <Square className="w-4 h-4" aria-hidden="true" />
            {rec.longEnough
              ? t('auth.voiceFactor.stop')
              : t('auth.voiceFactor.keepTalking')}
          </button>
        )}

        {rec.blob && !rec.recording && (
          <>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={submitting}
              className="btn-primary inline-flex items-center gap-2 px-4 min-h-11"
            >
              <Check className="w-4 h-4" aria-hidden="true" />
              {submitting ? t('auth.voiceFactor.checking') : t('auth.voiceFactor.confirm')}
            </button>
            <button
              type="button"
              onClick={rec.reset}
              disabled={submitting}
              className="btn-secondary px-4 min-h-11"
            >
              {t('auth.voiceFactor.again')}
            </button>
          </>
        )}

        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="btn-secondary px-4 min-h-11 ml-auto"
        >
          {t('common.cancel')}
        </button>
      </div>
    </div>
  );
}
