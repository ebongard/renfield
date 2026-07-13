"""
Federation identity — the Ed25519 keypair every Renfield uses to sign
federated queries and pairing handshakes.

Key management:
  - Private key lives at `secrets/federation_identity_key` (32 raw bytes).
  - File is created lazily on first use, 0600 perms.
  - Public key is derived at load time and exposed via `public_key_hex()`.
  - The keypair NEVER leaves this host. Peers identify us by the public
    key hex; we identify peers by the public key they present at pairing
    time, stored in `peer_users.remote_pubkey`.

The pairing protocol (services/pairing_service.py) and the query-time
signature check (F3.query_brain) both call `sign()` / `verify()` here —
having a single code path means key loading + PEM/DER edge cases live
in one file, not sprinkled across every caller.

Why Ed25519 vs RSA/ECDSA:
  - 32-byte keys, 64-byte signatures — compact over the wire and in QR
    codes (critical for F4 pairing UX).
  - Deterministic signing (no nonce required) reduces protocol footguns.
  - `cryptography.hazmat.primitives.asymmetric.ed25519` is already
    available via `python-jose[cryptography]` — no new dependency.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from loguru import logger


# Path is overridable for tests via `init_federation_identity(path=...)` and in
# production via the `federation_identity_key_path` setting (so an operator can
# point it at a PERSISTED secret mount — the default /app/secrets is ephemeral
# container storage, so the key would otherwise regenerate on every restart and
# break every pairing on the next deploy).
_DEFAULT_KEY_PATH = Path("/app/secrets/federation_identity_key")


class FederationIdentity:
    """
    Owns this Renfield's Ed25519 keypair. Module-level singleton access
    via `get_federation_identity()` so every caller shares the same
    loaded key (loading from disk on every request would be wasteful).

    The class is thread-safe for `sign()` / `verify()` — those ops have
    no shared mutable state in `cryptography`'s Ed25519 impl.
    """

    def __init__(self, private_key: ed25519.Ed25519PrivateKey):
        self._private_key = private_key
        self._public_key = private_key.public_key()

    def sign(self, message: bytes) -> bytes:
        """Sign `message` with our private key. Returns a 64-byte signature."""
        return self._private_key.sign(message)

    def public_key_bytes(self) -> bytes:
        """Raw 32-byte public key. Used for wire serialization + QR."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )
        return self._public_key.public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )

    def public_key_hex(self) -> str:
        """Hex-encoded public key — canonical form for display + DB storage."""
        return self.public_key_bytes().hex()

    @staticmethod
    def verify(pubkey_bytes: bytes, signature: bytes, message: bytes) -> bool:
        """
        Verify `signature` over `message` using `pubkey_bytes` (32 raw bytes).

        Returns True iff the signature is valid. Never raises — every failure
        mode (bad key length, bad signature bytes, verification mismatch)
        resolves to False so callers can treat signature failures uniformly.
        """
        try:
            pubkey = ed25519.Ed25519PublicKey.from_public_bytes(pubkey_bytes)
            pubkey.verify(signature, message)
            return True
        except Exception:
            return False


_instance_lock = threading.Lock()
_instance: FederationIdentity | None = None
# None = not explicitly initialized → resolve from settings at load time (below).
# A test (or explicit caller) may pin an exact path via init_federation_identity.
_configured_path: Path | None = None
# Set by _load_or_generate the first (and only) time it runs per process:
# True  = the key ALREADY existed at boot → persisted (a mounted secret/PVC).
# False = we generated a fresh key at boot → EPHEMERAL (regenerates each restart).
# None  = identity not loaded yet. The boot guard reads this after first load.
_loaded_from_disk: bool | None = None


def _resolve_key_path() -> Path:
    """The key file to load/create: an explicit init() path wins (tests), else
    the `federation_identity_key_path` setting, else the hardcoded default."""
    if _configured_path is not None:
        return _configured_path
    try:
        from utils.config import settings
        return Path(settings.federation_identity_key_path or _DEFAULT_KEY_PATH)
    except Exception:  # noqa: BLE001 — config import must never break identity load
        return _DEFAULT_KEY_PATH


