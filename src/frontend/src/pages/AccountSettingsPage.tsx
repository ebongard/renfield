/**
 * My account -- the settings that belong to the person themselves.
 *
 * WHY THIS PAGE EXISTS
 * Consenting to the voice as a second login factor is a consent to processing
 * biometric data (Art. 9 GDPR). Until 2026-09-28 it was reachable only through
 * user administration, i.e. only for accounts with `users.manage` -- a
 * household member could neither consent nor even see whether the factor
 * applies to their account. A consent only a third party can give is not one.
 *
 * The asymmetry stays: turning it on only for yourself (that is here), turning
 * it off on ANOTHER account still requires `admin` and happens in
 * administration.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ShieldCheck, Shield, Mic, Loader, UserCircle } from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { useConfirmDialog } from '../components/ConfirmDialog';
import {
  isVoiceFactorBlocker,
  useSetVoiceSecondFactor,
  useVoiceSecondFactorState,
} from '../api/resources/users';
import { extractApiError } from '../utils/axios';
import Alert from '../components/Alert';
import PageHeader from '../components/PageHeader';

export default function AccountSettingsPage() {
  const { t } = useTranslation();
  const { user, fetchUser } = useAuth();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();
  const voiceState = useVoiceSecondFactorState(user?.id);
  const setVoiceSecondFactor = useSetVoiceSecondFactor();

  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  // For turning it off only. The server requires the password there because a
  // token from BEFORE the consent could otherwise revoke it.
  const [password, setPassword] = useState('');
  const [askPassword, setAskPassword] = useState(false);

  const armed = !!user?.voice_second_factor_enabled;

  /**
   * THE REASON COMES FROM THE SERVER, AND IT HOLDS REGARDLESS OF THE CONSENT.
   *
   * Previously this page read only the feature flags -- the INSTANCE-WIDE half
   * of the preconditions -- and showed a reason only in the "armed" state.
   * Anyone who had not consented yet saw a blank "off", no matter how many
   * preconditions were missing, and was offered a button that was bound to
   * fail with a 409. Measured in the household on 2026-09-29: on 6 of 7
   * accounts `no_profile` was the blocker -- precisely the account-bound half
   * this page could not see.
   *
   * While the answer is outstanding, NOTHING is concluded: an unknown answer
   * is no evidence, neither for nor against a blocker.
   */
  const blocker = voiceState.data?.blocker ?? null;
  const blockerText = blocker ? t(`account.voiceBlocker.${blocker}`) : null;

  /**
   * What prevents TURNING IT ON -- not the same as "something is in the way".
   * `voice_path_off` deliberately does NOT block it: the cutover order is
   * collect the consents first, then flip the flag. The server sees it the
   * same way (`POST .../voice-second-factor` does not check
   * `voice_auth_enabled`).
   */
  const armingBlocked = blocker !== null && blocker !== 'voice_path_off';

  const handleToggle = async () => {
    if (!user) return;
    // Turning it off: ask for the password first, then confirm.
    if (armed && !askPassword) {
      setAskPassword(true);
      return;
    }
    const enabling = !armed;
    const confirmed = await confirm({
      title: enabling ? t('account.voiceArm') : t('account.voiceDisarm'),
      message: enabling ? t('account.voiceArmConfirm') : t('account.voiceDisarmConfirmSelf'),
      confirmLabel: enabling ? t('account.voiceArm') : t('account.voiceDisarm'),
      variant: 'warning',
    });
    if (!confirmed) return;

    setError(null);
    setSuccess(null);
    try {
      await setVoiceSecondFactor.mutateAsync({
        id: user.id,
        enabled: enabling,
        currentPassword: enabling ? undefined : password,
      });
      setPassword('');
      setAskPassword(false);
      // Re-read `/auth/me`: this page's state comes from there, not from the
      // mutation's response -- otherwise the page shows something other than
      // Sitzung glaubt.
      await fetchUser();
      setSuccess(enabling ? t('account.voiceArmed') : t('account.voiceDisarmed'));
    } catch (err) {
      // The 409 response carries the CODE, not the sentence -- otherwise raw
      // text like "no_profile" would end up in the UI. It is translated here,
      // from the same vocabulary as the state above.
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      setError(
        isVoiceFactorBlocker(detail)
          ? t(`account.voiceBlocker.${detail}`)
          : extractApiError(err, t('account.voiceFailed')),
      );
    }
  };

  return (
    <div className="p-6">
      <PageHeader icon={UserCircle} title={t('account.title')} subtitle={t('account.subtitle')} />

      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}
      {success && <Alert variant="success" onClose={() => setSuccess(null)}>{success}</Alert>}

      <div className="card mt-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1">
            <h2 className="flex items-center gap-2 text-lg font-medium text-gray-900 dark:text-white">
              <Mic className="w-5 h-5" aria-hidden="true" />
              {t('account.voiceTitle')}
            </h2>
            <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
              {t('account.voiceExplain')}
            </p>

            {/* Symbol AND text -- colour is never the only signal (DESIGN.md). */}
            <p
              className={`mt-3 flex items-center gap-2 text-sm font-medium ${
                armed && !blocker
                  ? 'text-amber-600 dark:text-amber-400'
                  : 'text-gray-500 dark:text-gray-400'
              }`}
              aria-live="polite"
            >
              {armed ? (
                <ShieldCheck className="w-4 h-4" aria-hidden="true" />
              ) : (
                <Shield className="w-4 h-4" aria-hidden="true" />
              )}
              {!armed
                ? t('account.voiceStateOff')
                : blocker
                  ? t('account.voiceStateResting')
                  : t('account.voiceStateOn')}
            </p>

            {/* The REASON, in BOTH states. It is the only hint as to why
                turning it on would fail -- previously the person only learned
                it after pressing, and in English. */}
            {blockerText && (
              <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">
                {blockerText}
              </p>
            )}

            {armed && (
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                {t('account.voiceNoFallback')}
              </p>
            )}

            {askPassword && (
              <div className="mt-4">
                <label
                  htmlFor="voice-disarm-password"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  {t('account.voicePasswordLabel')}
                </label>
                <input
                  id="voice-disarm-password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="input w-full max-w-sm min-h-11"
                />
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  {t('account.voicePasswordWhy')}
                </p>
              </div>
            )}
          </div>

          <button
            onClick={handleToggle}
            disabled={
              setVoiceSecondFactor.isPending
              || !user
              || (askPassword && !password)
              // Turning it on where the server MUST refuse with a 409 is not
              // offered in the first place. Turning it off stays possible
              // always -- that is the documented way back and must not depend
              // on any precondition (there is deliberately no fallback to the
              // password alone).
              || (!armed && armingBlocked)
            }
            title={!armed && armingBlocked && blockerText ? blockerText : undefined}
            className={armed ? 'btn-secondary min-h-11' : 'btn-primary min-h-11'}
          >
            {setVoiceSecondFactor.isPending && (
              <Loader className="w-4 h-4 mr-2 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            )}
            {armed ? t('account.voiceDisarm') : t('account.voiceArm')}
          </button>
        </div>
      </div>

      {ConfirmDialogComponent}
    </div>
  );
}
