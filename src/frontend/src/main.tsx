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

// OIDC URL-fragment hand-off. After a successful OIDC dance the backend
// redirects to /#access_token=<JWT>&expires_in=<seconds>&provider=entra.
// We move those tokens into localStorage (the standard storage the rest
// of the app reads from) and clear the fragment before React mounts —
// otherwise AuthContext's mount-time fetchUser() would miss the token
// and the user would briefly see the login page before the fetch retried.
// Fragment is never sent to the server, so the JWT does NOT show up in
// any HTTP request log even though it lands in the URL bar momentarily.
function _consumeOidcHashHandoff(): void {
  const hash = window.location.hash;
  if (!hash || !hash.startsWith('#access_token=')) {
    return;
  }
  const params = new URLSearchParams(hash.slice(1));
  const accessToken = params.get('access_token');
  if (!accessToken) return;

  localStorage.setItem('renfield_access_token', accessToken);
  // Clear the fragment from the URL bar without triggering a navigation.
  // Replacing with `window.location.pathname + window.location.search` keeps
  // any path/query the backend included (e.g. ?from=/brain).
  history.replaceState(
    null,
    '',
    window.location.pathname + window.location.search,
  );
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
