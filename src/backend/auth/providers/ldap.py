"""LDAP credential provider — authn only.

Ported from Reva's proven ``ldap_auth.py`` (RFC 4515 filter escaping,
service-account search + user-bind verify, connect/receive timeouts).
Transport (#1345): ``ldaps://`` only — a plain ``ldap://`` URL is refused,
there is no StartTLS — AND the server certificate is verified (chain against
``LDAP_CA_CERT_FILE`` or the system store, plus hostname); TLS without
verification would let a man-in-the-middle accept any password. What is intentionally NOT ported: local Renfield
user creation/update. Under the registry, identity resolution
(create-or-link a canonical user) is the ``post_authenticate`` consumer's job
(Reva). This provider only proves "these credentials are valid for this
directory subject" and emits a ``ProviderResult``.

Fail semantics (so the registry's fail-open policy is correct):
  * wrong password / user not found  → return ``None`` (decline, continue walk)
  * directory unreachable / timeout  → **raise** (registry skips + increments
    ``auth_provider_unreachable_total{ldap}`` + continues the walk)
  * ldap3 missing / unconfigured     → log + return ``None`` (misconfig is not
    an outage; do not pollute the unreachable counter)

Authz seam (defined, NOT implemented here): ``memberOf`` is serialized into
``extras["ldap_member_of"]`` as a JSON string per the contract convention. No
group→role mapping happens in this delivery — a future authz-provider layer
deserializes it. See provider_contract.py.
"""

from __future__ import annotations

import json
import re
import ssl
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from auth.provider_contract import ProviderResult
from auth.providers.base import make_result
from utils.config import settings

_LDAP_ESCAPE_RE = re.compile(r"([\\*\(\)\x00/])")


def _escape_ldap_filter(value: str) -> str:
    """Escape special characters in an LDAP filter value per RFC 4515."""
    return _LDAP_ESCAPE_RE.sub(lambda m: f"\\{ord(m.group(1)):02x}", value)


def _attr(attrs: dict[str, Any], key: str, default: str = "") -> str:
    val = attrs.get(key, default)
    if isinstance(val, list):
        return str(val[0]) if val else default
    return str(val) if val else default


def _verifying_tls(ldap3: Any, url: str) -> Any:
    """TLS settings that VERIFY the directory's certificate (#1345).

    ldap3's default ``Tls()`` is ``validate=CERT_NONE``: no chain check, no
    hostname check — anyone on the path could answer both binds with success,
    and with ``DB_AUTH_ENABLED=false`` the directory is the sole authority.
    ``CERT_REQUIRED`` makes ldap3 (2.9.1) verify the chain against
    ``LDAP_CA_CERT_FILE`` (or the system CA store) and then match the
    certificate against the URL's host after the handshake (``check_hostname``
    runs whenever validate is REQUIRED/OPTIONAL). SNI is sent for that host.
    """
    return ldap3.Tls(
        validate=ssl.CERT_REQUIRED,
        version=ssl.PROTOCOL_TLS_CLIENT,
        ca_certs_file=settings.ldap_ca_cert_file.strip() or None,
        sni=urlsplit(url.strip()).hostname,
    )


