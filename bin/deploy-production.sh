#!/usr/bin/env bash
#
# Renfield backend/frontend production deploy — codifies the mechanical backbone
# of the deploy-production skill (.claude/skills/deploy-production/SKILL.md),
# which stays the source of truth for the "why" and for everything this script
# DELIBERATELY does NOT do (see below).
#
# What it does (idempotent, re-runnable):
#   1. rsync the build contexts to the .159 build box
#   2. build + push the backend and/or frontend image to Harbor
#   2b. PREFLIGHT GATE: alembic head IN THE BUILT IMAGE vs. the running DB —
#      abort when they diverge and --migrate is not set (issue #1392)
#   3. (optional, --migrate) run the alembic-upgrade Job with the NEW image,
#      BEFORE the rollout, and verify it completed
#   4. `set image` + `rollout status` for the changed deployments
#   5. smoke test (/health, alembic current, live image tags)
#   6. clean the staging dir + prune old images on .159
#
# What it deliberately does NOT do (keep these manual — see the skill):
#   - SATELLITE deploys. Pi Zero SD cards brick on a bad restart (skill CRITICAL
#     SAFETY). Provision/restart satellites by hand with the satellite-deploy agent.
#   - voice-server / dlna-mcp / samsung-mcp images (separate repos + cadence).
#   - Harbor 504 retry judgment on the big deps layer (the skill documents it).
#   - Inventing version tags — you pass them in.
#
# Usage:
#   bin/deploy-production.sh --backend-tag 2026-06-25-satsec --frontend-tag v2.15.29 --migrate
#   bin/deploy-production.sh --backend-tag 2026-06-26-foo --skip-frontend          # backend only
#   bin/deploy-production.sh --frontend-tag v2.15.30 --skip-backend                # frontend only
#   bin/deploy-production.sh --backend-tag x --frontend-tag y --dry-run            # print, don't run
#   bin/deploy-production.sh --backend-tag x --skip-frontend --skip-migration-gate # bypass the gate (last resort)
#
# Conventions (from the live cluster): backend tags are date-based
# (YYYY-MM-DD-label), frontend tags are semver (vX.Y.Z). Both also get :latest.
set -euo pipefail

# --- config -----------------------------------------------------------------
BUILD_HOST="${RENFIELD_BUILD_HOST:-evdb@192.168.1.159}"
REGISTRY="${RENFIELD_REGISTRY:?set RENFIELD_REGISTRY, e.g. export RENFIELD_REGISTRY=your-registry.example/renfield}"
KCTX="${RENFIELD_KCTX:-renfield-private}"
NS="${RENFIELD_NS:-renfield}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- args -------------------------------------------------------------------
BACKEND_TAG="" FRONTEND_TAG="" DO_MIGRATE=0 DRY_RUN=0 SKIP_BACKEND=0 SKIP_FRONTEND=0 SKIP_MIGRATION_GATE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-tag)   BACKEND_TAG="$2"; shift 2 ;;
    --frontend-tag)  FRONTEND_TAG="$2"; shift 2 ;;
    --migrate)       DO_MIGRATE=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --skip-backend)  SKIP_BACKEND=1; shift ;;
    --skip-frontend) SKIP_FRONTEND=1; shift ;;
    # Deliberate bypass of the 2b gate. It exists so a probe that CANNOT answer
    # (build box unreachable, old pod already gone) does not leave the operator
    # with no way forward — but it has to be typed, which is the whole point:
    # the failure mode of #1392 was a gate that nobody had to think about.
    --skip-migration-gate) SKIP_MIGRATION_GATE=1; shift ;;
    -h|--help)       sed -n '2,34p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ $SKIP_BACKEND == 0 && -z "$BACKEND_TAG" ]] && { echo "ERROR: --backend-tag required (or --skip-backend)"; exit 2; }
[[ $SKIP_FRONTEND == 0 && -z "$FRONTEND_TAG" ]] && { echo "ERROR: --frontend-tag required (or --skip-frontend)"; exit 2; }

