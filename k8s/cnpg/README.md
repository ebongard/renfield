# CNPG HA Postgres for the renfield app DB (`renfield-pg`)

Manifests for moving the **renfield application database** off the single-node
`postgres` StatefulSet onto a 3-instance CloudNativePG (CNPG) cluster with
automatic failover + external S3 backups (PITR).

**Scope:** the `renfield` app DB only, **household (`ns renfield`) first**.
Paperless (`paperlessdb`) and the digital-twin DB stay on the legacy `postgres`
StatefulSet — it keeps running for them. xidra is Phase 6, after a household soak.

These files are **NOT in `kustomization.yaml`** — they are applied by hand,
phase by phase, so nothing here touches prod until you run the step.

## Files
| File | What |
|---|---|
| `00-storageclass-pg.yaml` | `longhorn-pg` SC — 1 Longhorn replica, node-local (PG replicates 3×) |
| `10-objectstore.yaml` | Barman ObjectStore → Garage S3 (`192.168.1.9:30188`, bucket `renfield-pg-backups`) |
| `20-cluster-renfield-pg.yaml` | The 3-instance `Cluster` — quorum sync, anti-affinity, pgvector image, import-from-legacy |
| `30-scheduledbackup.yaml` | Nightly base backup via the Barman plugin |
| `40-netpol-cnpg.yaml` | NetworkPolicy for the CNPG pods (app + intra-cluster + operator) |
| `50-pgdump-cronjob.yaml` | Nightly `pg_dump -Fc` → NFS (image-independent fallback) |
| `Dockerfile.pgvector` | Thin CNPG-16 + pgvector image (Phase 2 build) |

## Already done (Garage install)
- Garage S3 RUNNING on `192.168.1.9:30188`, bucket `renfield-pg-backups`, key `renfield-cnpg` (rw).
- Secret `renfield-pg-backup-s3` (ns `renfield`) with `ACCESS_KEY_ID` + `ACCESS_SECRET_KEY`.
- Verified end-to-end: an in-cluster pod did `ListBuckets`/`ListObjects` against the bucket.

---

## Phase 1 — operator + prerequisites
Pin to the current releases (verify latest before running):

```bash
# cert-manager (Barman plugin dependency)
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s

# CNPG operator (installs into ns cnpg-system)
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.27/releases/cnpg-1.27.0.yaml
kubectl -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=180s

# Barman Cloud plugin (needs cert-manager). Pin the version (v0.14.0 was verified
# with operator 1.27.0); bump deliberately, don't float on latest/.
kubectl apply -f https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.14.0/manifest.yaml
kubectl -n cnpg-system rollout status deploy/barman-cloud --timeout=180s

# Renfield-side prereqs (safe — no cluster created yet)
kubectl apply -f 00-storageclass-pg.yaml
kubectl apply -f 10-objectstore.yaml
kubectl apply -f 40-netpol-cnpg.yaml
```

## Phase 2 — build the pgvector operand image
The stock CNPG image has no pgvector. Build the thin image on `.159` and push to
Harbor (see header of `Dockerfile.pgvector`). Re-pin the base tag to the current
16.x minor. After the cluster is up, verify `\dx vector` is **≥ 0.8.1**.

## Phase 3 — dry-run + failover drill (scratch namespace, BEFORE prod)
Copy `20-cluster-renfield-pg.yaml` into a scratch ns, import from a **copy** of
the DB, then:
1. `kubectl cnpg status renfield-pg` → healthy, 1 primary + 2 standbys in sync.
2. Kill the primary pod → `renfield-pg-rw` repoints within seconds.
3. `kubectl drain` the primary's node → failover + the drained instance rejoins &
   re-syncs. Drain exactly ONE node at a time and wait for 3/3 before the next
   (see the dataDurability tradeoff in `20-cluster-renfield-pg.yaml`); a full
   drain also evicts co-located prod pods, so a cordon + pod-delete is the lighter
   test when you only need to prove promotion + `-rw` repoint.
4. `\dx vector` + a real similarity query return correct rows.

## Phase 4 — household cutover (`ns renfield`)
1. **Pre-create the app-user secret** so the app password matches the legacy one
   (value taken from the existing secret; never printed):
   ```bash
   PW=$(kubectl -n renfield get secret renfield-secrets -o jsonpath='{.data.postgres-password}' | base64 -d)
   kubectl -n renfield create secret generic renfield-pg-app \
     --type=kubernetes.io/basic-auth \
     --from-literal=username=renfield \
     --from-literal=password="$PW"
   unset PW
   ```
2. **Write-freeze — held CONTINUOUSLY until step 5 completes.** The import is a
   `pg_dump` snapshot taken at cluster-creation; ANY write to the legacy DB
   between that snapshot and the app being repointed is lost at cutover. So freeze
   *before* creating the cluster and keep it frozen through the repoint+rollout:
   ```bash
   kubectl -n renfield scale deploy/backend deploy/document-worker \
     deploy/meeting-worker deploy/pdf-split-worker --replicas=0
   ```
