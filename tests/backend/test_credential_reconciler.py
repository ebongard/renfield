"""Boot credential-reconciler — self-heals DB ingest tokens from the Secret source.

Regression guard for the 2026-07 xidra reset incident: a DB wipe cleared the
folder/email-ingest tokens in SystemSetting while the MCP's copy survived, so
pushes 403'd until re-synced by hand. The reconciler re-seeds the DB token from
``settings.*_ingest_token`` at boot.
"""
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import services.credential_reconciler as cr
from models.database import (
    SETTING_EMAIL_INGEST_TOKEN,
    SETTING_FOLDER_INGEST_TOKEN,
    SystemSetting,
)
from services.ingest_common import get_ingest_token


@pytest.fixture(autouse=True)
def _use_test_session(db_session: AsyncSession, monkeypatch):
    """Point the reconciler's AsyncSessionLocal at the test session."""
    @asynccontextmanager
    async def _factory():
        yield db_session

    monkeypatch.setattr(cr, "AsyncSessionLocal", _factory)


@pytest.mark.integration
async def test_seeds_token_when_db_empty(db_session, monkeypatch):
    """Fresh DB (no SystemSetting row) + authoritative env token → seed it."""
    monkeypatch.setattr(cr.settings, "folder_ingest_token", "authoritative-abc")
    monkeypatch.setattr(cr.settings, "email_ingest_token", "")
    actions = await cr.reconcile_credentials()
    assert await get_ingest_token(db_session, SETTING_FOLDER_INGEST_TOKEN) == "authoritative-abc"
    assert any("folder-ingest" in a and "seeded" in a for a in actions)


@pytest.mark.integration
async def test_heals_diverged_token(db_session, monkeypatch):
    """DB has a STALE token (the wipe/divergence case) → overwrite with authoritative."""
    db_session.add(SystemSetting(key=SETTING_FOLDER_INGEST_TOKEN, value="stale-old"))
    await db_session.commit()
    monkeypatch.setattr(cr.settings, "folder_ingest_token", "authoritative-new")
    monkeypatch.setattr(cr.settings, "email_ingest_token", "")
    actions = await cr.reconcile_credentials()
    assert await get_ingest_token(db_session, SETTING_FOLDER_INGEST_TOKEN) == "authoritative-new"
    assert any("healed" in a for a in actions)


@pytest.mark.integration
async def test_noop_when_already_matching(db_session, monkeypatch):
    """DB token already equals the authoritative value → no action, no churn."""
    db_session.add(SystemSetting(key=SETTING_FOLDER_INGEST_TOKEN, value="same"))
    await db_session.commit()
    monkeypatch.setattr(cr.settings, "folder_ingest_token", "same")
    monkeypatch.setattr(cr.settings, "email_ingest_token", "")
    actions = await cr.reconcile_credentials()
    assert actions == []


@pytest.mark.integration
async def test_noop_when_env_unset_legacy(db_session, monkeypatch):
    """No authoritative env token (legacy DB-authoritative install) → never touch
    the DB, even if a token is present."""
    db_session.add(SystemSetting(key=SETTING_FOLDER_INGEST_TOKEN, value="admin-generated"))
    await db_session.commit()
    monkeypatch.setattr(cr.settings, "folder_ingest_token", "")
    monkeypatch.setattr(cr.settings, "email_ingest_token", "")
    actions = await cr.reconcile_credentials()
    assert actions == []
    assert await get_ingest_token(db_session, SETTING_FOLDER_INGEST_TOKEN) == "admin-generated"


@pytest.mark.integration
async def test_both_tokens_independent(db_session, monkeypatch):
    """folder + email tokens are seeded independently."""
    monkeypatch.setattr(cr.settings, "folder_ingest_token", "folder-tok")
    monkeypatch.setattr(cr.settings, "email_ingest_token", "email-tok")
    await cr.reconcile_credentials()
    assert await get_ingest_token(db_session, SETTING_FOLDER_INGEST_TOKEN) == "folder-tok"
    assert await get_ingest_token(db_session, SETTING_EMAIL_INGEST_TOKEN) == "email-tok"
