"""Under pytest-xdist every worker runs on a database of its own (#1577).

`bin/throwaway_test_db.py --workers N` creates `<base>_gw0 .. <base>_gw<N-1>`
and sets RENFIELD_TEST_DB_PER_WORKER=1; the harness swaps the worker id into
RENFIELD_TEST_PG_URL at import. Two workers on one database would drop each
other's schema, so a worker without that promise stops.
"""

from types import SimpleNamespace

import pytest

from tests.backend import conftest as harness

pytestmark = pytest.mark.unit

_BASE = "postgresql+asyncpg://app:PASSWORD@db:5432/renfield_test_a1"


@pytest.fixture
def env(monkeypatch):
    for var in ("PYTEST_XDIST_WORKER", "RENFIELD_TEST_DB_PER_WORKER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RENFIELD_TEST_PG_URL", _BASE)
    return monkeypatch


def _url() -> str:
    import os

    return os.environ["RENFIELD_TEST_PG_URL"]


def test_without_xdist_the_url_is_left_alone(env):
    harness._point_this_xdist_worker_at_its_own_database()
    assert _url() == _BASE


def test_a_worker_gets_the_database_named_after_it(env):
    env.setenv("PYTEST_XDIST_WORKER", "gw3")
    env.setenv("RENFIELD_TEST_DB_PER_WORKER", "1")
    harness._point_this_xdist_worker_at_its_own_database()
    assert _url() == _BASE + "_gw3"
    # The password survives the rewrite: the worker has to log in with it.
    assert ":PASSWORD@" in _url()


def test_a_second_import_does_not_swap_the_id_in_twice(env):
    # conftest is imported again as `tests.backend.conftest`.
    env.setenv("PYTEST_XDIST_WORKER", "gw1")
    env.setenv("RENFIELD_TEST_DB_PER_WORKER", "1")
    harness._point_this_xdist_worker_at_its_own_database()
    harness._point_this_xdist_worker_at_its_own_database()
    assert _url() == _BASE + "_gw1"


def test_a_worker_without_a_database_of_its_own_stops(env):
    env.setenv("PYTEST_XDIST_WORKER", "gw0")
    with pytest.raises(pytest.exit.Exception, match="a database per worker"):
        harness._point_this_xdist_worker_at_its_own_database()
    assert _url() == _BASE


@pytest.mark.parametrize("worker", ["gw", "gw100", "x0", "gw0/../x"])
def test_an_unexpected_worker_id_stops(env, worker):
    env.setenv("PYTEST_XDIST_WORKER", worker)
    env.setenv("RENFIELD_TEST_DB_PER_WORKER", "1")
    with pytest.raises(pytest.exit.Exception, match="unexpected xdist worker id"):
        harness._point_this_xdist_worker_at_its_own_database()
    assert _url() == _BASE


def test_the_cleanup_counts_of_the_workers_reach_the_controller(monkeypatch):
    # Without this the controller prints nothing: its own counts stay zero.
    counts = {"delete": 0, "truncate_fallback": 0}
    monkeypatch.setattr(harness, "_CLEANUP_COUNTS", counts)

    worker_counts = {"delete": 7, "truncate_fallback": 1}
    monkeypatch.setattr(harness, "_CLEANUP_COUNTS", dict(worker_counts))
    config = SimpleNamespace(workeroutput={})
    harness.pytest_sessionfinish(SimpleNamespace(config=config))
    assert config.workeroutput["renfield_cleanup_counts"] == worker_counts

    monkeypatch.setattr(harness, "_CLEANUP_COUNTS", counts)
    for _ in range(2):
        harness.pytest_testnodedown(SimpleNamespace(workeroutput=config.workeroutput), None)
    assert counts == {"delete": 14, "truncate_fallback": 2}


def test_a_run_without_xdist_hands_nothing_on(monkeypatch):
    config = SimpleNamespace()
    harness.pytest_sessionfinish(SimpleNamespace(config=config))
    assert not hasattr(config, "workeroutput")
