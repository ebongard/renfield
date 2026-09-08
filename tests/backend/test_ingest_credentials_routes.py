"""Admin CRUD for ingest credentials (#1218 Phase 2).

The security-relevant assertions here are that a token value is returned exactly
ONCE and never by the list route, and that the legacy shared token reports its
SOURCE so the UI can refuse to offer a rotation the boot reconciler would
silently revert.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routes import ingest_credentials as routes
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from services.ingest_credentials import ROUTE_EMAIL, ROUTE_FOLDER

pytestmark = [pytest.mark.backend, pytest.mark.database]


@pytest.fixture
def app(db_session):
    a = FastAPI()
    a.include_router(routes.router, prefix="/api/ingest-credentials")
    a.dependency_overrides[get_db] = lambda: db_session
    a.dependency_overrides[require_permission(Permission.SETTINGS_MANAGE)] = (
        lambda: type("U", (), {"id": 1, "username": "admin"})()
    )
    # slowapi needs request.app.state.limiter present
    from services.api_rate_limiter import limiter
    a.state.limiter = limiter
    return a


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_mint_returns_the_token_once(client):
    r = await client.post("/api/ingest-credentials",
                          json={"client_id": "scanner", "label": "Scanner",
                                "route": ROUTE_FOLDER})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("rfi.scanner.")


async def test_list_never_returns_a_token(client):
    await client.post("/api/ingest-credentials",
                      json={"client_id": "scanner", "label": "S", "route": ROUTE_FOLDER})
    r = await client.get("/api/ingest-credentials")
    assert r.status_code == 200
    body = r.json()
    assert body["credentials"][0]["client_id"] == "scanner"
    # The whole response, not just the obvious field — a token must not leak
    # through any key.
    assert "rfi." not in r.text
    assert "token" not in body["credentials"][0]


async def test_duplicate_client_id_is_409(client):
    p = {"client_id": "scanner", "label": "S", "route": ROUTE_FOLDER}
    assert (await client.post("/api/ingest-credentials", json=p)).status_code == 200
    assert (await client.post("/api/ingest-credentials", json=p)).status_code == 409


async def test_reserved_client_id_is_400(client):
    r = await client.post("/api/ingest-credentials",
                          json={"client_id": "legacy", "label": "x", "route": ROUTE_FOLDER})
    assert r.status_code == 400


async def test_unknown_route_is_400(client):
    r = await client.post("/api/ingest-credentials",
                          json={"client_id": "x", "label": "x", "route": "nope"})
    assert r.status_code == 400


async def test_rotate_returns_a_new_token(client):
    first = (await client.post("/api/ingest-credentials",
             json={"client_id": "scanner", "label": "S",
                   "route": ROUTE_FOLDER})).json()["token"]
    second = (await client.post("/api/ingest-credentials/scanner/rotate")).json()["token"]
    assert second != first and second.startswith("rfi.scanner.")


async def test_rotate_unknown_is_404(client):
    assert (await client.post("/api/ingest-credentials/ghost/rotate")).status_code == 404


async def test_revoke_then_listed_as_disabled(client):
    await client.post("/api/ingest-credentials",
                      json={"client_id": "scanner", "label": "S", "route": ROUTE_FOLDER})
    assert (await client.delete("/api/ingest-credentials/scanner")).status_code == 200
    row = (await client.get("/api/ingest-credentials")).json()["credentials"][0]
    assert row["is_enabled"] is False and row["revoked_at"] is not None


async def test_revoke_unknown_is_404(client):
    assert (await client.delete("/api/ingest-credentials/ghost")).status_code == 404


async def test_legacy_reports_env_source(client, monkeypatch):
    # env-authoritative: the boot reconciler re-seeds the DB from this, so a UI
    # rotation would silently revert at the next restart. The UI keys its
    # read-only rendering off this field.
    monkeypatch.setattr(routes.settings, "folder_ingest_token", "seeded-from-env",
                        raising=False)
    legacy = {x["route"]: x for x in (await client.get("/api/ingest-credentials")).json()["legacy"]}
    assert legacy[ROUTE_FOLDER]["configured"] is True
    assert legacy[ROUTE_FOLDER]["source"] == "env"


async def test_legacy_reports_db_source_when_env_empty(client, monkeypatch):
    monkeypatch.setattr(routes.settings, "folder_ingest_token", "", raising=False)
    monkeypatch.setattr(routes.settings, "email_ingest_token", "", raising=False)
    legacy = {x["route"]: x for x in (await client.get("/api/ingest-credentials")).json()["legacy"]}
    assert legacy[ROUTE_FOLDER]["source"] == "db"
    assert set(legacy) == {ROUTE_FOLDER, ROUTE_EMAIL}


async def test_list_reports_the_feature_flag(client, monkeypatch):
    # The UI must be able to say "these are configured but not yet in effect".
    monkeypatch.setattr(routes.settings, "ingest_credentials_enabled", False, raising=False)
    assert (await client.get("/api/ingest-credentials")).json()["enabled"] is False
