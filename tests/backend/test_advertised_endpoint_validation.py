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


async def test_a_resolvable_host_passes():
    """🛑 Negative control: the check must not simply refuse everything.
    `localhost` resolves everywhere."""
    assert await _resolve_advertised_endpoints(
        [{"url": "http://localhost:8000"}]) == [{"url": "http://localhost:8000"}]


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

    monkeypatch.setattr(fp.settings, "federation_advertised_url",
                        "https://localhost")
    assert await _resolve_advertised_endpoints([{"url": ""}, {}]) == [
        {"url": "https://localhost"}]
