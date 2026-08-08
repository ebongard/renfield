import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import '@fontsource-variable/cormorant';
import '@fontsource-variable/dm-sans';
import App from './App';
import './index.css';
import 'react-day-picker/style.css';
import './i18n';

// Build stamp — also the PWA service-worker propagation lever.
// Injected at build time from the deploy's frontend tag via
// `--build-arg VITE_BUILD_STAMP=<tag>` (Dockerfile → ENV → import.meta.env; see
// bin/deploy-production.sh). Every deploy uses a unique tag, so the stamp is
// always fresh — no more hand-editing a literal that silently rots.
//
// It's also the PWA propagation lever: the SW (vite-plugin-pwa,
// registerType:autoUpdate) PRECACHES index.html via globPatterns, storing its
// response INCLUDING the CSP header captured at fetch time. A change that touches
// only a non-bundled asset (e.g. the nginx CSP header) would leave every built
// file byte-identical → sw.js unchanged → autoUpdate never fires. But the nginx
// CSP lives in THIS frontend image, so a header change rebuilds the image under a
// new tag → the stamp changes → the JS bundle hash changes → index.html's
// revision changes → the SW re-precaches (re-fetching the current CSP) and
// propagates. So a header-only change reaches existing PWA clients automatically,
// as long as the deploy uses a fresh tag. See reference_pwa_sw_nocache_nginx.
const __BUILD_STAMP__ = (import.meta.env.VITE_BUILD_STAMP as string | undefined) ?? 'dev';
console.info(`Renfield frontend build ${__BUILD_STAMP__}`);

// Edition selector for theme tokens. When VITE_APP_EDITION=pro, the
// CSS attribute selector [data-edition="pro"] in index.css re-points
// the gray-* palette to slate-* for a cooler enterprise feel —
// covers every surface in the app via the existing utility classes
// (962 gray-* usages remap automatically). Default Renfield
// community: no attribute set, gray-* stays warm.
const _edition = import.meta.env.VITE_APP_EDITION;
if (_edition === 'pro') {
  document.documentElement.dataset.edition = 'pro';
}

// OIDC URL-fragment hand-off (LEGACY — security audit). The old OIDC implicit
// flow redirects to /#access_token=<JWT>&expires_in=<seconds>&provider=entra and
// we move that token into localStorage before React mounts (otherwise
// AuthContext's mount-time fetchUser() would miss it and briefly flash the login
// page). The hardened replacement is the ?code=+PKCE exchange
// (pages/AuthCallback.tsx / SSO_HANDOFF_ENABLED), which validates state + a PKCE
// verifier and never puts a token in the URL.
//
// This handler is a token-INJECTION sink: any attacker-crafted `#access_token=`
// is copied into localStorage. We cannot fully close that on the client (the
// browser can't verify the HS256 signature), so we (a) gate the whole handler
// behind a build flag — a kill switch for the post-cutover build; default ON so
// no still-migrating SSO emitter (Reva) breaks — (b) accept only a structurally
// valid, UNEXPIRED access JWT, and (c) ALWAYS strip the fragment from the URL,
// even when we reject the token, so a crafted value never lingers in
// history/Referer. Flip VITE_SSO_LEGACY_FRAGMENT=false and delete this once the
// emitter is fully migrated to ?code=.
const _SSO_LEGACY_FRAGMENT_ENABLED =
  (import.meta.env.VITE_SSO_LEGACY_FRAGMENT ?? 'true') !== 'false';

function _looksLikeUnexpiredAccessJwt(token: string): boolean {
  const parts = token.split('.');
  if (parts.length !== 3) return false;
  try {
    const payload = JSON.parse(
      atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')),
    ) as { type?: string; exp?: number };
    return (
      payload.type === 'access'
      && typeof payload.exp === 'number'
      && payload.exp * 1000 > Date.now()
    );
  } catch {
    return false;
  }
}

function _consumeOidcHashHandoff(): void {
  if (!_SSO_LEGACY_FRAGMENT_ENABLED) return;
  const hash = window.location.hash;
  if (!hash || !hash.startsWith('#access_token=')) {
    return;
  }
  const params = new URLSearchParams(hash.slice(1));
  const accessToken = params.get('access_token');
  // Strip the fragment from the URL bar without triggering a navigation —
  // unconditionally, even if we reject the token below, so a crafted
  // `#access_token=` never lingers in history/Referer. Keep any path/query the
  // backend included (e.g. ?from=/brain).
  const clearFragment = (): void => {
    history.replaceState(
      null,
      '',
      window.location.pathname + window.location.search,
    );
  };
  if (!accessToken || !_looksLikeUnexpiredAccessJwt(accessToken)) {
    clearFragment();
    return;
  }

  localStorage.setItem('renfield_access_token', accessToken);
  clearFragment();
}
_consumeOidcHashHandoff();

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found in document');
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
