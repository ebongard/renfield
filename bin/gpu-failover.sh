#!/usr/bin/env bash
#
# Cold failover of the production GPU tier to Host D (docs/GPU_FAILOVER.md, #1636).
#
#   bin/gpu-failover.sh status
#   bin/gpu-failover.sh to-hostd <llm|ollama|voice|all> [--instance household|xidra|both] [--dry-run]
#   bin/gpu-failover.sh to-prod  <llm|ollama|voice|all> [--instance household|xidra|both] [--dry-run] [--force]
#
# Parts and the renfield-env keys they move (nothing else is touched):
#   llm     LLM_OPENAI_BASE_URL, OLLAMA_VISION_URL          cuda.local:8081   -> Host D :8081 (V100)
#   ollama  OLLAMA_URL, OLLAMA_EMBED_URL, AGENT_ROUTER_URL,
#           LLM_OPENAI_EMBED_BASE_URL                       cluster Ollama    -> Host D :11434 (4060 Ti)
#   voice   VOICE_SERVER_URL                                voice-server-anon -> Host D :8091 (4060 Ti)
#
# to-hostd: probes the Host D endpoints FROM INSIDE a backend pod of the instance (the path the
#   backend will take) and aborts if one is not healthy; then patches the keys in the live
#   ConfigMap, marks it (annotation renfield.io/gpu-failover) and restarts backend + workers.
# to-prod: puts the keys back to the values IN GIT (k8s/configmap.yaml, x-ren/k8s/configmap.yaml),
#   so afterwards the live ConfigMap equals git again. Probes the production endpoints first
#   and refuses while they are down (--force overrides).
#
# A key that is empty in git stays empty: voice is off on xidra and the failover does not
# switch it on. This is the one sanctioned live edit of renfield-env (.claude/rules/deploy.md:
# git stays the source of truth — the failover is a temporary deviation and to-prod ends it).
#
# Env: HOSTD_GPU_HOST (default 192.168.1.137 = LXC 100 gpu-ct), KUBE_CONTEXT (renfield-private),
#      X_REN_DIR (default ../x-ren next to this repo).
set -euo pipefail

HOSTD="${HOSTD_GPU_HOST:-192.168.1.137}"
CTX="${KUBE_CONTEXT:-renfield-private}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
X_REN_DIR="${X_REN_DIR:-$REPO/../x-ren}"
ANNOTATION="renfield.io/gpu-failover"
WORKLOADS=(backend document-worker pdf-split-worker meeting-worker)

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

ACTION="${1:-}"; [[ -n "$ACTION" ]] || usage; shift
PART=""; INSTANCE="both"; DRY_RUN=0; FORCE=0
if [[ "$ACTION" != status ]]; then PART="${1:-}"; [[ -n "$PART" ]] || usage; shift; fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance) [[ $# -ge 2 ]] || usage; INSTANCE="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --force)    FORCE=1; shift ;;
    -h|--help)  usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done
case "$ACTION" in status|to-hostd|to-prod) ;; *) usage ;; esac
case "$PART" in ""|llm|ollama|voice|all) ;; *) echo "unknown part: $PART" >&2; usage ;; esac
case "$INSTANCE" in household|xidra|both) ;; *) echo "unknown instance: $INSTANCE" >&2; usage ;; esac
[[ "$ACTION" == status ]] && INSTANCE=both

kc() { kubectl --context "$CTX" "$@"; }

keys_of() {
  case "$1" in
    llm)    echo LLM_OPENAI_BASE_URL OLLAMA_VISION_URL ;;
    ollama) echo OLLAMA_URL OLLAMA_EMBED_URL AGENT_ROUTER_URL LLM_OPENAI_EMBED_BASE_URL ;;
    voice)  echo VOICE_SERVER_URL ;;
    all)    echo "$(keys_of llm) $(keys_of ollama) $(keys_of voice)" ;;
  esac
}

