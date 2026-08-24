/**
 * Change Password Page
 *
 * Serves the FORCED-rotation flow (login audit): while the authenticated user
 * carries must_change_password, ProtectedRoute redirects here and the backend
 * 403s every other route. The user cannot leave until the password is changed.
 * Also usable as a voluntary change (linked from settings) — the mandatory
 * banner only shows when must_change_password is set.
 */
import { FormEvent, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { AlertCircle, Eye, EyeOff, KeyRound, Loader } from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { extractApiError } from '../utils/axios';

export default function ChangePasswordPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { user, changePassword, fetchUser } = useAuth();

  const mandatory = Boolean(user?.must_change_password);

  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!current || !next || !confirm) {
      setError(t('auth.fillAllRequiredFields'));
      return;
    }
    if (next.length < 8) {
      setError(t('auth.passwordTooShort'));
      return;
    }
    if (next !== confirm) {
      setError(t('auth.passwordsDoNotMatch'));
      return;
    }
    if (next === current) {
      setError(t('auth.newPasswordSameAsCurrent'));
      return;
    }

    setSubmitting(true);
    try {
      await changePassword(current, next);
      // Re-fetch so must_change_password clears in context → the gate releases.
      await fetchUser();
      navigate('/', { replace: true });
    } catch (err) {
      setError(extractApiError(err, t('auth.changePasswordFailed')));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 px-4">
      <div className="card w-full max-w-md p-8">
        <div className="flex flex-col items-center mb-6">
          <div className="w-12 h-12 rounded-full bg-primary-100 dark:bg-primary-900/40 flex items-center justify-center mb-3">
            <KeyRound className="w-6 h-6 text-primary-600 dark:text-primary-400" />
          </div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-white">
            {t('auth.changePasswordTitle')}
          </h1>
        </div>

        {mandatory && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 p-3 mb-5"
          >
            <AlertCircle className="w-5 h-5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-semibold text-amber-800 dark:text-amber-200">
                {t('auth.changePasswordRequired')}
              </p>
              <p className="text-sm text-amber-700 dark:text-amber-300">
                {t('auth.changePasswordRequiredHint')}
              </p>
            </div>
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="flex items-center gap-2 rounded-md bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 p-3 mb-4 text-sm text-red-700 dark:text-red-300"
          >
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="cp-current" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('auth.currentPassword')}
            </label>
            <input
              id="cp-current"
              type={show ? 'text' : 'password'}
              className="input w-full"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              placeholder={t('auth.enterCurrentPassword')}
            />
          </div>

          <div>
            <label htmlFor="cp-new" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('auth.newPassword')}
            </label>
            <div className="relative">
              <input
                id="cp-new"
                type={show ? 'text' : 'password'}
                className="input w-full pr-10"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                placeholder={t('auth.enterNewPassword')}
              />
              <button
                type="button"
                onClick={() => setShow((s) => !s)}
                className="absolute inset-y-0 right-0 px-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                aria-label={show ? 'hide' : 'show'}
              >
                {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t('auth.atLeast8Chars')}</p>
          </div>

          <div>
            <label htmlFor="cp-confirm" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('auth.confirmNewPassword')}
            </label>
            <input
              id="cp-confirm"
              type={show ? 'text' : 'password'}
              className="input w-full"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder={t('auth.confirmNewPassword')}
            />
          </div>

          <button type="submit" disabled={submitting} className="btn-primary w-full flex items-center justify-center gap-2">
            {submitting ? <Loader className="w-4 h-4 animate-spin" /> : <KeyRound className="w-4 h-4" />}
            {t('auth.changePasswordSubmit')}
          </button>
        </form>
      </div>
    </div>
  );
}