STAGING="/tmp/renfield-build-${BACKEND_TAG:-$FRONTEND_TAG}"
KUBECTL=(kubectl --context "$KCTX" -n "$NS")

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
run() { if [[ $DRY_RUN == 1 ]]; then printf '  [dry-run] %s\n' "$*"; else eval "$@"; fi; }
# Run a command on the build host. The command is passed to ssh as ONE argument,
# so the REMOTE shell does all the quoting — a payload containing single quotes
# (e.g. the prune's `--format '{{.Repository}}:{{.Tag}}'`) no longer breaks the
# way the old `run "ssh $HOST '$*'"` nested-single-quoting did under `eval`
# + `set -e` (which exited the script non-zero AFTER a successful deploy).
on_build() {
  if [[ $DRY_RUN == 1 ]]; then printf '  [dry-run] ssh %s: %s\n' "$BUILD_HOST" "$*"
  else ssh "$BUILD_HOST" "$*"; fi
}

# --- preflight --------------------------------------------------------------
log "preflight"
if ! git -C "$REPO_ROOT" diff --quiet || ! git -C "$REPO_ROOT" diff --cached --quiet; then
  echo "WARNING: working tree has uncommitted changes — the image will include them."
fi
echo "commit:   $(git -C "$REPO_ROOT" rev-parse --short HEAD) ($(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD))"
echo "backend:  $([[ $SKIP_BACKEND == 1 ]] && echo SKIP || echo "$BACKEND_TAG")"
echo "frontend: $([[ $SKIP_FRONTEND == 1 ]] && echo SKIP || echo "$FRONTEND_TAG")"
echo "migrate:  $DO_MIGRATE   dry-run: $DRY_RUN"
run "${KUBECTL[*]} version --output=json >/dev/null"  # cluster reachable?

# --- 1. rsync build contexts ------------------------------------------------
log "rsync build contexts → $BUILD_HOST:$STAGING"
on_build "rm -rf $STAGING; mkdir -p $STAGING/src/backend $STAGING/src/frontend"
RSYNC="rsync -az --delete"
if [[ $SKIP_BACKEND == 0 ]]; then
  run "$RSYNC --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
    --exclude='.coverage' --exclude='htmlcov' --exclude='.env' --exclude='.env.local' \
    --exclude='secrets/' --exclude='Users/' \
    $REPO_ROOT/src/backend/ $BUILD_HOST:$STAGING/src/backend/"
  # wakeword models + satellite source (bundled into the backend image)
  run "rsync -az $REPO_ROOT/data/wakeword-models/ $BUILD_HOST:$STAGING/src/backend/wakeword-models/"
  run "$RSYNC --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='provisioning' --exclude='tests' \
    $REPO_ROOT/src/satellite/ $BUILD_HOST:$STAGING/src/backend/satellite/"
  # Extra top-level Python packages from OUTSIDE this repo (e.g. a private
  # plugin named in PLUGIN_MODULES). Colon-separated package DIRECTORIES; each
  # lands at src/backend/<basename> → /app/<basename>, importable. Staging from
  # the canonical source here replaces a hand-copied, gitignored package in the
  # working tree — which a worktree checkout does not have and which goes stale.
  if [[ -n "${RENFIELD_BACKEND_EXTRA_PACKAGES:-}" ]]; then
    IFS=':' read -r -a _extra_pkgs <<< "$RENFIELD_BACKEND_EXTRA_PACKAGES"
    for pkg in "${_extra_pkgs[@]}"; do
      [[ -d "$pkg" ]] || { echo "ERROR: RENFIELD_BACKEND_EXTRA_PACKAGES entry is not a directory: $pkg" >&2; exit 2; }
      run "$RSYNC --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        ${pkg%/}/ $BUILD_HOST:$STAGING/src/backend/$(basename "${pkg%/}")/"
    done
  fi
fi
if [[ $SKIP_FRONTEND == 0 ]]; then
  run "$RSYNC --exclude='node_modules' --exclude='dist' --exclude='.vite' \
    --exclude='.cache' --exclude='.env' --exclude='.env.local' \
    $REPO_ROOT/src/frontend/ $BUILD_HOST:$STAGING/src/frontend/"
fi