hostd_value() {
  case "$1" in
    LLM_OPENAI_BASE_URL|OLLAMA_VISION_URL)      echo "http://$HOSTD:8081/v1" ;;
    OLLAMA_URL|OLLAMA_EMBED_URL|AGENT_ROUTER_URL) echo "http://$HOSTD:11434" ;;
    LLM_OPENAI_EMBED_BASE_URL)                  echo "http://$HOSTD:11434/v1" ;;
    VOICE_SERVER_URL)                           echo "http://$HOSTD:8091" ;;
  esac
}

ns_of()  { [[ "$1" == household ]] && echo renfield || echo renfield-xidra; }
git_cm() { [[ "$1" == household ]] && echo "$REPO/k8s/configmap.yaml" || echo "$X_REN_DIR/k8s/configmap.yaml"; }
instances() { [[ "$INSTANCE" == both ]] && echo household xidra || echo "$INSTANCE"; }

# The value of a top-level data key in a committed ConfigMap file (2-space indent,
# optionally quoted, optional trailing comment). Fails loudly when the key is absent.
git_value() {
  local file="$1" key="$2" v
  [[ -r "$file" ]] || { echo "ERROR: $file not readable (X_REN_DIR?)" >&2; exit 1; }
  v=$(sed -n -E "s/^  ${key}: *\"([^\"]*)\".*$/\1/p; s/^  ${key}: *([^\" #][^ #]*) *(#.*)?$/\1/p; s/^  ${key}: *(#.*)?$//p" "$file" | head -1)
  grep -qE "^  ${key}:" "$file" || { echo "ERROR: $key not in $file" >&2; exit 1; }
  printf '%s' "$v"
}

live_value() { kc -n "$1" get configmap renfield-env -o "jsonpath={.data.$2}"; }

# Probe URLs from inside the instance's backend pod: the path the backend itself will take.
probe() {
  local ns="$1"; shift
  kc -n "$ns" exec -i deploy/backend -c backend -- python - "$@" <<'PY'
import json, sys, urllib.request
bad = 0
for url in sys.argv[1:]:
    try:
        body = urllib.request.urlopen(url, timeout=8).read(300).decode("utf-8", "replace")
        # 200 (urlopen raises otherwise) AND a positive body: llama-server/voice-server
        # say "ok", Ollama names its version. A loading llama-server answers 503.
        ok = ('"ok"' in body or '"version"' in body) and '_ready":false' not in body
        print(("OK   " if ok else "BAD  ") + url + "  " + body[:80].replace("\n", " "))
        bad += 0 if ok else 1
    except Exception as e:
        print("FAIL " + url + "  " + str(e)[:80]); bad += 1
sys.exit(1 if bad else 0)
PY
}

health_urls() {  # <part> <side: hostd|prod> <instance>
  local part="$1" side="$2" inst="$3" file; file=$(git_cm "$inst")
  local urls=()
  if [[ "$part" == llm || "$part" == all ]]; then
    [[ $side == hostd ]] && urls+=("http://$HOSTD:8081/health") \
      || urls+=("$(git_value "$file" LLM_OPENAI_BASE_URL | sed 's#/v1$##')/health")
  fi
  if [[ "$part" == ollama || "$part" == all ]]; then
    [[ $side == hostd ]] && urls+=("http://$HOSTD:11434/api/version") \
      || urls+=("$(git_value "$file" OLLAMA_URL)/api/version")
  fi
  if [[ "$part" == voice || "$part" == all ]]; then
    local v; v=$(git_value "$file" VOICE_SERVER_URL)
    if [[ -n "$v" ]]; then
      [[ $side == hostd ]] && urls+=("http://$HOSTD:8091/health") || urls+=("$v/health")
    fi
  fi
  echo "${urls[@]:-}"
}

restart() {
  local ns="$1" w present=()
  for w in "${WORKLOADS[@]}"; do
    kc -n "$ns" get deploy "$w" >/dev/null 2>&1 && present+=("deploy/$w")
  done
  if (( DRY_RUN )); then echo "  [dry-run] rollout restart ${present[*]}"; return; fi
  kc -n "$ns" rollout restart "${present[@]}"
  for w in "${present[@]}"; do kc -n "$ns" rollout status "$w" --timeout=300s; done
}

