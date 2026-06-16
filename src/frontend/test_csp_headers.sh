#!/usr/bin/env bash
#
# test_csp_headers.sh — verifies the baseline Content-Security-Policy in
# nginx.conf is present on every document/worker/SW response (design §6 CSP
# test), and that it survives the nginx `add_header` inheritance trap on the
# locations that re-declare their own headers (`/`, `= /index.html`, the
# SPA fallback, `sw.js`/`registerSW.js`, `manifest.webmanifest`).
#
# There is no nginx test harness in this repo; this mirrors how the PWA
# no-cache headers were verified (a throwaway nginx:1.28-alpine container +
# header inspection — see memory reference_pwa_sw_nocache_nginx). It uses ONLY
# docker + the container's busybox wget, so it needs no host curl/node.
#
# Usage:  ./test_csp_headers.sh
# Exit:   0 = all assertions pass, non-zero = a header was missing/wrong.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HERE/nginx.conf"
IMG="nginx:1.28-alpine"
HEADER="Content-Security-Policy-Report-Only"   # v1 ships Report-Only; flip to
                                               # "Content-Security-Policy" here
                                               # when the policy is enforced.

# 0. nginx syntax must be valid.
echo "[csp-test] nginx -t ..."
docker run --rm -v "$CONF:/etc/nginx/conf.d/default.conf:ro" "$IMG" nginx -t

# 1. Minimal docroot so try_files / index resolve.
TMP="$(mktemp -d)"
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT
printf '<!doctype html><title>t</title>' > "$TMP/index.html"
printf '{}'        > "$TMP/manifest.webmanifest"
printf '/*sw*/'    > "$TMP/sw.js"
printf '/*reg*/'   > "$TMP/registerSW.js"
printf 'body{}'    > "$TMP/app-abc123.css"

NET="csptest_$$"
docker network create "$NET" >/dev/null
CID="$(docker run -d --rm --network "$NET" --name "cspnginx_$$" \
  -v "$CONF:/etc/nginx/conf.d/default.conf:ro" \
  -v "$TMP:/usr/share/nginx/html:ro" "$IMG")"
sleep 2

fail=0
hdrs() {  # fetch response headers for a path via a sibling busybox container
  docker run --rm --network "$NET" "$IMG" \
    sh -c "wget -S -q -O /dev/null http://cspnginx_$$:80$1 2>&1"
}

# Paths that MUST carry the CSP (document + worker contexts).
for path in "/" "/index.html" "/deep/spa/route" "/sw.js" "/registerSW.js" "/manifest.webmanifest"; do
  if hdrs "$path" | grep -qi "^[[:space:]]*${HEADER}:"; then
    echo "[csp-test] OK   $path  -> $HEADER present"
  else
    echo "[csp-test] FAIL $path  -> $HEADER MISSING"
    fail=1
  fi
done

# Spot-check a required directive made it through (catches an empty/truncated map).
if hdrs "/" | grep -qi "wasm-unsafe-eval"; then
  echo "[csp-test] OK   /  -> wasm-unsafe-eval present (wakeword WASM)"
else
  echo "[csp-test] FAIL /  -> wasm-unsafe-eval MISSING (wakeword would break when enforced)"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "[csp-test] FAILED"
  exit 1
fi
echo "[csp-test] all CSP header assertions passed"
