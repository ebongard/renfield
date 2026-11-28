/**
 * Own number for calls (#1683, Telefonie Phase 4) -- on "Mein Konto".
 *
 * Renders NOTHING unless the instance reports outbound calls as active
 * (`telephony_outbound_enabled`). The number only counts after a code call:
 * "Bestätigungsanruf" rings it and Renfield speaks a 4-digit code, which the
 * user types here. Until then nothing is stored and Renfield never calls it.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader, Phone, PhoneCall } from 'lucide-react';

import { useFeatureFlags } from '../api/resources/brain';
import {
  useCallNumberQuery,
  useDeleteCallNumber,
  useStartCallNumber,
  useVerifyCallNumber,
} from '../api/resources/preferences';
import { extractApiError } from '../utils/axios';
import Alert from './Alert';

export default function CallNumberCard() {
  const { t } = useTranslation();
  const { data: flags } = useFeatureFlags();
  const active = flags?.telephony_outbound_enabled === true;

  const status = useCallNumberQuery(active);
  const start = useStartCallNumber();
  const verify = useVerifyCallNumber();
  const remove = useDeleteCallNumber();

  const [number, setNumber] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  if (!active) return null;

  const data = status.data;
  const pending = data?.pending ?? null;
  const busy = start.isPending || verify.isPending || remove.isPending;

  const onStart = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSuccess(null);
    try {
      await start.mutateAsync(number);
      setCode('');
      setSuccess(t('callNumber.calling'));
    } catch (err) {
      setError(extractApiError(err, t('callNumber.startFailed')));
    }
  };

  const onVerify = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSuccess(null);
    try {
      await verify.mutateAsync(code);
      setNumber('');
      setCode('');
      setSuccess(t('callNumber.confirmed'));
    } catch (err) {
      setError(extractApiError(err, t('callNumber.wrongCode')));
    }
  };

  const onRemove = async () => {
    setError(null);
    setSuccess(null);
    try {
      await remove.mutateAsync();
      setSuccess(t('callNumber.removed'));
    } catch (err) {
      setError(extractApiError(err, t('common.error')));
    }
  };

  return (
    <div className="card mt-4">
      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}
      {success && <Alert variant="success" onClose={() => setSuccess(null)}>{success}</Alert>}

      <h2 className="flex items-center gap-2 text-lg font-medium text-gray-900 dark:text-white">
        <Phone className="w-5 h-5" aria-hidden="true" />
        {t('callNumber.title')}
      </h2>
      <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">{t('callNumber.explain')}</p>

      {data?.number && (
        <div className="mt-3 flex items-center justify-between gap-4">
          <p className="flex items-center gap-2 text-sm font-medium text-accent-700 dark:text-accent-300">
            <PhoneCall className="w-4 h-4 shrink-0" aria-hidden="true" />
            <span className="tabular-nums">{t('callNumber.current', { number: data.number })}</span>
          </p>
          <button type="button" onClick={onRemove} disabled={busy} className="btn-secondary min-h-11">
            {t('callNumber.remove')}
          </button>
        </div>
      )}

      <form onSubmit={onStart} className="mt-4 flex flex-wrap items-end gap-3">
        <label className="flex-1 min-w-48 text-sm text-gray-700 dark:text-gray-300">
          {data?.number ? t('callNumber.changeLabel') : t('callNumber.numberLabel')}
          <input
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            value={number}
            onChange={(e) => setNumber(e.target.value)}
            placeholder="0170 1234567"
            className="input mt-1 w-full"
          />
        </label>
        <button type="submit" disabled={busy || number.trim().length < 3} className="btn-primary min-h-11">
          {start.isPending && (
            <Loader className="w-4 h-4 mr-2 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          )}
          {t('callNumber.start')}
        </button>
      </form>

      {pending && (
        <form onSubmit={onVerify} className="mt-4 flex flex-wrap items-end gap-3" aria-live="polite">
          <label className="flex-1 min-w-48 text-sm text-gray-700 dark:text-gray-300">
            {t('callNumber.codeLabel', { number: pending })}
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{4}"
              maxLength={4}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              className="input mt-1 w-full tabular-nums tracking-widest"
            />
          </label>
          <button type="submit" disabled={busy || code.length !== 4} className="btn-primary min-h-11">
            {verify.isPending && (
              <Loader className="w-4 h-4 mr-2 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            )}
            {t('callNumber.confirm')}
          </button>
        </form>
      )}
    </div>
  );
}
