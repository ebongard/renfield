---
paths:
  - "k8s/**"
  - "bin/deploy-production.sh"
  - "bin/k8s-drift-check.sh"
  - "src/backend/Dockerfile"
  - "src/frontend/nginx.conf"
---
# Deploy & manifests

Loaded only when a manifest or the deploy script is read. The runbook is the `deploy-production` skill
(`.claude/skills/deploy-production/SKILL.md`) — user-invoked; canonical cluster reference `docs/KUBERNETES_DEPLOYMENT.md`.

## Reality
- **GitHub CI does not run for this project** (`ci.yml`, `pr-check.yml`, `release.yml` are kept for the audit trail only;
  a tag builds nothing). Backend tests run on the build box `192.168.1.159` in the `renfield-backend` container; images
  are built there and pushed to the registry; production is the private k8s cluster (context `renfield-private`).
- The registry hostname is NOT committed: manifests carry the placeholder `your-registry.example/renfield/…`, scripts
  need `RENFIELD_REGISTRY`.
- Two instances: household (`renfield`, auth off) and xidra (`renfield-xidra`, auth on). xidra's manifests and config
  live in the private `x-ren` repo — never apply household files there.

## Never
- Never `kubectl apply -f` a placeholder manifest raw (`ImagePullBackOff`, and a `:latest` apply moves a pinned deploy):
  render first with `bin/k8s-drift-check.sh --render <file> | kubectl -n <ns> apply -f -`. Run the drift check before
  ANY apply; it cannot see fields that exist only live.
- Never put a `namespace:` field into `k8s/alembic-upgrade-job.yaml` — it overrides `-n` and would silently migrate the
  wrong instance. Apply the Job BEFORE the rolling restart.
- Never rebuild the `renfield-mcp-config` ConfigMap from files: `kg_scopes.yaml` is not in the repo and the live
  `mail_accounts.yaml` is not the committed default. Swap ONLY the changed key. `mcp_servers.yaml`, `agent_roles.yaml`,
  `kg_scopes.yaml`, `mail_accounts.yaml` are ConfigMap-served and must NEVER appear in the backend image.
- Never `kubectl patch` the live `renfield-env` ConfigMap as the source of truth: edit `k8s/configmap.yaml`, commit,
  apply, then restart backend + workers. A live change that is not in git is a bug.
- Never touch satellites from a deploy (Pi Zero SD cards brick on a bad restart) — see `.claude/rules/satellites.md`.

## Ownership
Images are owned by `kubectl set image` (the deploy script); everything else in a workload is owned by the manifest.
`frontend` and `voice-server` run pinned tags, so `rollout restart` is a no-op for them. Every backend rebuild must
stage the private plugin packages named in `PLUGIN_MODULES` via `RENFIELD_BACKEND_EXTRA_PACKAGES`; the script refuses to
push an image that cannot import them. Roll all four backend-image deployments together (backend, document-worker,
meeting-worker, pdf-split-worker).

## Frontend propagation
The frontend is a PWA: `sw.js`, `registerSW.js`, `index.html`, `manifest.webmanifest` must be `no-cache`, hashed bundles
`immutable`. A location with its own `add_header` stops inheriting the security headers — re-declare them. A
header-only change needs a build bump (`__BUILD_STAMP__`) or existing clients keep the stale shell. Open tabs need a
reload after a frontend deploy.

## Probes
Backend readiness `/health/ready` (DB reachability), liveness `/health/live` (dependency-free — a DB-dependent liveness
would restart-storm every replica during a DB outage). A rollout against a dead DB waits at NotReady; that is intended.

## Ein Deploy ist keine Abhängigkeits-Aktualisierung
🛑 **Eine offene Untergrenze macht jeden Bau zur Wette.** Am 2026-10-04 führte `requirements.txt`
`sqlalchemy>=2.0.25` ohne den `[asyncio]`-Extra; `greenlet` war **nie** deklariert und kam nur transitiv mit.
Der Bau sprang von 2.0.54 auf **2.1.3**, wo greenlet nicht mehr implizit ist — Backend **und alle drei Worker**
starben beim Import (`CrashLoopBackOff`, Dienst unten bis zum Rollback auf das letzte gute Bild).
Behoben: `sqlalchemy[asyncio]>=2.0.25,<2.1`, dazu `tests/backend/test_async_stack_requirements.py` als Riegel.
🛑 **Der Prüfstand kann das NICHT finden** — er läuft im ALTEN Bild und hat die alten Pakete. Ein
Umgebungstest wäre genau dort grün, wo er nichts beweist; geprüft wird deshalb die **Deklaration**.
🛑 **Nach einem Bau, der Abhängigkeiten neu auflöst, die Versionen vergleichen** (`pip show` im alten gegen das
neue Bild). Ein grüner Testlauf beweist hier nichts.

## Bildmarken in den Manifesten
🛑 `kubectl set image` ändert die LIVE-Objekte, NICHT die Dateien — ohne Nachziehen wirft ein späteres
`kubectl apply -f` die Instanz auf das alte Bild zurück. `bin/deploy-production.sh` ruft dafür
`bin/k8s-write-image-tags.sh` **vor** der Drift-Prüfung und schreibt die Marken selbst; **committet
aber nicht** (die Manifeste einer privaten Instanz liegen in einem zweiten Repo — der Hinweis nennt das
richtige). Vorher war das Handarbeit nach jedem Deploy, und dreimal in Folge hieß der nächste x-ren-Commit
„Bildmarken auf den Live-Stand".
🛑 Ersetzt wird **nur die Marke hinter `renfield/<bild>:`**, nie der Registry-Name: das öffentliche Repo
trägt dort absichtlich den Platzhalter `your-registry.example`.
🛑 **`:latest` bleibt `:latest`** — ein gleitender Zeiger ist kein festgeschriebener Stand. Die öffentlichen
Manifeste tragen ihn absichtlich; ein frisch über `overlays/private/` aufgesetzter Cluster zöge sonst für
immer das Bild des Tages, an dem zuletzt jemand deployt hat. Beim ersten echten Lauf schrieb der Schritt
genau das in sieben öffentliche Manifeste (2026-09-29, zurückgenommen). Die private Instanz ist davon nicht
betroffen: dort stehen überall konkrete Marken, und die werden nachgezogen wie gehabt.
🛑 Ein fehlgeschlagener Schreibvorgang (schreibgeschützte Datei) wird **gemeldet und beendet mit 1** — er
zählte vorher als Erfolg, und der Betreiber las „5 Manifeste geschrieben", während nichts geschrieben war.
Bei Teilerfolg nennt die Ausgabe beide Hälften: was steht und was von Hand nachzuziehen ist.