def init_federation_identity(path: Path | str | None = None) -> None:
    """
    Override the key-file path (tests set a tmp_path here) BEFORE the
    first `get_federation_identity()` call. Re-initialising after load
    is an error — the loaded key would be stale and signatures could
    fail unpredictably.
    """
    global _configured_path, _instance
    if _instance is not None:
        raise RuntimeError(
            "init_federation_identity: singleton already loaded; "
            "call before first get_federation_identity()."
        )
    # None → clear any explicit pin so the path resolves from settings.
    _configured_path = None if path is None else Path(path)


def reset_federation_identity_for_tests() -> None:
    """
    Test-only helper — clears the singleton so the next
    `init_federation_identity` call succeeds. Never call from
    production code.
    """
    global _instance, _loaded_from_disk
    _instance = None
    _loaded_from_disk = None


def get_federation_identity() -> FederationIdentity:
    """
    Load-or-create the singleton. Creates the key file (0600) on first
    call; subsequent calls return the cached instance.
    """
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        _instance = _load_or_generate(_resolve_key_path())
        return _instance


def enforce_persistent_identity() -> None:
    """Boot guard: if `federation_require_persistent_identity` is set, FAIL fast
    unless the federation key was loaded from a persisted path (not freshly
    generated this boot). Ephemeral keys regenerate on every restart → the pubkey
    changes → every existing pairing breaks on the next deploy, silently. A hard
    fail at boot beats a WARNING nobody reads surfacing as broken pairings a
    deploy later. Call once at startup (after config load). No-op when the setting
    is off (default) — legacy/non-federating instances are unaffected.
    """
    from utils.config import settings
    if not getattr(settings, "federation_require_persistent_identity", False):
        return
    get_federation_identity()  # force the one-time load so _loaded_from_disk is set
    if _loaded_from_disk is not True:
        raise RuntimeError(
            "federation_require_persistent_identity is set but the federation "
            f"identity key at {_resolve_key_path()} was GENERATED at boot, not "
            "loaded from a persisted mount — it will regenerate on the next "
            "restart and break every pairing. Provision the persisted key "
            "(bin/provision_federation_identity.py) or unset the requirement."
        )
    logger.info("🔑 Federation identity verified persistent (boot guard passed)")


def _load_or_generate(path: Path) -> FederationIdentity:
    """
    Read the 32-byte private key from `path`, or generate one and
    write it with 0600 perms. Never logs the key material.

    Race-safe across processes: in-process callers serialize via
    `_instance_lock`, but two backend workers (gunicorn first-boot,
    parallel pytest-xdist workers, etc.) could both race through the
    `path.exists()` check. O_CREAT|O_EXCL picks one winner; the
    loser catches FileExistsError and re-reads the file the winner
    just wrote.
    """
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    global _loaded_from_disk

    path.parent.mkdir(parents=True, exist_ok=True)

    def _load_existing() -> FederationIdentity:
        raw = path.read_bytes()
        if len(raw) != 32:
            raise ValueError(
                f"federation_identity: {path} exists but is {len(raw)} bytes, "
                f"expected 32 (Ed25519 private key). A common cause is a trailing "
                f"newline from `kubectl create secret --from-file` — the key file "
                f"must be EXACTLY 32 raw bytes (use bin/provision_federation_identity.py)."
            )
        return FederationIdentity(ed25519.Ed25519PrivateKey.from_private_bytes(raw))

    if path.exists():
        identity = _load_existing()
        _loaded_from_disk = True  # key was ALREADY on disk at boot → persisted
        logger.info(f"🔑 Federation identity loaded from {path}")
        return identity

    # No key at this path → generate one. On a persisted mount this branch is
    # never taken (the secret file exists); taking it means the key is EPHEMERAL
    # (regenerates every restart) — the boot guard flags this when federation
    # requires a stable identity.
    _loaded_from_disk = False
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another process raced us to the create. Their key is now on
        # disk; read it (they had no way to tell us which pubkey they
        # wrote, so our in-memory `private_key` is discarded).
        logger.info(
            f"🔑 Federation identity: lost create race at {path}; "
            f"loading the winner's key"
        )
        return _load_existing()
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    logger.info(
        f"🔑 Federation identity generated at {path} "
        f"(pubkey={private_key.public_key().public_bytes_raw().hex()[:12]}...)"
    )
    return FederationIdentity(private_key)
