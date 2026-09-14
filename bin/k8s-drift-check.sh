#!/usr/bin/env bash
#
# k8s-drift-check.sh — READ-ONLY drift check: `kubectl diff` every workload
# manifest (Deployment / StatefulSet / CronJob / DaemonSet) in a directory
# against the live cluster, and report which ones an `apply` would change.
#
# Why: the manifests are the source of truth, but images roll via `set image`
# and fixes sometimes land via `kubectl patch`. Once a manifest falls behind the
# live object, the next routine `kubectl apply -f` silently REGRESSES the
# instance (old image, dropped args/env/secretRefs, old DB host). Reconciled
# 2026-09-14; this script keeps it that way.
#
# It never mutates anything: the only cluster calls are `kubectl get` and
# `kubectl diff` (server-side dry run).
#
# Placeholder rendering (public renfield manifests):
#   `your-registry.example/renfield` → $RENFIELD_REGISTRY (or, if unset, the
#   registry of the live backend image), and the `backend:latest` /
#   `frontend:latest` placeholders → the tag the live `backend` / `frontend`
#   Deployment runs right now. Images are owned by `set image`, so the image tag
#   is not drift; every OTHER pinned tag (dlna-mcp, samsung-mcp, ...) is compared
#   as written. Manifests without the placeholder (x-ren) are diffed verbatim —
#   there a stale image tag IS reported, which is the reminder to commit the bump.
#
# Blind spot (by design of `kubectl diff`): a field that exists ONLY live — added
# by `kubectl patch` / `set env` and never written into the manifest — survives an
# apply and therefore does not show up here. Drift in that direction is not
# regressed by an apply, but it is still missing from git.
#
# Usage:
#   bin/k8s-drift-check.sh                                   # household: k8s/ vs ns renfield
#   bin/k8s-drift-check.sh --show-diff k8s/backend.yaml      # print the (filtered) diff
#   bin/k8s-drift-check.sh -n renfield-xidra --dir ../x-ren/k8s
#   bin/k8s-drift-check.sh --render k8s/backend.yaml         # print the rendered manifest
#   bin/k8s-drift-check.sh --dry-run                         # list files + commands, no cluster
#
# The summary mode prints no object content. --show-diff prints the diff minus
# the last-applied annotation; Secret data is masked by kubectl itself, but a
# ConfigMap living in the same file would be shown — mind that when sharing output.
#
# Exit: 0 = no drift, 1 = drift in at least one manifest, 2 = usage/cluster error.
set -euo pipefail

KCTX="${RENFIELD_KCTX:-renfield-private}"
NS="${RENFIELD_NS:-renfield}"
REGISTRY="${RENFIELD_REGISTRY:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$REPO_ROOT/k8s"
SHOW_DIFF=0 DRY_RUN=0 RENDER=""
FILES=()
PLACEHOLDER="your-registry.example/renfield"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)       KCTX="$2"; shift 2 ;;
    -n|--namespace)  NS="$2"; shift 2 ;;
    --dir)           DIR="$2"; shift 2 ;;
    --registry)      REGISTRY="$2"; shift 2 ;;
    --show-diff)     SHOW_DIFF=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --render)        RENDER="$2"; shift 2 ;;
    -h|--help)       sed -n '2,45p' "$0"; exit 0 ;;
    -*)              echo "unknown arg: $1" >&2; exit 2 ;;
    *)               FILES+=("$1"); shift ;;
  esac
done

KUBECTL=(kubectl --context "$KCTX" -n "$NS")

is_workload() { grep -qE '^kind: (Deployment|StatefulSet|CronJob|DaemonSet)[[:space:]]*$' "$1"; }

# Tag of a container's image in a live Deployment ("" when absent).
live_tag() {
  local img
  img=$("${KUBECTL[@]}" get "deploy/$1" -o "jsonpath={.spec.template.spec.containers[?(@.name==\"$2\")].image}" 2>/dev/null) || true
  [[ "$img" == *:* ]] && printf '%s' "${img##*:}"
}

