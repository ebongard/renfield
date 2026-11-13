"""
The name an INSTANCE goes by on its peers -- sending, verifying, storing.

A pairing row used to carry one name only: `remote_display_name`, the name of
the PERSON who paired on the far side. When one person owns both instances,
each side therefore showed the other under that person's name -- on the kiosk,
on the peers page, in the audit view and in the tool description the model
reads. This module adds the instance's own name next to it.

The carrier is an ATTACHMENT, not a field of the signed envelope
-------------------------------------------------------------------
Every federation envelope is verified by recomputing a canonical payload from
a FIXED field list (`initiate_canonical_payload`, `complete_canonical_payload`)
or from the parsed model (`offer.model_dump(exclude={"signature"})`). An
unknown field is dropped by the parser on an older version, so:

  - a name INSIDE the signed payload would make an older responder recompute
    different bytes and reject the whole request -- a query to an instance one
    release behind would stop working;
  - a name OUTSIDE the signed payload would be unauthenticated.

So the name travels as two extra top-level fields, `instance_name` and
`instance_name_signature`, which no envelope signature covers. The second
field is an Ed25519 signature by the SAME key over

    {"purpose": NAME_ATTACHMENT_PURPOSE, "instance_name": <name>,
     "pubkey": <signer pubkey hex>, "envelope_signature": <envelope signature hex>}

An older peer ignores both fields and verifies the envelope as before. A newer
peer verifies the envelope first and the attachment second. Binding the
attachment to the envelope's own signature means it cannot be lifted onto
another envelope, and it inherits that envelope's replay defence.

🛑 An attachment that does not verify is DROPPED, and the envelope stands. The
request itself is authenticated; refusing it over a broken name would add a
new way for a query to fail. The far side then simply stays on the fallback
label.

🛑 The name is DISPLAY TEXT. It is never an identifier, never a lookup key and
never part of a tool or server name -- `peer_<id>` and the pubkey stay the
only keys. `remember_remote_instance_name` is the single writer of
`peer_users.remote_instance_name` outside the pairing upsert, and both take
their value from `verify_name_attachment`.
"""
from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from services.federation_identity import FederationIdentity, canonical_bytes
from utils.config import settings

#: Domain separation: the attachment payload can never be mistaken for any
#: other structure this key signs.
NAME_ATTACHMENT_PURPOSE = "renfield.federation.instance_name.v1"

#: Upper bound of a stored name, in characters. Matches the column width.
INSTANCE_NAME_MAX_CHARS = 64

#: The two wire fields of the attachment, on every envelope that carries one.
ATTACHMENT_FIELDS = ("instance_name", "instance_name_signature")

#: Unicode general categories that never belong in a display name: control
#: characters, format characters (zero-width and bidirectional overrides, which
#: can make a label read differently from what it holds), surrogates,
#: private-use and unassigned code points.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def sanitize_instance_name(raw: Any) -> str | None:
    """Reduce a name from the far side (or from our own setting) to display text.

    Drops control and format characters, collapses every whitespace run to one
    space, trims, and caps the length. Letters, digits, punctuation and symbols
    of any script pass unchanged -- "x-idra" and "Küche" come out as they went
    in. Returns `None` for anything that is not a string or is empty afterwards.
    """
    if not isinstance(raw, str):
        return None
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in raw
        if ch.isspace() or unicodedata.category(ch) not in _FORBIDDEN_CATEGORIES
    )
    name = " ".join(kept.split())[:INSTANCE_NAME_MAX_CHARS].strip()
    return name or None