class LDAPProvider:
    """Directory bind verification. Priority 50 → walked before DB."""

    provider_id = "ldap"
    supported_channels = frozenset({"web"})
    priority = 50

    @property
    def enabled(self) -> bool:
        # Config-driven so enabling is a config-only change (no redeploy) and
        # tests can flip it via settings.
        return settings.ldap_auth_enabled

    async def authenticate(
        self, *, username: str, password: str, channel: str
    ) -> ProviderResult | None:
        if not settings.ldap_auth_enabled:
            return None
        if not username or not password:
            return None

        try:
            import ldap3
            from ldap3 import SAFE_SYNC, Connection, Server
        except ImportError:
            logger.error("ldap3 not installed — LDAP auth unavailable")
            return None

        user_base_dn = settings.ldap_auth_user_base_dn
        if not user_base_dn or not settings.ldap_url:
            logger.warning(
                "LDAP auth enabled but LDAP_URL / LDAP_AUTH_USER_BASE_DN "
                "not configured — declining"
            )
            return None
        if not settings.ldap_url.strip().lower().startswith("ldaps://"):
            # The config validator refuses this at boot; this guard covers a
            # settings object mutated at runtime. No StartTLS → a plain ldap://
            # bind would send both passwords in clear text (#1345).
            logger.error(
                "LDAP auth: LDAP_URL is not ldaps:// — refusing to send "
                "credentials unencrypted, declining"
            )
            return None

        safe_username = _escape_ldap_filter(username)
        user_filter = settings.ldap_auth_user_filter.format(
            username=safe_username
        )

        server = Server(
            settings.ldap_url,
            use_ssl=True,
            tls=_verifying_tls(ldap3, settings.ldap_url),
            connect_timeout=settings.ldap_connect_timeout,
        )

        # OpenLDAP (entryUUID/uid) vs AD (objectGUID/sAMAccountName) split the
        # subject + username attrs. Strict OpenLDAP rejects the WHOLE search
        # with LDAPAttributeError "invalid attribute type objectGUID" when an
        # unknown attr is in the explicit list — fail-open then swallows it
        # and the walk falls through to db, returning 401. Try the AD-aware
        # superset first; on LDAPAttributeError, retry without the AD-only
        # names. entryUUID/uid alone still gives a stable subject + username
        # for OpenLDAP; AD continues to get objectGUID/sAMAccountName.
        _attrs_full = [
            "entryUUID", "objectGUID", "displayName", "mail", "memberOf",
            "uid", "cn", "sn", "givenName", "sAMAccountName",
        ]
        _attrs_openldap = [
            a for a in _attrs_full if a not in ("objectGUID", "sAMAccountName")
        ]
        try:
            # Step 1: service-account bind → locate the user entry.
            with Connection(
                server,
                user=settings.ldap_bind_dn,
                password=settings.ldap_bind_password.get_secret_value(),
                auto_bind=True,
                receive_timeout=settings.ldap_receive_timeout,
                client_strategy=SAFE_SYNC,
            ) as conn:
                # SAFE_SYNC → entries live in `response`, not conn.entries.
                try:
                    _ok, _result, response, _request = conn.search(
                        search_base=user_base_dn,
                        search_filter=user_filter,
                        attributes=_attrs_full,
                    )
                except ldap3.core.exceptions.LDAPAttributeError:
                    logger.debug(
                        "LDAP auth: directory rejected AD-only attrs — "
                        "retrying as OpenLDAP (entryUUID/uid only)"
                    )
                    _ok, _result, response, _request = conn.search(
                        search_base=user_base_dn,
                        search_filter=user_filter,
                        attributes=_attrs_openldap,
                    )
                entries = [
                    r
                    for r in (response or [])
                    if r.get("type") == "searchResEntry"
                ]
                if not entries:
                    logger.debug(
                        f"LDAP auth: user {username!r} not found in "
                        f"{user_base_dn}"
                    )
                    return None
                user_dn = entries[0]["dn"]
                attrs = entries[0].get("attributes", {})

            # Step 2: user-bind → verify the supplied password.
            with Connection(
                server,
                user=user_dn,
                password=password,
                auto_bind=True,
                receive_timeout=settings.ldap_receive_timeout,
                client_strategy=SAFE_SYNC,
            ):
                pass  # bind ok ⇒ credentials valid

        except ldap3.core.exceptions.LDAPBindError:
            logger.debug(
                f"LDAP auth: bind failed for {username!r} (wrong password "
                f"or not found)"
            )
            return None
        except ldap3.core.exceptions.LDAPSocketOpenError as e:
            # Directory unreachable → let the registry treat this as
            # provider-unreachable (fail-open skip + counter + continue walk).
            logger.error(f"LDAP auth: server unreachable at {settings.ldap_url}: {e}")
            raise
        except ldap3.core.exceptions.LDAPException as e:
            logger.error(f"LDAP auth: directory error for {username!r}: {e}")
            raise

        # Step 3: build the contract result. subject = strong directory id.
        subject = (
            _attr(attrs, "entryUUID")
            or _attr(attrs, "objectGUID")
            or user_dn  # stable-enough fallback; logged below if used
        )
        if subject == user_dn:
            logger.warning(
                f"LDAP auth: no entryUUID/objectGUID for {username!r}; "
                f"falling back to DN as subject (less stable across moves)"
            )

        display_name = (
            _attr(attrs, "displayName")
            or _attr(attrs, "cn")
            or _attr(attrs, "uid")
            or username
        )
        mail = _attr(attrs, "mail") or None
        member_of = attrs.get("memberOf") or []
        if isinstance(member_of, str):
            member_of = [member_of]

        logger.info(
            f"LDAP auth: {username!r} authenticated as {display_name!r} "
            f"(subject={subject})"
        )
        return make_result(
            provider_id=self.provider_id,
            subject=str(subject),
            display_name=display_name,
            channel=channel,
            email=mail,
            # Directory mail is organization-asserted ⇒ treat as verified so
            # the cross-provider email join may use it.
            email_verified=mail is not None,
            extras={"ldap_member_of": json.dumps([str(m) for m in member_of])},
        )
