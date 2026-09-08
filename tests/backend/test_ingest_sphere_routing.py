"""Server-authoritative sphere routing on client identity (#1218 Phase 4).

The property under test is that the DESTINATION is a function of WHO
authenticated, never of what the request said. A stolen push token must be able
to file a document and nothing more: it cannot choose an owner, raise a tier, or
put the document in another knowledge base.
"""

import pytest

from services import ingest_credentials as ic
from services.folder_ingest import resolve_owner_user_id, resolve_target_kb

pytestmark = [pytest.mark.backend, pytest.mark.database]


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(ic.settings, "ingest_credentials_enabled", True, raising=False)


async def test_client_without_an_override_uses_the_global_default(db_session, monkeypatch):
    # Backward compatibility: this is exactly what every client did before
    # per-integration credentials existed.
    monkeypatch.setattr("services.folder_ingest.settings.folder_ingest_kb_name", "Eingang")
    token = await ic.mint_credential(
        db_session, client_id="files", label="Files", route=ic.ROUTE_FOLDER)
    client = await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    assert client.kb_name is None and client.owner is None and client.tier is None
    kb = await resolve_target_kb(db_session, client.kb_name)
    assert kb.name == "Eingang"


async def test_client_override_routes_to_its_own_kb(db_session, monkeypatch):
    monkeypatch.setattr("services.folder_ingest.settings.folder_ingest_kb_name", "Eingang")
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="Scanner", route=ic.ROUTE_FOLDER,
        kb_name="Scans", tier=2)
    client = await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    assert client.kb_name == "Scans" and client.tier == 2
    kb = await resolve_target_kb(db_session, client.kb_name)
    assert kb.name == "Scans"


async def test_two_clients_route_to_different_spheres(db_session):
    # The whole point: one endpoint, one route, two destinations decided by
    # which credential authenticated.
    a = await ic.mint_credential(db_session, client_id="files", label="F",
                                 route=ic.ROUTE_FOLDER, kb_name="Eingang", tier=0)
    b = await ic.mint_credential(db_session, client_id="scanner", label="S",
                                 route=ic.ROUTE_FOLDER, kb_name="Scans", tier=3)
    ca = await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, a)
    cb = await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, b)
    assert (ca.kb_name, ca.tier) == ("Eingang", 0)
    assert (cb.kb_name, cb.tier) == ("Scans", 3)


async def test_clamp_uses_the_canonical_ladder_bounds(db_session):
    # Not hardcoded 0/4: a literal would silently diverge if the ladder changed.
    from models.database import TIER_PUBLIC, TIER_SELF
    assert ic._clamp_tier(99) == TIER_PUBLIC
    assert ic._clamp_tier(-5) == TIER_SELF


async def test_tier_is_clamped_to_the_circle_ladder(db_session):
    # A row edited outside the API must not express a tier that does not exist.
    await ic.mint_credential(db_session, client_id="scanner", label="S",
                             route=ic.ROUTE_FOLDER, tier=99)
    from sqlalchemy import select
    from models.database import IngestCredential
    row = (await db_session.execute(
        select(IngestCredential).where(IngestCredential.client_id == "scanner")
    )).scalar_one()
    assert row.tier == 4


async def test_sphere_can_be_changed_and_cleared(db_session):
    await ic.mint_credential(db_session, client_id="scanner", label="S",
                             route=ic.ROUTE_FOLDER, kb_name="Scans", tier=2, owner="alice")
    assert await ic.set_client_sphere(
        db_session, "scanner", owner=None, tier=None, kb_name=None) is True
    token = await ic.rotate_credential(db_session, "scanner")
    client = await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    # Clearing must actually clear — an operator removing an owner must remove it.
    assert client.owner is None and client.tier is None and client.kb_name is None


async def test_set_sphere_on_unknown_client_is_false(db_session):
    assert await ic.set_client_sphere(
        db_session, "ghost", owner=None, tier=None, kb_name=None) is False


async def test_legacy_client_has_no_sphere_override(db_session):
    # The shared token has no identity, so it cannot carry a sphere — it must
    # keep falling back to the global configuration.
    async def legacy_ok(db, token):
        return True

    client = await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, "shared", legacy_verify=legacy_ok)
    assert client.legacy is True
    assert client.owner is None and client.tier is None and client.kb_name is None


async def test_owner_falls_back_to_global_when_unset(db_session, monkeypatch):
    monkeypatch.setattr("services.folder_ingest.settings.folder_ingest_target_user", "")
    assert await resolve_owner_user_id(db_session, None) is None


# --- the property that matters: the request cannot choose the destination -----

@pytest.fixture
async def push_client(db_session):
    """The folder-ingest push route, mounted alone against the test session."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from slowapi.errors import RateLimitExceeded
    from unittest.mock import MagicMock

    from api.routes import folder_ingest as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.auth_service import get_current_user
    from services.database import get_db

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(route.router, prefix="/api/folder-ingest")

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id=1, username="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_a_push_cannot_choose_its_own_sphere(db_session, push_client, monkeypatch):
    """A stolen token may file a document; it may NOT decide whose it is.

    The push claims owner/tier/kb in its metadata. The backend must ignore all
    three and use the credential row it authenticated as.
    """
    import json
    from unittest.mock import AsyncMock
    from api.routes import folder_ingest as route
    from services.folder_ingest import IngestResult, IngestStatus

    monkeypatch.setattr(route.settings, "folder_ingest_enabled", True, raising=False)
    monkeypatch.setattr(route, "_worker_is_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(route.settings, "folder_ingest_default_tier", 0, raising=False)

    token = await ic.mint_credential(
        db_session, client_id="scanner", label="Scanner", route=ic.ROUTE_FOLDER,
        kb_name="Scans", tier=1)

    captured = {}

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return IngestResult(IngestStatus.INGESTED, document_id=1)

    monkeypatch.setattr(route, "ingest_document", _capture)

    resp = await push_client.post(
        "/api/folder-ingest/document",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")},
        data={"metadata": json.dumps({
            "filename": "x.pdf",
            # All three are hostile: the client naming its own sphere.
            "owner": "admin", "tier": 4, "kb": "Privat", "kb_name": "Privat",
        })},
    )
    assert resp.status_code == 200, resp.text
    # The tier came from the CREDENTIAL (1), not the request (4).
    assert captured["default_tier"] == 1, "request-supplied tier was honoured"