# --- 2. build + push --------------------------------------------------------
if [[ $SKIP_BACKEND == 0 ]]; then
  log "build backend:$BACKEND_TAG"
  on_build "set -e; cd $STAGING/src/backend && \
    docker build -q -t $REGISTRY/backend:latest -t $REGISTRY/backend:$BACKEND_TAG -f Dockerfile ."

  # Plugin guard, BEFORE the push: every package the target instance loads via
  # PLUGIN_MODULES (ConfigMap renfield-env + Secret renfield-env-private) must
  # be importable in the image just built. A missing package does not stop the
  # backend — the loader logs and carries on — so without this check the
  # plugin is silently dead from the next rollout on. It happened four times.
  if [[ $DRY_RUN == 1 ]]; then
    printf '  [dry-run] verify PLUGIN_MODULES of %s importable in backend:%s\n' "$NS" "$BACKEND_TAG"
  else
    plugin_pkgs="$( { "${KUBECTL[@]}" get cm renfield-env -o jsonpath='{.data.PLUGIN_MODULES}' 2>/dev/null; echo; \
      "${KUBECTL[@]}" get secret renfield-env-private -o jsonpath='{.data.PLUGIN_MODULES}' 2>/dev/null | base64 -d 2>/dev/null; echo; } \
      | tr ',' '\n' | sed -E 's/[:.].*$//; s/^[[:space:]]+//; s/[[:space:]]+$//' | grep -v '^$' | sort -u | tr '\n' ' ')"
    if [[ -n "${plugin_pkgs// /}" ]]; then
      log "verify plugin packages importable: $plugin_pkgs"
      on_build "docker run --rm $REGISTRY/backend:$BACKEND_TAG python3 -c \
        'import importlib.util, sys; m = [p for p in sys.argv[1:] if importlib.util.find_spec(p) is None]; print(\"missing: \" + (\" \".join(m) or \"none\")); sys.exit(1 if m else 0)' \
        $plugin_pkgs" \
        || { echo "ERROR: $NS loads PLUGIN_MODULES packages the image lacks — NOT pushed. Stage them via RENFIELD_BACKEND_EXTRA_PACKAGES." >&2; exit 1; }
    fi
  fi

  log "push backend:$BACKEND_TAG"
  on_build "set -e; docker push -q $REGISTRY/backend:$BACKEND_TAG && docker push -q $REGISTRY/backend:latest"
fi
if [[ $SKIP_FRONTEND == 0 ]]; then
  log "build + push frontend:$FRONTEND_TAG"
  on_build "set -e; cd $STAGING/src/frontend && \
    docker build -q --build-arg VITE_FEATURE_VOICE_STREAM=true --build-arg VITE_BUILD_STAMP=$FRONTEND_TAG -t $REGISTRY/frontend:latest -t $REGISTRY/frontend:$FRONTEND_TAG -f Dockerfile . && \
    docker push -q $REGISTRY/frontend:$FRONTEND_TAG && docker push -q $REGISTRY/frontend:latest"
fi

# --- 2b. Vorflug-Tor: Alembic-Kopf im BILD gegen die laufende Datenbank ------
# Das Bild ist gepusht, aber NOCH NICHTS ausgerollt — die letzte Stelle, an der
# ein Abbruch kostenlos ist.
#
# Warum es das gibt (#1392): `--migrate` ist ein Schalter, den der Bediener
# setzen muss, und wer ihn vergisst, bekommt keinen Hinweis, sondern einen
# Ausfall. Am 2026-10-11 ~35 min Produktionsschaden auf dem Haushalt: der PR
# enthielt keine Migration, das Bild wird aber aus `main` gebaut, und dort lag
# `pc20260929_kg_validity`. Das ORM waehlt jede Spalte unbedingt → roher
# SQLAlchemy-Fehler samt SQL in der Seite, KG-Extraktion tot. `/health/ready`
# meldete durchgehend `healthy`; gefunden hat es allein der Browser-E2E.
#
# 🛑 Das Tor MIGRIERT NICHT von selbst. Eine Migration ist eine Entscheidung —
# ein stiller Rollout darueber hinweg ist keine.
#
# 🛑 Und es laesst bei Unklarheit NICHT durch: schlaegt eine der beiden
# Abfragen fehl, bricht es ab (ausser im --dry-run, der nichts veraendern
# darf). Ein Tor, das im Zweifel oeffnet, ist genau der Fehler, gegen den
# dieses Issue geschrieben ist.

