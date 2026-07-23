#!/usr/bin/env bash
# Release the voice-server image with guardrails (extraction plan T4 / D7).
#
# The v0.1.5-vs-v0.2.0 drift happened because releases were hand-rolled:
# mutable tags, no changelog, __version__ stuck at 0.1.0 while images said
# v0.2.0, /health lying about what runs. This script makes a release refuse
# to happen unless every identity lines up:
#
#   git tree clean  ==  git tag  ==  __version__  ==  image tag  ==  /health
#
# and records the pushed digest so consuming manifests can pin immutably.
#
# Usage:
#   bin/release-voice-server.sh v0.3.0            # release
#   bin/release-voice-server.sh v0.3.0 --dry-run  # print, don't build
#
# Env overrides: RENFIELD_BUILD_HOST (default evdb@192.168.1.159),
#                RENFIELD_REGISTRY   (required, e.g. your-registry.example/renfield),
#                HF_TOKEN_FILE       (default ~/.hf_token on the build host; used
#                                     as the BuildKit secret to bake pyannote)
#
# What it does, in order (all guardrails BEFORE any build):
#   1. refuse dirty tree / detached HEAD
#   2. refuse tag not matching vX.Y.Z
#   3. refuse __version__ mismatch (voice_server/__init__.py)
#   4. refuse missing CHANGELOG section for this version
#   5. refuse existing git tag OR existing registry tag (tags are immutable)
#   6. rsync voice-server/ to the build host, docker build (BuildKit,
#      hf_token secret), push ONLY the semver tag (no :latest)
#   7. smoke the built image: import app, opus decode available,
#      /health-reported version == tag
#   8. export the OpenAPI contract to voice-server/contracts/openapi-<tag>.json
#   9. record tag/date/git-sha/digest in voice-server/RELEASES.md
#  10. create the local git tag voice-server-<tag> (push it yourself)
#
# The digest line in RELEASES.md is what consuming manifests pin:
#   image: <registry>/voice-server@sha256:...
set -euo pipefail

BUILD_HOST="${RENFIELD_BUILD_HOST:-evdb@192.168.1.159}"
REGISTRY="${RENFIELD_REGISTRY:?set RENFIELD_REGISTRY, e.g. export RENFIELD_REGISTRY=your-registry.example/renfield}"
IMAGE="$REGISTRY/voice-server"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VSDIR="$REPO_ROOT/voice-server"

TAG="${1:-}"
DRY_RUN=0
[[ "${2:-}" == "--dry-run" ]] && DRY_RUN=1
[[ -z "$TAG" ]] && { sed -n '2,20p' "$0"; exit 2; }

