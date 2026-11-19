# cuda-host — the 5090 host `cuda.local`

The chat and vision LLM of both instances runs outside Kubernetes on the RTX 5090 host
`cuda.local` (port 8081). This directory holds its host configuration; background and the
container definition are in [`docs/GPU_TOPOLOGY.md`](../../docs/GPU_TOPOLOGY.md) §2.

| File | Purpose |
|---|---|
| `llama-server.service` | systemd unit that runs the `llama-server` container (llama.cpp, image by digest) |
| `llm-ab.sh` | A/B of the installed image against a candidate, on port 8082, while the unit is stopped |
| `llm-check.py` | the check `llm-ab.sh` runs: plain chat, one tool call, decode speed (3 runs) |

## Why a systemd unit

The container used to be a hand-made `docker run --restart unless-stopped`. At boot dockerd
started it before the NFS share `/mnt/llm` was mounted; the bind mount of the model file
failed, the container exited with 127, and dockerd never retried it. The backends' init
container `wait-for-deps` waits on `cuda.local:8081/health`, so both instances hung in Init.

The unit fixes that in three ways:

- `RequiresMountsFor=/mnt/llm` adds `Requires=` and `After=` on `mnt-llm.mount` (the share is an
  fstab entry with `x-systemd.automount`, so systemd generates that mount unit): the service
  starts only after the share is really mounted.
- `ExecStartPre=/usr/bin/test -r <model>` fails while a model file is absent, so docker never
  gets to bind a missing path.
- `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=0`: any failed start is retried
  every 10 s, forever. A failed start is no longer final.

Limit: if the mount itself fails (NFS server unreachable for longer than the fstab
`x-systemd.mount-timeout=180`), systemd reports a *dependency* failure, and `Restart=` does not
cover that. Then `sudo systemctl start llama-server` once the NFS server is back.

## Install (on `cuda.local`, needs sudo)

Check first that no long request is running (`curl -s localhost:8081/slots`, every slot
`is_processing: false`) — the switch is a short LLM outage for both instances.

```bash
sudo install -m 0644 llama-server.service /etc/systemd/system/llama-server.service
sudo systemctl daemon-reload
# hand the running container over to systemd
docker update --restart=no llama-server
docker stop llama-server && docker rm llama-server
sudo systemctl enable --now llama-server.service
# the model loads from NFS in about 1-2 minutes
until curl -fsS localhost:8081/health | grep -q '"ok"'; do sleep 5; done
```

Verify: `docker inspect llama-server` shows the same image digest and arguments as the unit,
`curl -s localhost:8081/props` reports the build and the model alias `qwen3.6`.

## Change the model, the arguments or the image

Edit `llama-server.service` here, commit, copy it to `/etc/systemd/system/`, then
`sudo systemctl daemon-reload && sudo systemctl restart llama-server`. Pin a new image by
digest (`docker buildx imagetools inspect ghcr.io/ggml-org/llama.cpp:server-cuda-<build>`),
never by the moving `server-cuda` tag. Update `docs/GPU_TOPOLOGY.md` §2 in the same commit.

A build can be pushed **without any tag**: upstream's `docker.yml` creates the
`server-cuda-b<N>` tags in a final job that is skipped as soon as ANY platform leg fails
(on 2026-11-18 and 2026-11-19 the unrelated `musa` leg did). The images are pushed by digest
all the same, and each leg uploads its digests as a run artifact:
`gh run download <run-id> -R ggml-org/llama.cpp -n digests-cuda_cuda12-amd64` lists
`cuda12 amd64 server sha256:…`. Before pinning such a digest, confirm the build from the image
itself — `docker run --rm --entrypoint /app/llama-server <image@digest> --version` and the
label `org.opencontainers.image.version` — because no tag vouches for it.

Test a new image before switching: with the unit stopped, `sh llm-ab.sh <image>` (both files
copied next to each other on the host) runs the installed image and the candidate one after
the other with the production arguments on port 8082 (plain chat, a tool call, decode speed). A long `sha256:` reference typed into the console is fragile — give
the pulled image a short local name first (`docker tag <image@digest> llama-new:b<N>`).

Never `docker restart` / `docker run` the container by hand while the unit is active: the
unit owns it and recreates it with `--rm`.
