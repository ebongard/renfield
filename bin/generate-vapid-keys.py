#!/usr/bin/env python3
"""Generate a VAPID key pair for Web Push (#79) and print it.

    python bin/generate-vapid-keys.py

Prints the two environment lines for ``PUSH_VAPID_PRIVATE_KEY`` and
``PUSH_VAPID_PUBLIC_KEY``. Nothing is written to disk and nothing is sent
anywhere: put the private key into the instance's secret store yourself (a
Kubernetes Secret, the compose ``.env``) -- never into a ConfigMap or into git.

One pair per instance. Replacing a pair invalidates every existing browser
subscription: the push service rejects sends signed with a different key, the
rows are then removed on the next push and every device has to subscribe again.

Needs only ``cryptography``. Where the host has none, run it inside a backend
container (the script is fed on stdin, nothing is copied):

    docker exec -i renfield-backend python - < bin/generate-vapid-keys.py
"""

from __future__ import annotations

import base64


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate() -> tuple[str, str]:
    """Return ``(private_key, public_key)``, both base64url without padding:
    the raw 32-byte P-256 scalar and the 65-byte uncompressed point."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(private_raw), _b64url(public_raw)


def main() -> None:
    private_key, public_key = generate()
    print("# Web Push VAPID key pair -- the private key is a SECRET.")
    print(f"PUSH_VAPID_PRIVATE_KEY={private_key}")
    print(f"PUSH_VAPID_PUBLIC_KEY={public_key}")


if __name__ == "__main__":
    main()