switch() {  # <instance> <target: hostd|prod>
  local inst="$1" target="$2" ns file k v cur patch="" changed=0
  ns=$(ns_of "$inst"); file=$(git_cm "$inst")
  echo "== $inst ($ns): $PART -> $target"
  for k in $(keys_of "$PART"); do
    v=$(git_value "$file" "$k")
    if [[ -z "$v" ]]; then echo "  $k: empty in git (feature off) — left alone"; continue; fi
    [[ "$target" == hostd ]] && v=$(hostd_value "$k")
    cur=$(live_value "$ns" "$k")
    if [[ "$cur" == "$v" ]]; then echo "  $k: already $v"; continue; fi
    echo "  $k: $cur -> $v"
    patch+="${patch:+,}\"$k\":\"$v\""; changed=1
  done
  (( changed )) || { echo "  nothing to change"; return; }
  if (( DRY_RUN )); then echo "  [dry-run] patch configmap renfield-env"; restart "$ns"; return; fi
  kc -n "$ns" patch configmap renfield-env --type merge -p "{\"data\":{$patch}}" >/dev/null
  # The mark names what is still on Host D; it disappears once every key equals git.
  local left; left=$(drift "$inst" | cut -d= -f1 | tr '\n' ' ' | sed 's/ $//')
  if [[ -z "$left" ]]; then
    kc -n "$ns" annotate configmap renfield-env "$ANNOTATION-" >/dev/null 2>&1 || true
  else
    kc -n "$ns" annotate configmap renfield-env --overwrite "$ANNOTATION=on Host D since $(date -u +%FT%TZ): $left" >/dev/null
  fi
  restart "$ns"
}

drift() {  # the managed keys whose live value differs from git, one "KEY live" per line
  local inst="$1" ns file k
  ns=$(ns_of "$inst"); file=$(git_cm "$inst")
  for k in $(keys_of all); do
    [[ "$(live_value "$ns" "$k")" == "$(git_value "$file" "$k")" ]] || echo "$k=$(live_value "$ns" "$k")"
  done
}

if [[ "$ACTION" == status ]]; then
  for inst in $(instances); do
    ns=$(ns_of "$inst")
    echo "== $inst ($ns)  mark: $(kc -n "$ns" get configmap renfield-env -o "jsonpath={.metadata.annotations.renfield\.io/gpu-failover}" || true)"
    d=$(drift "$inst")
    if [[ -z "$d" ]]; then echo "  GPU keys = git (production tier)"; else echo "$d" | sed 's/^/  DIFFERS FROM GIT: /'; fi
    echo "  production endpoints:"; probe "$ns" $(health_urls all prod "$inst") | sed 's/^/    /' || true
    echo "  Host D endpoints:";     probe "$ns" $(health_urls all hostd "$inst") | sed 's/^/    /' || true
  done
  exit 0
fi

for inst in $(instances); do
  ns=$(ns_of "$inst")
  if [[ "$ACTION" == to-hostd ]]; then side=hostd; else side=prod; fi
  urls=$(health_urls "$PART" "$side" "$inst")
  if [[ -n "$urls" ]]; then
    echo "== $inst: probing $side endpoints from deploy/backend"
    if ! probe "$ns" $urls | sed 's/^/  /'; then
      if [[ "$ACTION" == to-prod && $FORCE == 1 ]]; then
        echo "  production endpoint not healthy — continuing because of --force"
      else
        echo "ERROR: $side endpoint not healthy — nothing changed$([[ $ACTION == to-prod ]] && echo ' (--force overrides)')" >&2
        exit 1
      fi
    fi
  fi
done
for inst in $(instances); do
  if [[ "$ACTION" == to-hostd ]]; then switch "$inst" hostd; else switch "$inst" prod; fi
done
echo "done. Check: bin/gpu-failover.sh status"
