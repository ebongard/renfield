import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import '@fontsource-variable/cormorant';
import '@fontsource-variable/dm-sans';
import App from './App';
import { consumeSsoFragmentHandoff } from './utils/ssoFragment';
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

// OIDC URL-fragment hand-off (LEGACY — security audit). See utils/ssoFragment.ts:
// the old implicit flow redirects to /#access_token=<JWT>; we consume it into
// localStorage before React mounts (else AuthContext's mount-time fetchUser()
// would miss it and briefly flash the login page). It is a token-injection sink,
// gated behind VITE_SSO_LEGACY_FRAGMENT (kill switch, default ON so a still-
// migrating emitter isn't broken), accepts only a valid unexpired access JWT,
// and always strips the fragment. The hardened replacement is the ?code=+PKCE
// exchange (pages/AuthCallback.tsx / SSO_HANDOFF_ENABLED); flip the flag off and
// delete this once the emitter is fully migrated to ?code=.
consumeSsoFragmentHandoff(
  (import.meta.env.VITE_SSO_LEGACY_FRAGMENT ?? 'true') !== 'false',
);

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