BACKEND_TAG="" FRONTEND_TAG="" RESOLVED=0
resolve_placeholders() {
  [[ $RESOLVED == 1 ]] && return 0
  RESOLVED=1
  if [[ $DRY_RUN == 1 ]]; then
    REGISTRY="${REGISTRY:-<registry>}"; BACKEND_TAG="<live-backend-tag>"; FRONTEND_TAG="<live-frontend-tag>"
    return 0
  fi
  if [[ -z "$REGISTRY" ]]; then
    local img
    img=$("${KUBECTL[@]}" get deploy/backend -o 'jsonpath={.spec.template.spec.containers[?(@.name=="backend")].image}' 2>/dev/null) || true
    [[ "$img" == */backend:* ]] || { echo "ERROR: set RENFIELD_REGISTRY (no live deploy/backend to derive it from)" >&2; exit 2; }
    REGISTRY="${img%/backend:*}"
  fi
  BACKEND_TAG="$(live_tag backend backend)"
  FRONTEND_TAG="$(live_tag frontend frontend)"
}

# Print a manifest with the placeholders substituted (stdout).
render() {
  local f="$1"
  if grep -q "$PLACEHOLDER" "$f"; then
    resolve_placeholders
    local args=(-e "s#$PLACEHOLDER#$REGISTRY#g")
    [[ -n "$BACKEND_TAG" ]] && args+=(-e "s#$REGISTRY/backend:latest#$REGISTRY/backend:$BACKEND_TAG#g")
    [[ -n "$FRONTEND_TAG" ]] && args+=(-e "s#$REGISTRY/frontend:latest#$REGISTRY/frontend:$FRONTEND_TAG#g")
    sed "${args[@]}" "$f"
  else
    cat "$f"
  fi
}

if [[ -n "$RENDER" ]]; then
  [[ -f "$RENDER" ]] || { echo "ERROR: no such file: $RENDER" >&2; exit 2; }
  render "$RENDER"
  exit 0
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  [[ -d "$DIR" ]] || { echo "ERROR: manifest dir not found: $DIR" >&2; exit 2; }
  shopt -s nullglob
  for f in "$DIR"/*.yaml "$DIR"/*/*.yaml; do is_workload "$f" && FILES+=("$f"); done
fi
[[ ${#FILES[@]} -gt 0 ]] || { echo "ERROR: no workload manifests found" >&2; exit 2; }

echo "drift check: context=$KCTX namespace=$NS (${#FILES[@]} manifest(s), read-only)"
DRIFT=0 ERRORS=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || { echo "  ERROR  $f (missing)"; ERRORS=1; continue; }
  if [[ $DRY_RUN == 1 ]]; then
    grep -q "$PLACEHOLDER" "$f" && resolve_placeholders
    printf '  [dry-run] render %s | %s diff -f -\n' "$f" "${KUBECTL[*]}"
    continue
  fi
  out="$TMP/diff"; rc=0
  render "$f" | "${KUBECTL[@]}" diff -f - >"$out" 2>&1 || rc=$?
  case $rc in
    0) printf '  ok     %s\n' "$f" ;;
    1)
      # Count real change lines: skip the file headers, the last-applied
      # annotation (it always differs once anything differs) and generation.
      n=$(grep -E '^[+-]' "$out" | grep -vE '^(\+\+\+|---) |^[+-] *(generation:|\{"apiVersion")' | wc -l | tr -d ' ')
      printf '  DRIFT  %s (%s changed line(s))\n' "$f" "$n"
      DRIFT=1
      if [[ $SHOW_DIFF == 1 ]]; then
        grep -vE '^diff -u|^(\+\+\+|---) |last-applied-configuration|^[ +-] *\{"apiVersion"|^[ +-] *generation:' "$out" | sed 's/^/         /'
      fi
      ;;
    *) printf '  ERROR  %s (kubectl diff exit %s)\n' "$f" "$rc"; sed 's/^/         /' "$out" | head -5; ERRORS=1 ;;
  esac
done

[[ $DRY_RUN == 1 ]] && exit 0
if [[ $ERRORS == 1 ]]; then exit 2; fi
if [[ $DRIFT == 1 ]]; then
  echo "drift found — reconcile the manifest (or record why the repo is intentionally ahead) before any apply."
  exit 1
fi
echo "no drift."
