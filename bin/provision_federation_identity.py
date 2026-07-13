#!/usr/bin/env python3
"""Provision the persisted federation identity secret for a Renfield instance.

The federation Ed25519 identity key MUST live on persistent storage or it
regenerates on every pod restart — the pubkey changes and every existing peer
pairing breaks on the next deploy. This script generates the key and stores it in
the `federation-identity` k8s Secret that `k8s/backend.yaml` mounts at
`/app/secrets/federation_identity_key`.

Key format: EXACTLY 32 raw bytes — the loader (`services/federation_identity.py`
`_load_existing`) hard-rejects any other length. We base64 the 32 bytes straight
into the Secret's `data`, so there is no trailing-newline footgun (the classic
`kubectl create secret --from-file=<file-with-newline>` = 33 bytes = boot failure).

Rotation-safe + idempotent: refuses to overwrite an existing key (rotating it
re-pairs everyone). `--force` rotates behind a loud warning. `--verify` round-trips
the stored key and prints the pubkey.

Prereqs: `kubectl` on PATH with cluster access + the `cryptography` package.

Examples:
    bin/provision_federation_identity.py -n renfield
    bin/provision_federation_identity.py -n renfield-xidra --verify
    bin/provision_federation_identity.py -n renfield --force   # ROTATE (breaks pairings)
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

SECRET_NAME = "federation-identity"
KEY_FIELD = "federation_identity_key"


def _kubectl(context: str, namespace: str, args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", "--context", context, "-n", namespace, *args],
        text=True, capture_output=True, **kw,
    )


def _pubkey_hex(raw: bytes) -> str:
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()


def _verify(context: str, namespace: str) -> None:
    r = _kubectl(context, namespace, ["get", "secret", SECRET_NAME, "-o", f"jsonpath={{.data.{KEY_FIELD}}}"])
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit(f"✗ secret {namespace}/{SECRET_NAME} not found (or has no {KEY_FIELD})")
    raw = base64.b64decode(r.stdout.strip())
    if len(raw) != 32:
        sys.exit(f"✗ {namespace}/{SECRET_NAME}: key is {len(raw)} bytes, expected 32 — federation would FAIL to boot")
    print(f"✓ {namespace}/{SECRET_NAME}: valid 32-byte key, pubkey={_pubkey_hex(raw)}")


def _provision(context: str, namespace: str, force: bool) -> None:
    if force:
        print(
            f"⚠  --force: ROTATING {namespace}/{SECRET_NAME} (if it exists).\n"
            f"   This changes this instance's federation pubkey and BREAKS EVERY\n"
            f"   EXISTING PAIRING. You must re-pair afterwards. Continuing..."
        )

    raw = ed25519.Ed25519PrivateKey.generate().private_bytes(
        encoding=Encoding.Raw, format=PrivateFormat.Raw, encryption_algorithm=NoEncryption(),
    )
    assert len(raw) == 32, "Ed25519 raw private key must be 32 bytes"

    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": SECRET_NAME, "namespace": namespace},
        "type": "Opaque",
        "data": {KEY_FIELD: base64.b64encode(raw).decode("ascii")},
    }
    # `create` (not `apply`) is the rotation guard: it FAILS with AlreadyExists
    # rather than silently upserting, so a flaky/ambiguous `get` can never lead to
    # overwriting a live key. --force does an explicit delete-then-create.
    if force:
        _kubectl(context, namespace, ["delete", "secret", SECRET_NAME, "--ignore-not-found"])
    r = _kubectl(context, namespace, ["create", "-f", "-"], input=json.dumps(manifest))
    if r.returncode != 0:
        if "AlreadyExists" in (r.stderr or ""):
            sys.exit(
                f"✓ {namespace}/{SECRET_NAME} already exists — refusing to overwrite.\n"
                f"  (Rotating changes the pubkey and BREAKS every existing pairing.\n"
                f"   Use --force to rotate deliberately, or --verify to inspect.)"
            )
        sys.exit(f"✗ kubectl create failed: {r.stderr.strip()}")
    print(
        f"✓ provisioned {namespace}/{SECRET_NAME} (pubkey={_pubkey_hex(raw)}).\n"
        f"  Roll the backend to load it: kubectl -n {namespace} rollout restart deploy/backend"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", "-n", required=True, help="target k8s namespace (e.g. renfield, renfield-xidra)")
    ap.add_argument("--context", default="renfield-private", help="kubectl context (default: renfield-private)")
    ap.add_argument("--force", action="store_true", help="ROTATE an existing key — breaks all pairings for this instance")
    ap.add_argument("--verify", action="store_true", help="check the stored key is a valid 32-byte key and print its pubkey")
    args = ap.parse_args()

    if args.verify:
        _verify(args.context, args.namespace)
    else:
        _provision(args.context, args.namespace, args.force)


if __name__ == "__main__":
    main()
