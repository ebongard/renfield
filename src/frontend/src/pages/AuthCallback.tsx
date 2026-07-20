import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router';
import { Loader, XCircle } from 'lucide-react';

import apiClient from '../utils/axios';
import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY } from '../utils/authTokens';
import { readPkce, clearPkce } from '../utils/pkce';
import { useAuth } from '../context/AuthContext';

/**
 * SSO callback — the token-in-URL replacement (docs/design/sso-token-handoff-hardening.md).
 *
 * A federated login redirects here with `?code=&state=` (an opaque, single-use
 * code — NOT a token). We verify `state` against the value stashed when the
 * login started, then POST the code + our PKCE `code_verifier` to
 * `/api/auth/sso/exchange` and receive the tokens in the response body. A token
 * never rides in the URL, and a code leaked via history is worthless without the
 * verifier this tab holds.
 */
export default function AuthCallback() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { fetchUser } = useAuth();
  const [failed, setFailed] = useState(false);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return; // exchange is single-use — never run it twice (StrictMode)
    ran.current = true;

    const fail = () => {
      clearPkce();
      setFailed(true);
      navigate('/login?error=sso', { replace: true });
    };

    const code = params.get('code');
    const state = params.get('state');
    // Only ever navigate to a same-site absolute path. Reject protocol-relative
    // (`//evil.com`), backslash-tricks (`/\evil.com`) and any `scheme:` target so
    // a hostile `from` in the callback URL can't become an open redirect —
    // defense in depth (the exchange already gates on state, and react-router
    // would throw on a cross-origin push, but neither should be load-bearing).
    const rawFrom = params.get('from') || '/';
    const from =
      rawFrom.startsWith('/') && !rawFrom.startsWith('//') && !rawFrom.startsWith('/\\')
        ? rawFrom
        : '/';
    const { verifier, state: storedState } = readPkce();

    // Reject anything we didn't initiate: missing pieces, or a state that does
    // not match the one we stashed (CSRF / injected callback).
    if (!code || !state || !verifier || !storedState || state !== storedState) {
      fail();
      return;
    }

    (async () => {
      try {
        const resp = await apiClient.post('/api/auth/sso/exchange', {
          code,
          code_verifier: verifier,
          state,
        });
        const { access_token, refresh_token } = resp.data as {
          access_token?: string;
          refresh_token?: string;
        };
        if (!access_token) {
          fail();
          return;
        }
        localStorage.setItem(ACCESS_TOKEN_KEY, access_token);
        if (refresh_token) localStorage.setItem(REFRESH_TOKEN_KEY, refresh_token);
        clearPkce();
        await fetchUser();
        navigate(from, { replace: true });
      } catch {
        fail();
      }
    })();
    // params/navigate/fetchUser are stable for this one-shot effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
      <div className="text-center">
        {failed ? (
          <>
            <XCircle className="w-10 h-10 mx-auto text-red-500 mb-3" />
            <p className="text-gray-700 dark:text-gray-300">{t('auth.signInFailed')}</p>
          </>
        ) : (
          <>
            <Loader className="w-10 h-10 mx-auto animate-spin text-primary-600 dark:text-primary-400 mb-3" />
            <p className="text-gray-700 dark:text-gray-300">{t('auth.completingSignIn')}</p>
          </>
        )}
      </div>
    </div>
  );
}