# Nimmt die Ausgabe von `alembic heads`/`alembic current` auf stdin und gibt je
# Zeile EINE Revision aus. Beide Kommandos schreiben INFO-Zeilen auf denselben
# Strom, das Bild zusaetzlich eine Pydantic-Warnung, und Annotationen stehen in
# Klammern (`(head)`, `(branchlabel)`). Bewusst grep-UNABHAENGIG von Dateinamen:
# Dateinamen tragen die Revision NICHT — `pc20260929_kg_relation_validity.py`
# haelt die Revision `pc20260929_kg_validity`, und genau diese Verwechslung hat
# den Bediener am 2026-10-11 ein zweites Mal beruhigt.
# Bewusst ohne `mapfile`/Arrays: macOS liefert bash 3.2 aus, und dieses Skript
# wird vom Laptop gestartet — ein bash-4-Builtin waere hier ein Tor, das auf dem
# Rechner des Bedieners gar nicht erst laeuft.
alembic_revs() {
  sed -E 's/\([^)]*\)//g' | tr -d '[:blank:]' | grep -E '^[0-9A-Za-z][0-9A-Za-z_-]{2,}$' || true
}
count_lines() { printf '%s' "$1" | grep -c . || true; }

# Abbruch des Tores. Im --dry-run wird nur GEZEIGT, was es taete: ein Probelauf
# veraendert nichts und darf deshalb auch nicht am Tor scheitern — er ist die
# einzige Moeglichkeit, das Tor vor einem echten Deploy zu proben.
gate_abort() {
  if [[ $DRY_RUN == 1 ]]; then
    printf '  [dry-run] TOR WUERDE ABBRECHEN: %s\n' "$1"
  else
    printf '\n\033[1;31mERROR: %s\033[0m\n' "$1" >&2
    echo "       Bewusstes Uebergehen (nur wenn die Lage geklaert ist): --skip-migration-gate" >&2
    exit 1
  fi
}

if [[ $SKIP_BACKEND == 1 ]]; then
  : # kein neues Backend-Bild → es gibt keinen Kopf zu vergleichen
elif [[ $SKIP_MIGRATION_GATE == 1 ]]; then
  log "Migrations-Tor UEBERSPRUNGEN (--skip-migration-gate)"
  echo "  WARNING: der Alembic-Kopf des Bildes wurde NICHT gegen $NS geprueft." >&2
