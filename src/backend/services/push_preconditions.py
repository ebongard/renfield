"""The preconditions of Web Push (#79) -- in ONE place.

"Push is active" is asked by three callers: the routes (answer as absent when
it is not), the sender (send nothing when it is not) and the feature list the
frontend reads (offer no toggle when it is not). Each of them asking a
different subset is how a hurdle gets raised on one condition and cleared on
five (see ``services/voice_factor_preconditions``), so all three call
:func:`push_blocker`.

The flag alone activates nothing. Push needs:

* ``PUSH_NOTIFICATIONS_ENABLED``;
* ``AUTH_ENABLED`` -- a subscription row belongs to the authenticated user, and
  the recipient rule is defined per user. Without a login there is nobody to
  bind a device to;
* a VAPID key pair from the environment whose two halves belong together. A
  public key that does not match the private one would hand browsers a key the
  push service then refuses every send for -- found here, not in production;
* a contact address for the VAPID ``sub`` claim (Apple refuses sends without).

Nothing here generates or stores a key. ``bin/generate-vapid-keys.py`` prints a
pair for the operator; it reaches the process as a secret in the environment.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache

from loguru import logger

from utils.config import settings

# Machine-readable reasons. They go into the log and the startup warning, never
# to a client: a disabled route answers exactly like a route that does not exist.
FLAG_OFF = "flag_off"
AUTH_OFF = "auth_off"
KEY_MISSING = "vapid_key_missing"
KEY_MALFORMED = "vapid_key_malformed"
KEY_MISMATCH = "vapid_key_mismatch"
CLAIMS_MISSING = "vapid_claims_email_missing"

_PRIVATE_KEY_BYTES = 32
_PUBLIC_KEY_BYTES = 65  # 0x04 || X || Y


def _b64url_decode(value: str) -> bytes | None:
    value = value.strip()
    if not value:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


@lru_cache(maxsize=4)
def _key_pair_blocker(private_key: str, public_key: str) -> str | None:
    """Why this key pair cannot sign a push, or ``None``.

    Cached on the two strings: the check derives a public key from the private
    one, and the routes ask on every request.
    """
    if not private_key.strip() or not public_key.strip():
        return KEY_MISSING
    private_raw = _b64url_decode(private_key)
    public_raw = _b64url_decode(public_key)
    if (
        private_raw is None
        or public_raw is None
        or len(private_raw) != _PRIVATE_KEY_BYTES
        or len(public_raw) != _PUBLIC_KEY_BYTES
        or public_raw[0] != 0x04
    ):
        return KEY_MALFORMED
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        derived = (
            ec.derive_private_key(int.from_bytes(private_raw, "big"), ec.SECP256R1())
            .public_key()
            .public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )
    except ValueError:
        # A scalar outside the curve order.
        return KEY_MALFORMED
    return None if derived == public_raw else KEY_MISMATCH


def vapid_private_key() -> str:
    secret = settings.push_vapid_private_key
    return secret.get_secret_value().strip() if secret is not None else ""


def vapid_public_key() -> str:
    return settings.push_vapid_public_key.strip()


def vapid_subject() -> str:
    """The ``sub`` claim: the configured address as a ``mailto:`` URI."""
    address = settings.push_vapid_claims_email.strip()
    if not address:
        return ""
    return address if address.lower().startswith("mailto:") else f"mailto:{address}"


def push_blocker() -> str | None:
    """What keeps Web Push off on this instance, or ``None`` when it is active."""
    if not settings.push_notifications_enabled:
        return FLAG_OFF
    if not settings.auth_enabled:
        return AUTH_OFF
    key_blocker = _key_pair_blocker(vapid_private_key(), vapid_public_key())
    if key_blocker is not None:
        return key_blocker
    if "@" not in vapid_subject():
        return CLAIMS_MISSING
    return None


def push_active() -> bool:
    return push_blocker() is None


def warn_if_push_misconfigured() -> None:
    """The ONE startup line for "switched on, but not running".

    Flag off is the default and silent. Flag on with a missing precondition
    leaves the feature off -- the routes answer as absent, nothing is sent --
    and without this line nobody would know why.
    """
    blocker = push_blocker()
    if blocker is None or blocker == FLAG_OFF:
        return
    logger.warning(
        f"PUSH_NOTIFICATIONS_ENABLED is set but Web Push stays OFF: {blocker}. "
        "Needs AUTH_ENABLED, a matching PUSH_VAPID_PRIVATE_KEY / PUSH_VAPID_PUBLIC_KEY "
        "pair (bin/generate-vapid-keys.py) and PUSH_VAPID_CLAIMS_EMAIL."
    )
