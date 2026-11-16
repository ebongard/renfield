"""An unresolvable endpoint is refused at pairing time, not stored.

🛑 The occasion, found on 2026-10-29: `peer_users.transport_config` held
`https://renfield.loca` -- the name was missing its final `l`, AND it was the
FAR SIDE's name instead of our own. Both came out of the "Erreichbare URL"
field in the pairing dialog, which means the address at which others reach THIS
instance.

The pairing reported success. The failure only showed on the first real use --
days later, as `[Errno -2] Name or service not known` deep inside the
federation client, and even then only because we went looking for it.

The existing sanitisation already discarded empty values on the grounds that
otherwise "a garbage endpoint" would be persisted. A typo is garbage too.
"""
from __future__ import annotations

import socket

import pytest
from fastapi import HTTPException

from api.routes.federation_pairing import _resolve_advertised_endpoints

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_an_unresolvable_host_is_refused():
    """The reported case, verbatim."""
    with pytest.raises(HTTPException) as e:
        await _resolve_advertised_endpoints([{"url": "https://renfield.loca"}])
    assert e.value.status_code == 400
    assert "renfield.loca" in str(e.value.detail)


async def test_the_message_says_WHOSE_address_is_meant():
    """🛑 The message has to name the misreading that produced the error --
    otherwise the person enters the same wrong name again, only spelled
    correctly. The direction is the mistake, not the spelling."""
    with pytest.raises(HTTPException) as e:
        await _resolve_advertised_endpoints([{"url": "https://gibtesnicht.invalid"}])
    text = str(e.value.detail)
    assert "GEGENSEITE" in text and "DIESE" in text
    assert "leer" in text.lower()


async def test_a_resolvable_address_passes():
    """🛑 Negative control: the check must not simply refuse everything.

    An IP literal resolves everywhere without a name server. It used to be
    `http://localhost:8000`; the endpoint hardening refuses that address twice
    over (plain http, and a name for this machine), and rightly so -- what this
    test is about is the RESOLUTION check, so it takes an address the form
    rules accept. 192.0.2.0/24 is the documentation range.
    """
    assert await _resolve_advertised_endpoints(
        [{"url": "https://192.0.2.10:8443"}]) == [{"url": "https://192.0.2.10:8443"}]


async def test_a_resolvable_NAME_passes_and_is_the_name_that_was_asked(monkeypatch):
    """The name path, with the resolver replaced: the test box has no name
    that is guaranteed to resolve and is not a name for the box itself."""
    asked = []

    def resolver(host, *args, **kwargs):
        asked.append(host)
        if host != "instance.example":
            raise OSError("Name or service not known")
        return [(None, None, None, "", ("192.0.2.10", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    assert await _resolve_advertised_endpoints(
        [{"url": "https://instance.example"}]) == [{"url": "https://instance.example"}]
    with pytest.raises(HTTPException):
        await _resolve_advertised_endpoints([{"url": "https://instance.exampl"}])
    assert asked == ["instance.example", "instance.exampl"]


async def test_a_url_without_a_hostname_is_refused():
    with pytest.raises(HTTPException) as e:
        await _resolve_advertised_endpoints([{"url": "keine-url"}])
    assert e.value.status_code == 400


async def test_the_CONFIGURED_default_is_not_checked(monkeypatch):
    """🛑 Only the input from the UI is checked.

    The configured default is the operator's business, goes through a review,
    and may deliberately carry a name this instance does not resolve itself
    (split horizons: the public name points somewhere else from the outside).
    Were the check to hit that too, it would break a valid setup -- and do so at
    pairing time, which is exactly when nobody expects it.
    """
    from api.routes import federation_pairing as fp

    monkeypatch.setattr(fp.settings, "federation_advertised_url",
                        "https://nur-von-aussen.invalid")
    assert await _resolve_advertised_endpoints([]) == [
        {"url": "https://nur-von-aussen.invalid"}]


async def test_an_all_blank_override_still_falls_through(monkeypatch):
    """The existing behaviour stays: empty entries do not count as an
    override and must not trigger the check."""
    from api.routes import federation_pairing as fp

    # Not `https://localhost`: a name for this machine is refused as the
    # configured default too (`federation_endpoint_guard`), and this test is
    # about the fall-through, not about the form rules.
    monkeypatch.setattr(fp.settings, "federation_advertised_url",
                        "https://instance.example")
    assert await _resolve_advertised_endpoints([{"url": ""}, {}]) == [
        {"url": "https://instance.example"}]
