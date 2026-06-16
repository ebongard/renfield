import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import '@fontsource-variable/cormorant';
import '@fontsource-variable/dm-sans';
import App from './App';
import './index.css';
import './i18n';

// Build stamp — also the PWA service-worker propagation lever.
// The SW (vite-plugin-pwa, registerType:autoUpdate) PRECACHES index.html via
// globPatterns, storing its response INCLUDING the CSP header captured at fetch
// time. A change that touches only a non-bundled asset (e.g. the nginx CSP
// header) leaves every built file byte-identical → the precache manifest and
// sw.js don't change → autoUpdate never fires → existing clients keep serving
// the stale-CSP shell. Bumping this string changes the JS bundle hash → rewrites
// index.html's <script src> → index.html's revision changes → the SW re-precaches
// it (re-fetching the current CSP) and propagates. So: when you change a served
// HTTP header (CSP etc.) without any other frontend change, BUMP THIS so the fix
// reaches existing PWA clients. See reference_pwa_sw_nocache_nginx.
const __BUILD_STAMP__ = '2026-06-16.csp-blob-worklet';
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
