"""The interim ticket: single-use, short-lived, address-bound, fail-closed.

🛑 Why these four properties are checked one by one: they are exactly the ones
an interim ticket fails at silently, and none of them shows up in operation.

* **Not single-use** -> an intercepted ticket can be redeemed any number of
  times, so the second factor is overcome once and open permanently after.
* **Not address-bound** -> whoever intercepts the ticket redeems it from
  anywhere.
* **Not fail-closed** -> a Redis outage removes the second factor silently, and
  nobody notices, because the login then simply works.
* **Bound to a forgeable value** -> worse than no binding, because it feigns
  security: the attacker sets the header themselves.
"""
from __future__ import annotations

import json

import pytest

from services import voice_second_factor_store as store

pytestmark = [pytest.mark.unit]


class _FakeRedis:
    """Only the two calls the store makes -- `set(nx=True)` and `getdel`."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.set_calls: list[dict] = []
        self.fail_set = False
        self.fail_getdel = False

    async def set(self, key, value, ex=None, nx=False):
        if self.fail_set:
            raise RuntimeError("Redis weg")
        self.set_calls.append({"key": key, "ex": ex, "nx": nx})
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def getdel(self, key):
        if self.fail_getdel:
            raise RuntimeError("Redis weg")
        return self.data.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(store, "get_redis", lambda: r)
    return r


class TestSingleUse:
    async def test_a_ticket_works_exactly_once(self, redis):
        """🛑 The most important property. A second redemption has to fail, or
        the second factor is overcome permanently after one capture."""
        t = await store.issue_ticket(42, None)
        assert t

        first = await store.consume_ticket(t, None)
        assert first is not None
        assert first.user_id == 42

        second = await store.consume_ticket(t, None)
        assert second is None, "das Ticket war zweimal einlösbar"

    async def test_it_is_written_with_nx_and_a_ttl(self, redis):
        """`nx` prevents overwriting somebody else's ticket, `ex` limits the
        window in which an intercepted ticket is worth anything."""
        await store.issue_ticket(1, None)
        call = redis.set_calls[-1]
        assert call["nx"] is True
        assert call["ex"] == store.DEFAULT_TTL_SECONDS

    async def test_an_unknown_ticket_is_refused(self, redis):
        assert await store.consume_ticket("gibtesnicht", None) is None

    async def test_an_empty_ticket_never_reaches_redis(self, redis):
        assert await store.consume_ticket("", None) is None
        assert redis.set_calls == []

    async def test_tickets_are_unguessable(self, redis):
        a = await store.issue_ticket(1, None)
        b = await store.issue_ticket(1, None)
        assert a != b
        # 256 bits as URL-safe text: well over 40 characters.
        assert len(a) > 40


class TestAddressBinding:
    async def test_a_bound_ticket_is_refused_from_another_address(self, redis):
        t = await store.issue_ticket(7, "10.0.0.1")
        assert await store.consume_ticket(t, "10.0.0.2") is None

    async def test_a_bound_ticket_is_refused_without_an_address(self, redis):
        """No free pass: whoever has no dependable address at redemption time
        does not redeem a bound ticket."""
        t = await store.issue_ticket(7, "10.0.0.1")
        assert await store.consume_ticket(t, None) is None

    async def test_a_bound_ticket_works_from_the_same_address(self, redis):
        t = await store.issue_ticket(7, "10.0.0.1")
        p = await store.consume_ticket(t, "10.0.0.1")
        assert p is not None and p.user_id == 7

    async def test_an_unbound_ticket_ignores_the_address(self, redis):
        """🛑 Without `TRUSTED_PROXIES` there is deliberately NO binding. A
        binding to a forgeable header would be a gain for the attacker, not for
        us: they set it themselves and thereby lock others out."""
        t = await store.issue_ticket(7, None)
        p = await store.consume_ticket(t, "10.0.0.99")
        assert p is not None and p.user_id == 7

    async def test_a_failed_binding_still_consumes_the_ticket(self, redis):
        """After a redemption attempt from the wrong address the ticket is
        GONE. Otherwise an attacker could guess which address is bound any
        number of times, and the ticket would stay lying around for the
        rightful user."""
        t = await store.issue_ticket(7, "10.0.0.1")
        assert await store.consume_ticket(t, "10.0.0.2") is None
        assert await store.consume_ticket(t, "10.0.0.1") is None, (
            "ein Fehlversuch hat das Ticket nicht verbraucht"
        )


class TestFailClosed:
    async def test_no_ticket_when_redis_refuses_the_write(self, redis):
        """🛑 Without a ticket `/auth/login` issues no tokens. A Redis outage
        must not remove the second factor silently -- it has to refuse the
        login."""
        redis.fail_set = True
        assert await store.issue_ticket(1, None) is None

    async def test_no_consumption_when_redis_refuses_the_read(self, redis):
        t = await store.issue_ticket(1, None)
        redis.fail_getdel = True
        assert await store.consume_ticket(t, None) is None

    async def test_a_collision_yields_no_ticket(self, redis, monkeypatch):
        """Practically impossible at 256 bits -- but if it happens, no ticket
        is issued, rather than overwriting somebody else's."""
        monkeypatch.setattr(store.secrets, "token_urlsafe", lambda _n: "immergleich")
        assert await store.issue_ticket(1, None) is not None
        assert await store.issue_ticket(2, None) is None

    async def test_a_corrupt_payload_is_discarded(self, redis):
        t = await store.issue_ticket(1, None)
        redis.data[store._key(t)] = "{kein json"
        assert await store.consume_ticket(t, None) is None

    async def test_a_payload_without_a_user_is_discarded(self, redis):
        t = await store.issue_ticket(1, None)
        redis.data[store._key(t)] = json.dumps({"client_ip": None})
        assert await store.consume_ticket(t, None) is None


class TestNoSecretsInRedis:
    async def test_only_the_identity_is_stored(self, redis):
        """🛑 There is NO token in Redis. `/auth/voice` mints the JWTs fresh
        and re-checks the user while doing so -- otherwise an account
        deactivated in the meantime would get through, and
        `must_change_password` would be stale."""
        t = await store.issue_ticket(5, "10.0.0.1")
        raw = json.loads(redis.data[store._key(t)])
        assert set(raw) == {"user_id", "client_ip"}
        assert raw["user_id"] == 5