else
  log "Vorflug-Tor: Alembic-Kopf im Bild gegen $NS"
  IMAGE_REF="$REGISTRY/backend:$BACKEND_TAG"
  GATE_PROBED=1

  # Beide Abfragen laufen AUCH im --dry-run: sie sind lesend, und ein Probelauf,
  # der das Tor nicht ausfuehrt, probt es auch nicht. Deshalb hier `ssh` direkt
  # statt `on_build` (das im --dry-run nur druckt).
  IMG_HEADS=""
  if img_out="$(ssh "$BUILD_HOST" "docker run --rm $IMAGE_REF sh -c 'cd /app && python -m alembic heads' 2>&1")"; then
    IMG_HEADS="$(printf '%s\n' "$img_out" | alembic_revs)"
  else
    printf '%s\n' "$img_out" | tail -5 >&2
    gate_abort "Alembic-Kopf aus $IMAGE_REF nicht ermittelbar (Bild auf $BUILD_HOST nicht vorhanden oder docker-Fehler). Im Zweifel wird NICHT ausgerollt."
    GATE_PROBED=0
  fi

  # Die laufende Datenbank: der noch laufende ALTE Pod liest `alembic_version`.
  # Robuster als psql gegen den CNPG-Pod, dessen Name je Namensraum abweicht.
  DB_REVS=""
  if db_out="$("${KUBECTL[@]}" exec deploy/backend -c backend -- sh -c 'cd /app && python -m alembic current' 2>&1)"; then
    DB_REVS="$(printf '%s\n' "$db_out" | alembic_revs)"
  else
    printf '%s\n' "$db_out" | tail -5 >&2
    gate_abort "Alembic-Revision der Datenbank in $NS nicht ermittelbar (kein laufender backend-Pod?). Im Zweifel wird NICHT ausgerollt."
    GATE_PROBED=0
  fi

  if [[ $GATE_PROBED == 1 ]]; then
    IMG_HEAD_COUNT="$(count_lines "$IMG_HEADS")"
    DB_REV_COUNT="$(count_lines "$DB_REVS")"
    IMG_HEAD="$(printf '%s' "$IMG_HEADS" | head -1)"
    DB_REV="$(printf '%s' "$DB_REVS" | head -1)"
    echo "  Bild  $IMAGE_REF"
    echo "        Kopf:     ${IMG_HEADS//$'\n'/ } (${IMG_HEAD_COUNT})"
    echo "  DB    $NS"
    echo "        Revision: ${DB_REVS//$'\n'/ } (${DB_REV_COUNT})"

    if [[ $IMG_HEAD_COUNT -eq 0 ]]; then
      gate_abort "Aus der Ausgabe von \`alembic heads\` liess sich keine Revision lesen — Format geaendert? Es wird nicht geraten."
    elif [[ $IMG_HEAD_COUNT -gt 1 ]]; then
      # Gegabelte Kette. Bisher wurde das nur beim SCHREIBEN einer Migration
      # geprueft; hier steht es auf der Rollout-Seite.
      gate_abort "Mehrere Alembic-Koepfe im Bild (gegabelte Kette): ${IMG_HEADS//$'\n'/ }. Erst zusammenfuehren (merge-Revision), dann ausrollen."
    elif [[ $DB_REV_COUNT -ne 1 ]]; then
      # Leere oder gegabelte `alembic_version`. Mit --migrate zieht der
      # Migrationsschritt das gerade, ohne ihn ist die Lage unklar.
      if [[ $DO_MIGRATE == 1 ]]; then
        echo "  WARNING: Datenbank-Revision in $NS unklar ($DB_REV_COUNT Eintraege) — --migrate ist gesetzt, der Migrationsschritt laeuft davor." >&2
      else
        gate_abort "Datenbank-Revision in $NS unklar ($DB_REV_COUNT Eintraege) — leere oder gegabelte \`alembic_version\`. Nicht ausrollen."
      fi
    elif [[ "$IMG_HEAD" == "$DB_REV" ]]; then
      echo "  OK: Bild und Datenbank stehen auf $IMG_HEAD — keine Migration ausstehend."
    # Koepfe verschieden. Die Richtung entscheidet, und sie wird ERFRAGT, nicht
    # aus Bildmarken oder Datumsnamen geraten: kennt das Bild die DB-Revision,
    # dann liegt sie in seiner Kette und die DB ist ZURUECK (der gefaehrliche
    # Fall). Kennt es sie nicht, traegt die DB eine Revision, die dieses Bild
    # nie gesehen hat — nach einem Rueckfall der uebliche Fall.
    elif ssh "$BUILD_HOST" "docker run --rm $IMAGE_REF sh -c 'cd /app && python -m alembic show $DB_REV' >/dev/null 2>&1"; then
      if [[ $DO_MIGRATE == 1 ]]; then
        echo "  Datenbank ist ZURUECK ($DB_REV → $IMG_HEAD) — --migrate ist gesetzt, der Migrationsschritt laeuft vor dem Rollout."
      else
        gate_abort "$(printf 'Die Datenbank in %s ist ZURUECK hinter dem Bild — ohne --migrate wird das ein Ausfall.\n       Alembic-Kopf im Bild:  %s\n       Alembic in der DB:     %s\n       Das ORM waehlt jede Spalte unbedingt: ein Pod auf diesem Bild scheitert an JEDER\n       Abfrage auf die geaenderten Tabellen, und /health/ready meldet dabei weiter healthy.\n       → --migrate nachlegen (die Migration laeuft dann VOR dem Rollout).' "$NS" "$IMG_HEAD" "$DB_REV")"
      fi
    else
      # Erlaubt (zusaetzliche Spalten sind nullbar), aber nicht verschweigen.
      echo "  WARNING: die Datenbank in $NS traegt $DB_REV — eine Revision, die dieses Bild nicht kennt (Kopf: $IMG_HEAD)." >&2
      echo "           Das ist ein RUECKFALL auf ein aelteres Bild. Erlaubt, solange die zusaetzlichen Spalten nullbar sind — nachsehen." >&2
    fi
  fi
fi

