#!/usr/bin/env bash
# Writes the freshly rolled-out image tags into the manifests -- does NOT commit.
#
# WHY THIS EXISTS
# ---------------
# `kubectl set image` changes the LIVE objects, not the manifests. So after
# every deploy the files drift again, and a later `kubectl apply -f` throws the
# instance back onto the old image. `bin/k8s-drift-check.sh` notices that and
# WARNS -- after which it was fixed by hand, anew after every deploy. Three
# times in a row the next commit in x-ren was called "image tags to the live
# state". Manual work one has to remember is not a solution.
#
# 🛑 THE TAG ONLY, NEVER THE REGISTRY NAME. The public manifests deliberately
# carry the placeholder `your-registry.example/...`; the real address does not
# belong in the public repo. The replacement therefore applies exclusively
# behind `renfield/<image>:` and leaves everything before it untouched -- no
# matter whether the placeholder or the real address stands there.
#
# 🛑 `:latest` STAYS `:latest`. It is a moving pointer, not a pinned state. The
# public manifests carry it deliberately: a freshly set-up cluster
# (`kubectl apply -k overlays/private/`) pulls the current image with it. Were
# this script to write the date tag in, such a cluster would forever get the
# image of the day somebody last deployed. Noticed on the FIRST real run: seven
# public manifests were rewritten from `latest` to the day's tag. I had checked
# only against x-ren (where concrete tags stand everywhere) -- never the case
# that occurs daily.
#
# 🛑 DOES NOT COMMIT AND DOES NOT PUSH. The script says what to commit; the
# decision stays with a person. That holds especially because a private
# instance's manifests live in a SECOND repo.
#
# Usage:
#   bin/k8s-write-image-tags.sh --dir ../x-ren/k8s --backend-tag 2026-09-28-x \
#                               --frontend-tag 2026-09-28-y [--dry-run]
set -euo pipefail

DIR="" BACKEND_TAG="" FRONTEND_TAG="" DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)           DIR="$2"; shift 2 ;;
    --backend-tag)   BACKEND_TAG="$2"; shift 2 ;;
    --frontend-tag)  FRONTEND_TAG="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -z "$DIR" ]] && { echo "ERROR: --dir required" >&2; exit 2; }
[[ -d "$DIR" ]] || { echo "ERROR: no such directory: $DIR" >&2; exit 2; }
[[ -z "$BACKEND_TAG$FRONTEND_TAG" ]] && { echo "  (nichts zu schreiben: weder --backend-tag noch --frontend-tag)"; exit 0; }

# A tag must not carry characters that take the replacement or the YAML apart.
for t in "$BACKEND_TAG" "$FRONTEND_TAG"; do
  [[ -z "$t" ]] && continue
  [[ "$t" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ERROR: unplausible tag: $t" >&2; exit 2; }
done

# Replaces the tag behind `renfield/<image>:` -- unless it reads `latest`.
# sed has no negative lookahead, so `latest` is taken out of reach beforehand:
# `@` is not part of the permitted tag character set and the expression requires
# a `:`, so `renfield/<image>@@LATEST@@` can no longer match. The boundary
# `([^A-Za-z0-9._-]|$)` makes sure only the tag `latest` is protected and not,
# say, `latest-rc`.
marke_setzen() {  # $1 = image name, $2 = new tag; reads stdin, writes stdout
  sed -E "s#(renfield/$1):latest([^A-Za-z0-9._-]|\$)#\1@@LATEST@@\2#g" \
    | sed -E "s#(renfield/$1:)[A-Za-z0-9._-]+#\1$2#g" \
    | sed -E "s#(renfield/$1)@@LATEST@@#\1:latest#g"
}

changed=() fehlgeschlagen=()
while IFS= read -r -d '' f; do
  before="$(cat "$f")"
  after="$before"
  # Only the part BEHIND `renfield/<image>:` is replaced. The prefix -- and
  # with it the registry name, real or placeholder -- stays untouched.
  if [[ -n "$BACKEND_TAG" ]]; then
    after="$(printf '%s' "$after" | marke_setzen backend "$BACKEND_TAG")"
  fi
  if [[ -n "$FRONTEND_TAG" ]]; then
    after="$(printf '%s' "$after" | marke_setzen frontend "$FRONTEND_TAG")"
  fi
  [[ "$after" == "$before" ]] && continue

  if [[ $DRY_RUN == 1 ]]; then
    changed+=("$f")
    continue
  fi
  # 🛑 WRITE FIRST, REPORT AFTER. Before this the file was counted BEFORE the
  # write ran -- a write-protected file produced "Permission denied" on stderr
  # and still stood in the success list afterwards. In a deploy the operator
  # then reads "5 manifests written" while nothing was written. A tool that
  # reports failures as success is worse than one that does nothing at all.
  if printf '%s\n' "$after" > "$f"; then
    changed+=("$f")
  else
    fehlgeschlagen+=("$f")
  fi
done < <(find "$DIR" -maxdepth 1 -name '*.yaml' -print0 | sort -z)

# 🛑 Report what SUCCEEDED first, then what failed. On a partial success the
# operator needs both halves: which manifests are already correct and which
# have to be brought along by hand.
if [[ ${#changed[@]} -eq 0 && ${#fehlgeschlagen[@]} -eq 0 ]]; then
  echo "  Bildmarken schon auf dem Live-Stand — nichts zu tun."
  exit 0
fi

if [[ ${#changed[@]} -gt 0 ]]; then
  if [[ $DRY_RUN == 1 ]]; then was='[dry-run] wuerde schreiben:'; else was='geschrieben:'; fi
  printf '  %s %d Manifest(e) in %s:\n' "$was" "${#changed[@]}" "$DIR"
  for f in "${changed[@]}"; do printf '    %s\n' "$f"; done
fi

if [[ ${#fehlgeschlagen[@]} -gt 0 ]]; then
  printf 'ERROR: %d Manifest(e) NICHT geschrieben — von Hand nachziehen:\n' \
    "${#fehlgeschlagen[@]}" >&2
  for f in "${fehlgeschlagen[@]}"; do printf '    %s\n' "$f" >&2; done
  exit 1
fi

# The hint names the RIGHT repo -- a private instance's manifests do not live
# in the repo that was deployed from.
repo_root="$(cd "$DIR" && git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "$repo_root" ]]; then
  echo
  echo "  🛑 NICHT committet — das bleibt Ihre Entscheidung:"
  echo "     cd $repo_root && git add -u k8s/ && git commit -m 'chore(k8s): Bildmarken auf den Live-Stand'"
fi
