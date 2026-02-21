/**
 * Registration Page
 *
 * Allows new users to create an account.
 */
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, Link } from 'react-router';
import { useAuth } from '../context/AuthContext';
import { UserPlus, Loader, AlertCircle, CheckCircle, Eye, EyeOff } from 'lucide-react';

export default function RegisterPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { register, isAuthenticated, authEnabled, allowRegistration, loading: authLoading } = useAuth();

  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);

  // Redirect if already authenticated or registration is disabled
  useEffect(() => {
    if (!authLoading) {
      if (isAuthenticated || !authEnabled) {
        navigate('/', { replace: true });
      } else if (!allowRegistration) {
        navigate('/login', { replace: true });
      }
    }
  }, [isAuthenticated, authEnabled, allowRegistration, authLoading, navigate]);

  // Clear error after 5 seconds
  useEffect(() => {
    if (error) {
      const timer = setTimeout(() => setError(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [error]);

  const handleSubmit = async (e) => {
    e.preventDefault();

    // Validation
    if (!username || !password) {
      setError(t('auth.fillAllRequiredFields'));
      return;
    }

    if (username.length < 3) {
      setError(t('auth.usernameTooShort'));
      return;
    }

    if (password.length < 8) {
      setError(t('auth.passwordTooShort'));
      return;
    }

    if (password !== confirmPassword) {
      setError(t('auth.passwordsDoNotMatch'));
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await register(username, password, email || null);
      setSuccess(true);
      // Redirect to login after 2 seconds
      setTimeout(() => {
        navigate('/login', { replace: true });
      }, 2000);
    } catch (err) {
      setError(err.response?.data?.detail || t('auth.registrationFailed'));
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

  return (
    <div className="min-h-screen bg-[radial-gradient(ellipse_at_center,_rgba(0,255,208,0.08)_0%,_#0f1117_70%)] flex items-center justify-center px-4 relative">
      {/* Noise overlay */}
      <div
        className="absolute inset-0 opacity-[0.03] pointer-events-none"
        style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='1'/%3E%3C/svg%3E")`,
        }}
      />

      <div className="max-w-md w-full relative z-10">
        {/* Logo/Title */}
        <div className="text-center mb-8">
          <img src="/logo-icon.svg" alt="" className="w-20 h-20 mx-auto mb-4" aria-hidden="true" />
          <h1 className="text-4xl font-bold font-display text-cream">Renfield</h1>
          <p className="text-gray-400 mt-2">{t('auth.createYourAccount')}</p>
        </div>

        {/* Registration Card */}
        <div className="card-primary bg-gray-900 border-gray-700">
          {/* Success Message */}
          {success && (
            <div className="bg-green-900/20 border border-green-700 rounded-lg p-4 mb-6">
              <div className="flex items-center space-x-3">
                <CheckCircle className="w-5 h-5 text-green-500 shrink-0" />
                <div>
                  <p className="text-green-400 font-medium">{t('auth.accountCreatedSuccess')}</p>
                  <p className="text-green-400/70 text-sm">{t('auth.redirectingToLogin')}</p>
                </div>
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

          <form onSubmit={handleSubmit} className="space-y-5">
            {/* Username */}
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.username')} <span className="text-red-500">*</span>
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('auth.chooseUsername')}
                className="input w-full bg-gray-800 border-gray-600 text-gray-100 placeholder-gray-500"
                autoComplete="username"
                autoFocus
                disabled={loading || success}
                minLength={3}
              />
              <p className="text-gray-500 text-xs mt-1">{t('auth.atLeast3Chars')}</p>
            </div>

            {/* Email (Optional) */}
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.email')} <span className="text-gray-500">({t('auth.optional')})</span>
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder={t('auth.emailPlaceholder')}
                className="input w-full bg-gray-800 border-gray-600 text-gray-100 placeholder-gray-500"
                autoComplete="email"
                disabled={loading || success}
              />
            </div>

            {/* Password */}
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.password')} <span className="text-red-500">*</span>
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={t('auth.createPassword')}
                  className="input w-full pr-10 bg-gray-800 border-gray-600 text-gray-100 placeholder-gray-500"
                  autoComplete="new-password"
                  disabled={loading || success}
                  minLength={8}
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
              <p className="text-gray-500 text-xs mt-1">{t('auth.atLeast8Chars')}</p>
            </div>

            {/* Confirm Password */}
            <div>
              <label htmlFor="confirmPassword" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.confirmPassword')} <span className="text-red-500">*</span>
              </label>
              <input
                id="confirmPassword"
                type={showPassword ? 'text' : 'password'}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder={t('auth.confirmYourPassword')}
                className="input w-full bg-gray-800 border-gray-600 text-gray-100 placeholder-gray-500"
                autoComplete="new-password"
                disabled={loading || success}
              />
            </div>

            {/* Submit Button */}
            <button
              type="submit"
              disabled={loading || success}
              className="w-full btn btn-primary py-3 flex items-center justify-center space-x-2"
            >
              {loading ? (
                <Loader className="w-5 h-5 animate-spin" />
              ) : (
                <>
                  <UserPlus className="w-5 h-5" />
                  <span>{t('auth.createAccount')}</span>
                </>
              )}
            </button>
          </form>

          {/* Login Link */}
          <div className="mt-6 pt-6 border-t border-gray-700 text-center">
            <p className="text-gray-400">
              {t('auth.alreadyHaveAccount')}{' '}
              <Link
                to="/login"
                className="text-primary-500 hover:text-primary-400 font-medium"
              >
                {t('auth.signIn')}
              </Link>
            </p>
          </div>
        </div>

        {/* Footer */}
        <p className="text-center text-gray-500 text-sm mt-8">
          Renfield - {t('auth.personalAssistant')}
        </p>
      </div>
    </div>
  );
}