# --- 3. migrate (BEFORE rollout, with the new image) ------------------------
if [[ $DO_MIGRATE == 1 ]]; then
  log "alembic migration (new image, before rollout)"
  run "${KUBECTL[*]} delete job alembic-upgrade --ignore-not-found"
  # The public manifest carries the registry placeholder — substitute the real
  # registry + the freshly pushed tag at apply time (a raw apply fails the
  # image pull; bit the 2026-08-18 pdfsplit deploy).
  run "sed 's#your-registry.example/renfield/backend:latest#$REGISTRY/backend:${BACKEND_TAG:-latest}#' $REPO_ROOT/k8s/alembic-upgrade-job.yaml | ${KUBECTL[*]} apply -f -"
  if [[ $DRY_RUN == 0 ]]; then
    if ! "${KUBECTL[@]}" wait --for=condition=Complete job/alembic-upgrade --timeout=300s; then
      echo "ERROR: migration job did not complete — logs follow:" >&2
      "${KUBECTL[@]}" logs job/alembic-upgrade >&2 || true
      echo "(left the job in place; 'Multiple head revisions' = forked chain, fix-forward needed)" >&2
      exit 1
    fi
    "${KUBECTL[@]}" logs job/alembic-upgrade | grep -iE "running upgrade|no upgrade|error" || true
    "${KUBECTL[@]}" delete job alembic-upgrade
  fi
fi

# --- 4. rollout (pinned tags → set image, not rollout restart) --------------
log "rollout"
if [[ $SKIP_BACKEND == 0 ]]; then
  run "${KUBECTL[*]} set image deploy/backend backend=$REGISTRY/backend:$BACKEND_TAG"
  run "${KUBECTL[*]} set image deploy/document-worker worker=$REGISTRY/backend:$BACKEND_TAG"  # container is 'worker'
  # EVERY deployment running the backend image, not just the two obvious ones.
  # meeting-worker and pdf-split-worker were skipped here for months and drifted
  # several tags behind on BOTH instances — harmless while migrations stayed
  # additive, but it is silent version skew that grows with every rollout, and
  # nothing reports it. Optional by design: an instance need not run them.
  for w in meeting-worker pdf-split-worker; do
    # The existence probe is a live call, so it must not run in a dry run — the
    # rest of a dry run touches nothing, and without a cluster it would report
    # both workers as absent, which is the opposite of informative.
    if [[ $DRY_RUN == 1 ]] || ${KUBECTL[*]} get deploy "$w" >/dev/null 2>&1; then
      run "${KUBECTL[*]} set image deploy/$w worker=$REGISTRY/backend:$BACKEND_TAG"
    else
      echo "  (no $w in $NS — skipped)"
    fi
  done
fi
[[ $SKIP_FRONTEND == 0 ]] && run "${KUBECTL[*]} set image deploy/frontend frontend=$REGISTRY/frontend:$FRONTEND_TAG"
if [[ $DRY_RUN == 0 ]]; then
  [[ $SKIP_BACKEND == 0 ]] && "${KUBECTL[@]}" rollout status deploy/backend --timeout=600s
  [[ $SKIP_BACKEND == 0 ]] && "${KUBECTL[@]}" rollout status deploy/document-worker --timeout=600s
  [[ $SKIP_FRONTEND == 0 ]] && "${KUBECTL[@]}" rollout status deploy/frontend --timeout=600s
fi

# --- 5. smoke test ----------------------------------------------------------
log "smoke test"
if [[ $DRY_RUN == 0 ]]; then
  "${KUBECTL[@]}" exec deploy/backend -c backend -- curl -sS http://localhost:8000/health | head -c 120; echo
  "${KUBECTL[@]}" exec deploy/backend -c backend -- alembic current 2>&1 | grep -vi INFO | tail -1
  "${KUBECTL[@]}" get deploy backend document-worker frontend \
    -o 'custom-columns=D:.metadata.name,IMG:.spec.template.spec.containers[0].image'
fi