def host_of_url(url: Any) -> str | None:
    """The host part of a URL, or `None` when there is none."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        host = urlparse(url.strip()).hostname
    except ValueError:
        return None
    return sanitize_instance_name(host)


def local_instance_name() -> str | None:
    """The name THIS instance advertises.

    `FEDERATION_INSTANCE_NAME`, else the host of `FEDERATION_ADVERTISED_URL`,
    else nothing. 🛑 Never a person's name: that is the confusion this exists
    to end.
    """
    return (
        sanitize_instance_name(settings.federation_instance_name)
        or host_of_url(settings.federation_advertised_url)
    )


# =============================================================================
# The signed attachment
# =============================================================================


def _attachment_payload(
    name: str, signer_pubkey_hex: str, envelope_signature_hex: str,
) -> dict[str, Any]:
    return {
        "purpose": NAME_ATTACHMENT_PURPOSE,
        "instance_name": name,
        "pubkey": signer_pubkey_hex,
        "envelope_signature": envelope_signature_hex,
    }


def sign_name_attachment(
    identity: FederationIdentity, envelope_signature_hex: str,
) -> dict[str, str]:
    """The two attachment fields for an envelope this instance just signed.

    Empty when this instance has no name to advertise -- the envelope then
    goes out exactly as it did before this module existed.
    """
    name = local_instance_name()
    if name is None:
        return {}
    payload = _attachment_payload(
        name, identity.public_key_hex(), envelope_signature_hex,
    )
    return {
        "instance_name": name,
        "instance_name_signature": identity.sign(canonical_bytes(payload)).hex(),
    }


def verify_name_attachment(
    *,
    name: Any,
    signature_hex: Any,
    signer_pubkey_hex: str,
    envelope_signature_hex: str | None,
) -> str | None:
    """The sanitised name, or `None` when there is none to trust.

    🛑 Call this only AFTER the envelope itself verified against
    `signer_pubkey_hex` and that key was matched to a non-revoked pairing. This
    function proves that the holder of the key vouches for the name on this
    very envelope; it does not prove that the key is one we paired with.

    The signature is checked over the name AS SENT. Sanitising happens after,
    so a name we would alter is still attributable -- and then stored in its
    reduced form.
    """
    if not isinstance(name, str) or not isinstance(signature_hex, str):
        return None
    if not envelope_signature_hex:
        return None
    try:
        pubkey = bytes.fromhex(signer_pubkey_hex)
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return None
    payload = _attachment_payload(name, signer_pubkey_hex, envelope_signature_hex)
    if not FederationIdentity.verify(pubkey, signature, canonical_bytes(payload)):
        logger.warning(
            f"Federation: instance-name attachment from {signer_pubkey_hex[:12]}… "
            "does not verify -- name ignored, envelope stands"
        )
        return None
    return sanitize_instance_name(name)


def wire_body(model: Any) -> dict[str, Any]:
    """An envelope as it goes on the wire.

    The attachment is OMITTED when there is none, rather than sent as two
    `null` keys: what an instance without a name puts on the wire is then
    exactly what this code sent before the attachment existed. Every other
    field is dumped as it always was, `null`s included.
    """
    absent = {f for f in ATTACHMENT_FIELDS if getattr(model, f, None) is None}
    return model.model_dump(exclude=absent)


# =============================================================================
# Reading and storing
# =============================================================================


def stored_instance_name(peer: Any) -> str | None:
    """The VERIFIED name on a pairing row, or `None`."""
    name = getattr(peer, "remote_instance_name", None)
    return name if isinstance(name, str) and name else None


def endpoint_host(peer: Any) -> str | None:
    """Host of the endpoint the far side advertised at pairing time."""
    from services.federation_query_asker import _select_endpoint

    config = getattr(peer, "transport_config", None)
    if not isinstance(config, dict):
        return None
    try:
        return host_of_url(_select_endpoint(peer))
    except Exception:
        return None


def peer_instance_label(peer: Any) -> str | None:
    """What to SHOW as the instance part of a peer's label.

    The verified name; while there is none, the host of the stored endpoint;
    else `None`, and the caller shows the person's name alone as before.
    """
    return stored_instance_name(peer) or endpoint_host(peer)


async def remember_remote_instance_name(peer_id: int, name: str | None) -> bool:
    """Store a VERIFIED name on the pairing. Returns whether the row changed.

    Its own short session and a conditional UPDATE: the callers sit on the
    request path of a query or a ping, and the name changes about once in an
    instance's life -- so the common case writes nothing at all. A revoked
    pairing is never touched. Failures are swallowed: a name that did not get
    stored must not fail the request that carried it.
    """
    if not name:
        return False
    from sqlalchemy import or_, update

    from models.database import PeerUser
    from services.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                update(PeerUser)
                .where(
                    PeerUser.id == peer_id,
                    PeerUser.revoked_at.is_(None),
                    or_(
                        PeerUser.remote_instance_name.is_(None),
                        PeerUser.remote_instance_name != name,
                    ),
                )
                .values(remote_instance_name=name)
            )
            await session.commit()
            changed = bool(result.rowcount)
    except Exception as e:
        logger.warning(f"Federation: instance name for peer {peer_id} not stored: {e}")
        return False
    if changed:
        logger.info(f"🔗 Peer {peer_id} is now known as instance {name!r}")
        await _refresh_tool_description()
    return changed


async def _refresh_tool_description() -> None:
    """The tool description names the instance, so it follows a name change."""
    try:
        from services.peer_mcp_registry import resync_bound_manager

        await resync_bound_manager()
    except Exception as e:
        logger.warning(f"Federation: peer registry resync after a name change failed: {e}")