3. Create the cluster (imports from live `postgres`). These files carry the
   `your-registry.example` placeholder — substitute the real registry at apply
   time (same pattern as the alembic job in the deploy skill):
   ```bash
   sed "s#your-registry.example/renfield#$RENFIELD_REGISTRY#" 20-cluster-renfield-pg.yaml | kubectl apply -f -
   kubectl cnpg status renfield-pg -n renfield   # wait: import complete, 3/3 healthy
   ```
4. Verify on the new cluster: `\dx vector` (≥0.8.6), HNSW indexes present, a real
   RAG similarity query returns correct rows (count-only is enough).
5. **Repoint the renfield app DB** `@postgres` → `@renfield-pg-rw` (host swap only):
   - `k8s/backend.yaml:185`
   - `k8s/document-worker.yaml:119`
   - `k8s/meeting-worker.yaml:90`
   - `k8s/pdf-split-worker.yaml:81`
   - `k8s/alembic-upgrade-job.yaml:62`
   - `k8s/configmap.yaml:46` (the `__PG_PASSWORD__` template — keep consistent)

   **Do NOT touch** Paperless `PAPERLESS_DBHOST=postgres` or the twin DB config —
   they stay on the legacy StatefulSet.
6. `kubectl apply -f k8s/configmap.yaml`, run the alembic job against `-rw` FIRST,
   then scale backend + workers back up (this ends the write-freeze):
   ```bash
   kubectl -n renfield scale deploy/backend deploy/document-worker \
     deploy/meeting-worker deploy/pdf-split-worker --replicas=1   # or prior replica counts
   ```
7. **Turn on backups** (both the plugin base backup and the pg_dump fallback are
   inert until applied — WAL archiving alone is NOT restorable):
   ```bash
   kubectl apply -f 30-scheduledbackup.yaml
   sed "s#your-registry.example/renfield#$RENFIELD_REGISTRY#" 50-pgdump-cronjob.yaml | kubectl apply -f -
   kubectl cnpg backup renfield-pg -n renfield   # take an immediate base backup — don't wait for 02:30
   ```
8. Browser E2E (chat + Wissenssuche), watch Traefik/backend logs. Soak a few days.

## Phase 5 — backups sharp + verified
- Confirm the on-demand base backup + continuous WAL archiving actually LAND in
  Garage (`kubectl cnpg status renfield-pg` shows `First/Last Point of Recoverability`
  + list the bucket). NB: the ObjectStore CR has no `region` field; barman/boto
  default the S3 region — the ListBuckets connectivity check does not exercise
  barman's signing, so explicitly confirm a base backup + a WAL segment upload
  succeed before trusting the backup (Garage is generally region-lenient, but
  verify, don't assume).
- **Test a PITR restore into a scratch cluster** (`bootstrap.recovery` from the
  ObjectStore) — a backup you have not restored is not a backup.
- Confirm the nightly `pg_dump` CronJob wrote a dump to NFS and restores cleanly.

## Phase 6 — xidra (`ns renfield-xidra`)
Repeat 4–5 in `renfield-xidra` after the household soak. Repoint targets:
`k8s/xidra/meeting-worker.yaml:70`, `k8s/xidra/renfield-env.configmap.yaml:22`
(+ the base files xidra reuses). The xidra Barman ObjectStore/secret + app
secret need their own copies in that ns; Garage can hold a second bucket
(`xidra-pg-backups`) with its own key.

---

## Rollback (per cutover)
The legacy `postgres` StatefulSet was never stopped → set `DATABASE_URL` back to
`@postgres`, `rollout restart`. Divergence window = time since cutover (small
write volume during soak; acceptable).

## Open decisions / honest flags
- **pgvector image (Phase 2):** required custom build — the plan's "pgvector rides
  along in the stock image" assumption is **false**; the stock CNPG operand has no
  pgvector. Handled by `Dockerfile.pgvector`.
- **asyncpg + TLS:** CNPG's default pg_hba allows scram over the pod network
  without client certs, so `@renfield-pg-rw` should work unchanged. If the app
  can't connect, append `?ssl=require` to the DATABASE_URL. Verify in Phase 4.
- **NFS dump export:** `50-pgdump-cronjob.yaml` reuses `nfs-csi-renfield-uploads`.
  A dedicated backup export/SC would cleanly separate dumps from app uploads.
- **Garage is on the NAS box itself:** external to the k8s *cluster* (survives a
  cluster loss) but NOT to a NAS-box loss. A later off-box/offsite sync of the
  bucket would complete 3-2-1. Deferred.
- **Single control-plane node (`k8s-cp`):** the real availability ceiling. DB-HA
  is real but capped until the control plane is also HA. Separate track.
- **Deferred:** Paperless + twin DB migration into CNPG; control-plane HA;
  ImageVolume-pgvector (needs k8s 1.33+/PG18).