# --- 5b. manifest drift (read-only, warn-only) --------------------------------
# After `set image` the live objects must still match the repo manifests (images
# excepted for the placeholder manifests). If they don't, the next routine
# `kubectl apply -f` regresses the instance — flag it now, never block the deploy.
log "manifest drift check (read-only)"
MANIFESTS_DIR="${RENFIELD_MANIFESTS_DIR:-}"
[[ -z "$MANIFESTS_DIR" && "$NS" == "renfield" ]] && MANIFESTS_DIR="$REPO_ROOT/k8s"
# A private instance keeps its manifests in a sibling repo. Look there by
# convention instead of skipping: the check only ran when someone remembered to
# export RENFIELD_MANIFESTS_DIR, so on xidra it effectively never ran — and the
# image tag in x-ren silently fell ELEVEN DAYS behind the live deployment
# (found 2026-09-23). A `kubectl apply -k` would have rolled four deployments
# back onto an image whose ORM predates the live schema. A check that depends on
# being remembered is not a check.
if [[ -z "$MANIFESTS_DIR" ]]; then
  for candidate in "$REPO_ROOT/../x-ren/k8s"; do
    [[ -d "$candidate" ]] && { MANIFESTS_DIR="$candidate"; break; }
  done
  [[ -n "$MANIFESTS_DIR" ]] && echo "  (manifests: $MANIFESTS_DIR — sibling repo, by convention)"
fi
# Die Bildmarken ZUERST nachziehen, dann pruefen: `kubectl set image` aendert die
# LIVE-Objekte, nicht die Dateien, also driften sie nach JEDEM Deploy erneut. Die
# Pruefung darunter hat das bisher nur gemeldet; behoben wurde es von Hand, und
# dreimal in Folge hiess der naechste Commit in x-ren "Bildmarken auf den
# Live-Stand". Committet wird hier NICHTS — das Skript sagt nur, was anliegt.
if [[ -n "$MANIFESTS_DIR" ]]; then
  log "Bildmarken in die Manifeste schreiben (ohne Commit)"
  # Explizite `if`-Bloecke statt `[[ … ]] && cmd`: unter `set -e` ist der
  # Rueckgabewert einer solchen Kette der des fehlgeschlagenen Tests, und ob das
  # abbricht, haengt am Kontext. In einem Deploy-Skript will man das nicht raten.
  tag_args=(--dir "$MANIFESTS_DIR")
  if [[ -n "$BACKEND_TAG"  ]]; then tag_args+=(--backend-tag "$BACKEND_TAG"); fi
  if [[ -n "$FRONTEND_TAG" ]]; then tag_args+=(--frontend-tag "$FRONTEND_TAG"); fi
  if [[ $DRY_RUN == 1      ]]; then tag_args+=(--dry-run); fi
  "$REPO_ROOT/bin/k8s-write-image-tags.sh" "${tag_args[@]}" \
    || echo "WARNING: Bildmarken konnten nicht geschrieben werden — von Hand nachziehen" >&2
fi

if [[ -z "$MANIFESTS_DIR" ]]; then
  echo "  (skipped: set RENFIELD_MANIFESTS_DIR to the manifests of $NS, e.g. ../x-ren/k8s)"
elif [[ $DRY_RUN == 1 ]]; then
  printf '  [dry-run] %s --context %s -n %s --dir %s\n' "$REPO_ROOT/bin/k8s-drift-check.sh" "$KCTX" "$NS" "$MANIFESTS_DIR"
else
  RENFIELD_REGISTRY="$REGISTRY" "$REPO_ROOT/bin/k8s-drift-check.sh" --context "$KCTX" -n "$NS" --dir "$MANIFESTS_DIR" \
    || echo "WARNING: manifests drift from live $NS (see above) — reconcile them (x-ren: commit the image-tag bump) before any kubectl apply" >&2
fi

# --- 6. cleanup + prune on the build box ------------------------------------
log "cleanup + image prune on $BUILD_HOST"
on_build "rm -rf $STAGING; \
  for repo in backend frontend; do \
    docker images \"$REGISTRY/\$repo\" --format '{{.Repository}}:{{.Tag}}' | tail -n +4 | xargs -r -n1 docker rmi 2>/dev/null || true; \
  done; \
  docker image prune -f >/dev/null 2>&1; docker builder prune -f --keep-storage 10GB >/dev/null 2>&1; \
  df -h / | tail -1" \
  || echo "WARNING: build-box cleanup failed (non-fatal — deploy already succeeded); check disk on $BUILD_HOST" >&2

log "DONE"