log()  { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
fail() { printf '\033[1;31mREFUSED: %s\033[0m\n' "$*" >&2; exit 1; }
run()  { if [[ $DRY_RUN == 1 ]]; then printf '  [dry-run] %s\n' "$*"; else eval "$@"; fi; }
on_build() {
  if [[ $DRY_RUN == 1 ]]; then printf '  [dry-run][%s] %s\n' "$BUILD_HOST" "$1"
  else ssh "$BUILD_HOST" "$1"; fi
}

# --- guardrails (no build until ALL pass) -----------------------------------
log "guardrails for $TAG"

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "tag must be vX.Y.Z, got: $TAG"
VERSION="${TAG#v}"

cd "$REPO_ROOT"
[[ -z "$(git status --porcelain)" ]] || fail "working tree is dirty — commit or stash first"
git symbolic-ref -q HEAD >/dev/null || fail "detached HEAD — release from a branch"
GIT_SHA=$(git rev-parse --short HEAD)

CODE_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$VSDIR/voice_server/__init__.py")
[[ "$CODE_VERSION" == "$VERSION" ]] || fail \
  "__version__ is '$CODE_VERSION' but releasing '$VERSION' — /health would lie. Fix voice_server/__init__.py first."

grep -q "^## \[$VERSION\]" "$VSDIR/CHANGELOG.md" 2>/dev/null || fail \
  "voice-server/CHANGELOG.md has no '## [$VERSION]' section — write the changelog first"

GIT_TAG="voice-server-$TAG"
git rev-parse -q --verify "refs/tags/$GIT_TAG" >/dev/null && fail "git tag $GIT_TAG already exists — tags are immutable, bump the version"

# Registry immutability: refuse if the tag already exists remotely.
if [[ $DRY_RUN == 0 ]]; then
  if on_build "docker manifest inspect $IMAGE:$TAG >/dev/null 2>&1"; then
    fail "$IMAGE:$TAG already exists in the registry — tags are immutable, bump the version"
  fi
fi

log "guardrails passed (git $GIT_SHA, version $VERSION)"

# --- build + push on the build host -----------------------------------------
STAGING="/tmp/voice-server-release-$TAG"
log "rsync source to $BUILD_HOST:$STAGING"
run "rsync -az --delete --exclude='__pycache__' --exclude='.pytest_cache' \
  '$VSDIR/' '$BUILD_HOST:$STAGING/'"

log "build + push $IMAGE:$TAG"
on_build "set -e; cd $STAGING && \
  SECRET_ARG=''; [ -f \${HF_TOKEN_FILE:-\$HOME/.hf_token} ] && SECRET_ARG=\"--secret id=hf_token,src=\${HF_TOKEN_FILE:-\$HOME/.hf_token}\"; \
  DOCKER_BUILDKIT=1 docker build \$SECRET_ARG -t $IMAGE:$TAG -f Dockerfile . && \
  docker push $IMAGE:$TAG"

# --- post-build smoke (the built artifact, not the source) ------------------
log "smoke: import app + opus + version == $VERSION"
on_build "docker run --rm -e AUTH_REQUIRED=false $IMAGE:$TAG python - <<'PY'
from voice_server import __version__
from voice_server.services.opus_decode import OPUSLIB_AVAILABLE
from voice_server.main import app  # import-smokes the whole ASGI surface
assert __version__ == \"$VERSION\", f\"image reports {__version__}, expected $VERSION\"
assert OPUSLIB_AVAILABLE, \"opuslib/libopus missing — satellites would 503 (silent skew)\"
print(\"smoke ok:\", __version__)
PY"

# --- contract export --------------------------------------------------------
log "export OpenAPI contract"
run "mkdir -p '$VSDIR/contracts'"
if [[ $DRY_RUN == 0 ]]; then
  on_build "docker run --rm -e AUTH_REQUIRED=false $IMAGE:$TAG python -c \
    'import json; from voice_server.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))'" \
    > "$VSDIR/contracts/openapi-$TAG.json"
fi

# --- record digest ----------------------------------------------------------
log "record digest"
if [[ $DRY_RUN == 0 ]]; then
  DIGEST=$(on_build "docker inspect --format='{{index .RepoDigests 0}}' $IMAGE:$TAG")
  [[ "$DIGEST" == *"@sha256:"* ]] || fail "could not read pushed digest for $IMAGE:$TAG"
  printf '| %s | %s | %s | `%s` |\n' "$TAG" "$(date -u +%Y-%m-%d)" "$GIT_SHA" "$DIGEST" >> "$VSDIR/RELEASES.md"
  git tag "$GIT_TAG"
  log "released $IMAGE:$TAG"
  echo "digest (pin THIS in consuming manifests):"
  echo "  image: $DIGEST"
  echo
  echo "next steps:"
  echo "  - commit contracts/openapi-$TAG.json + RELEASES.md"
  echo "  - git push origin $GIT_TAG"
  echo "  - update consumers to the digest (renfield k8s/voice-server.yaml,"
  echo "    private_k8s voice-server manifests) — imagePullPolicy: IfNotPresent"
else
  echo "  [dry-run] would push, record digest in RELEASES.md, git tag $GIT_TAG"
fi
