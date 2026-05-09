/**
 * Login Page
 *
 * Provides login form and optional registration link.
 *
 * W10 — first page migrated to TypeScript. Pattern for subsequent pages:
 *   - Import already-typed dependencies (AuthContext, axios utils, etc.).
 *   - Type useState calls with the field's actual shape (string vs string|null).
 *   - Type form/input event handlers with React's FormEvent /ChangeEvent.
 *   - Type useLocation's state shape narrowly (location.state is unknown).
 */
import { FormEvent, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useLocation, useNavigate } from 'react-router';
import { AlertCircle, Eye, EyeOff, Loader, LogIn } from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { extractApiError } from '../utils/axios';

// react-router's `useLocation().state` is typed as `unknown`. Narrow it
// here so the redirect-after-login path is exercised through a real
// type, not a runtime guess.
interface LocationStateWithFrom {
  from?: { pathname?: string };
}

export default function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const {
    login,
    isAuthenticated,
    authEnabled,
    allowRegistration,
    loading: authLoading,
  } = useAuth();

  const [username, setUsername] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [showPassword, setShowPassword] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Redirect path from location state, default to home.
  const fromState = (location.state as LocationStateWithFrom | null) ?? null;
  const from = fromState?.from?.pathname ?? '/';

  // OIDC SSO flag — backend route mounting and frontend button visibility
  // are independently gated. The frontend flag must match the backend's
  // REVA_OIDC_ENABLED for the dance to actually work end-to-end. When the
  // backend is on but the frontend flag is off, the button is hidden but
  // direct navigation to /auth/oidc/login still works.
  const oidcEnabled = import.meta.env.VITE_OIDC_ENABLED === 'true';

  // Surface OIDC error redirects from the backend. After a failed dance
  // /auth/oidc/callback redirects to /login?error=<code> where <code> is
  // one of the documented values (oidc_state_invalid, oidc_token_invalid,
  // oidc_code_invalid, oidc_disabled, oidc_idp_unreachable, oidc_idp_error,
  // oidc_cancelled, oidc_internal). Render the localized message above
  // the credentials form so the user knows what happened.
  const oidcErrorCode = (() => {
    const code = new URLSearchParams(location.search).get('error');
    return code && code.startsWith('oidc_') ? code : null;
  })();

  // Redirect if already authenticated or auth is disabled
  useEffect(() => {
    if (!authLoading && (isAuthenticated || !authEnabled)) {
      navigate(from, { replace: true });
    }
  }, [isAuthenticated, authEnabled, authLoading, navigate, from]);

  // Clear error after 5 seconds
  useEffect(() => {
    if (error) {
      const timer = setTimeout(() => setError(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [error]);

  const handleSubmit = async (e: FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    if (!username || !password) {
      setError(t('auth.enterCredentials'));
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await login(username, password);
      navigate(from, { replace: true });
    } catch (err: unknown) {
      setError(extractApiError(err, t('auth.loginFailed')));
    } finally {
      setLoading(false);
    }
  };

  // Show loading while checking auth status
  if (authLoading) {
    return (
      <div className="min-h-screen bg-[#0f1117] flex items-center justify-center">
        <Loader className="w-8 h-8 animate-spin text-primary-500" />
      </div>
    );
  }

  // Edition-aware variants. ``pro`` is the white-label enterprise build
  // (Reva for X-IDRA): tone the dramatic radial-vignette down to a subtle
  // cool-steel gradient, suppress the self-register link, prepend tenant
  // context to the hero, and surface compliance trust markers in the footer.
  // ``community`` (default) keeps the household-warm aesthetic untouched.
  const isPro = import.meta.env.VITE_APP_EDITION === 'pro';
  const tenantName = (import.meta.env.VITE_APP_TENANT_NAME || '').toString();
  const appName = import.meta.env.VITE_APP_NAME || 'Renfield';

  // Pro background: subtle linear gradient instead of theatrical radial
  // vignette + noise. Banking customers expect restrained surfaces, not
  // consumer-app drama. Community keeps the warm-editorial radial.
  const bgClass = isPro
    ? 'min-h-screen bg-gradient-to-b from-slate-900 via-[#0f1419] to-slate-950 flex items-center justify-center px-4 relative'
    : 'min-h-screen bg-[radial-gradient(ellipse_at_center,_rgba(0,255,208,0.08)_0%,_#0f1117_70%)] flex items-center justify-center px-4 relative';

  return (
    <div className={bgClass}>
      {/* Noise overlay — community only; pro reads cleaner without it */}
      {!isPro && (
        <div
          className="absolute inset-0 opacity-[0.03] pointer-events-none"
          style={{
            backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='1'/%3E%3C/svg%3E")`,
          }}
        />
      )}

      <div className="max-w-md w-full relative z-10">
        {/* Brand lockup: logo above wordmark; tenant context (when present)
            in muted cream below. Pro edition uses the existing white-label
            VITE_APP_LOGO_URL — the Reva asset is a horizontal lockup that
            already contains the wordmark, so we display it at h-20 w-auto
            and skip the H1 to avoid rendering "Reva" twice. Community keeps
            the square icon + Cormorant H1 lockup that Renfield ships with. */}
        <div className="text-center mb-8">
          {isPro ? (
            <img
              src={import.meta.env.VITE_APP_LOGO_URL || '/reva-logo.png'}
              alt={appName}
              className="h-20 w-auto mx-auto mb-4"
            />
          ) : (
            <>
              <img src="/logo-icon.svg" alt="" className="w-20 h-20 mx-auto mb-4" aria-hidden="true" />
              <h1 className="text-4xl font-bold font-display text-cream">{appName}</h1>
            </>
          )}
          {isPro && tenantName ? (
            <p className="text-gray-400 mt-1 text-sm">
              {t('auth.tenantPrefix')} <span className="text-cream/90">{tenantName}</span>
            </p>
          ) : null}
          <p className="text-gray-400 mt-2">{t('auth.signInToAccount')}</p>
        </div>

        {/* Login Card */}
        <div className="card-primary bg-gray-900 border-gray-700">
          {/* OIDC error banner — shown when /auth/oidc/callback redirected
              here with ?error=<code>. Distinct from the form `error` state
              so a fresh form-submit error doesn't suppress the OIDC banner
              the user just landed with. */}
          {oidcErrorCode && (
            <div className="bg-amber-900/20 border border-amber-700 rounded-lg p-4 mb-6">
              <div className="flex items-center space-x-3">
                <AlertCircle className="w-5 h-5 text-amber-500 shrink-0" />
                <p className="text-amber-400">
                  {t(`auth.oidcError.${oidcErrorCode}`, t('auth.oidcError.oidc_internal'))}
                </p>
              </div>
            </div>
          )}

          {/* Error Alert */}
          {error && (
            <div className="bg-red-900/20 border border-red-700 rounded-lg p-4 mb-6">
              <div className="flex items-center space-x-3">
                <AlertCircle className="w-5 h-5 text-red-500 shrink-0" />
                <p className="text-red-400">{error}</p>
              </div>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-6">
            {/* Username */}
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.username')}
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('auth.enterUsername')}
                className="input w-full bg-gray-800 border-gray-600 text-gray-100 placeholder-gray-500"
                autoComplete="username"
                autoFocus
                disabled={loading}
              />
            </div>

            {/* Password */}
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.password')}
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={t('auth.enterPassword')}
                  className="input w-full pr-10 bg-gray-800 border-gray-600 text-gray-100 placeholder-gray-500"
                  autoComplete="current-password"
                  disabled={loading}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-300"
                  tabIndex={-1}
                >
                  {showPassword ? (
                    <EyeOff className="w-5 h-5" />
                  ) : (
                    <Eye className="w-5 h-5" />
                  )}
                </button>
              </div>
            </div>

            {/* Submit Button */}
            <button
              type="submit"
              disabled={loading}
              className="w-full btn btn-primary py-3 flex items-center justify-center space-x-2"
            >
              {loading ? (
                <Loader className="w-5 h-5 animate-spin" />
              ) : (
                <>
                  <LogIn className="w-5 h-5" />
                  <span>{t('auth.signIn')}</span>
                </>
              )}
            </button>
          </form>

          {/* OIDC SSO button — full-page navigation (not Link/navigate)
              because /auth/oidc/login responds 302 to the IdP, which is
              outside our origin and can't be handled by react-router.
              The backend's `_redirect_login_error` brings the user back
              to /login?error=<code> on failure; the OIDC error banner
              above renders the localized copy. */}
          {oidcEnabled && (
            <>
              <div className="relative my-6" aria-hidden="true">
                <div className="absolute inset-0 flex items-center">
                  <div className="w-full border-t border-gray-700" />
                </div>
                <div className="relative flex justify-center text-xs">
                  <span className="bg-gray-900 px-3 text-gray-500 uppercase tracking-wider">
                    {t('auth.ssoOrDivider')}
                  </span>
                </div>
              </div>
              <a
                href="/auth/oidc/login"
                className="w-full btn bg-white text-gray-900 hover:bg-gray-100 py-3 flex items-center justify-center space-x-3 font-medium"
              >
                {/* Microsoft logo (4-square mark). Inline SVG keeps the
                    button self-contained — no extra asset request, no
                    icon-library dep. The 4 brand hex values are fixed by
                    Microsoft's brand guidelines and not localized. */}
                <svg
                  className="w-5 h-5"
                  viewBox="0 0 21 21"
                  xmlns="http://www.w3.org/2000/svg"
                  aria-hidden="true"
                >
                  <rect x="1" y="1" width="9" height="9" fill="#f25022" />
                  <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
                  <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
                  <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
                </svg>
                <span>{t('auth.signInWithSso')}</span>
              </a>
            </>
          )}

          {/* Registration Link — community edition only. Pro tenants
              provision users via LDAP / Microsoft Graph; surfacing
              self-register on the login page contradicts the security
              model and creates user confusion. (Backend-side rejection
              of /api/auth/register for pro is a separate hardening
              follow-up.) */}
          {allowRegistration && !isPro && (
            <div className="mt-6 pt-6 border-t border-gray-700 text-center">
              <p className="text-gray-400">
                {t('auth.dontHaveAccount')}{' '}
                <Link
                  to="/register"
                  className="text-primary-500 hover:text-primary-400 font-medium"
                >
                  {t('auth.createOne')}
                </Link>
              </p>
            </div>
          )}
        </div>

        <p className="text-center text-gray-500 text-sm mt-8">
          {appName} · {t('auth.personalAssistant')}
        </p>
      </div>
    </div>
  );
}
