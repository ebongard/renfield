"""Security tests for per-document access control (audit HIGH-1 / HIGH-3).

`check_document_access` is the authoritative per-document ACL that the knowledge
routes (get / delete / reindex / batch / in-doc-search) and the `/documents/move`
source check now route through. It closes two IDOR holes the old
``if doc.knowledge_base_id:``-gated inline checks left open:

- a document with ``knowledge_base_id IS NULL`` (KB-less / orphaned) skipped the
  ACL entirely → any authenticated user could read/delete/reindex it;
- a set-but-orphaned KB id (row missing) fell through the ``if kb and …`` guard
  and was likewise granted.

These tests assert the owner-based fallback + fail-closed behavior.
"""
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import api.routes.knowledge as knowledge_mod
from api.routes.knowledge import check_document_access
from models.database import Document, KnowledgeBase, Role, User
from models.permissions import Permission
from services.atom_service import AtomService


async def _mk_role(db, name, perms) -> Role:
    role = Role(name=name, permissions=perms, is_system=False)
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


async def _mk_user(db, username, role) -> User:
    user = User(username=username, password_hash="x", role_id=role.id, is_active=True)
    db.add(user)
    await db.commit()
    await db.refresh(user, ["role"])
    return user


async def _mk_kb(db, name, owner_id=None) -> KnowledgeBase:
    kb = KnowledgeBase(name=name, description=name, is_active=True, owner_id=owner_id)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _mk_doc(db, *, owner_user_id=None, kb_id=None, tier=0) -> Document:
    """Create a document the way the real ingest path does: ownership lives on a
    linked ``kb_document`` atom (``Document.atom_id`` → ``atoms.owner_user_id``),
    created via AtomService — NOT a column on ``documents``."""
    atom_svc = None
    atom_id = None
    if owner_user_id is not None:
        atom_svc = AtomService(db)
        atom_id = await atom_svc.create_with_source(
            atom_type="kb_document", owner_user_id=owner_user_id, tier=tier
        )
    doc = Document(
        # Unique file_hash: (file_hash, knowledge_base_id) is UNIQUE with
        # NULLS NOT DISTINCT on Postgres, so two KB-less (NULL kb) docs with a
        # NULL hash would collide.
        file_hash=uuid.uuid4().hex,
        filename="d.pdf",
        title="D",
        file_path="/tmp/d.pdf",
        file_type="pdf",
        file_size=1,
        status="completed",
        chunk_count=1,
        knowledge_base_id=kb_id,
        atom_id=atom_id,
        circle_tier=tier,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    if atom_id is not None:
        await atom_svc.finalize_source_id(atom_id, doc.id)
        await db.commit()
    return doc


@pytest.fixture
def _auth_on(monkeypatch):
    monkeypatch.setattr(knowledge_mod.settings, "auth_enabled", True, raising=False)


@pytest.mark.database
class TestCheckDocumentAccess:
    async def test_auth_off_allows_everyone(self, db_session: AsyncSession, monkeypatch):
        monkeypatch.setattr(knowledge_mod.settings, "auth_enabled", False, raising=False)
        role = await _mk_role(db_session, "r", ["kb.own"])
        a = await _mk_user(db_session, "a", role)
        doc = await _mk_doc(db_session, owner_user_id=a.id)
        # A different (None) user still passes when auth is disabled.
        assert await check_document_access(doc, None, "read", db_session) is True

    async def test_owner_reaches_kbless_doc(self, db_session: AsyncSession, _auth_on):
        role = await _mk_role(db_session, "own", ["kb.own"])
        a = await _mk_user(db_session, "a", role)
        doc = await _mk_doc(db_session, owner_user_id=a.id, kb_id=None)
        assert await check_document_access(doc, a, "read", db_session) is True
        assert await check_document_access(doc, a, "delete", db_session) is True

    async def test_non_owner_denied_kbless_doc(self, db_session: AsyncSession, _auth_on):
        """The HIGH-3 IDOR: a KB-less doc must NOT be readable by a non-owner."""
        role = await _mk_role(db_session, "own", ["kb.own"])
        a = await _mk_user(db_session, "a", role)
        b = await _mk_user(db_session, "b", role)
        doc = await _mk_doc(db_session, owner_user_id=a.id, kb_id=None)
        assert await check_document_access(doc, b, "read", db_session) is False
        assert await check_document_access(doc, b, "delete", db_session) is False

    async def test_admin_reaches_any_doc(self, db_session: AsyncSession, _auth_on):
        own = await _mk_role(db_session, "own", ["kb.own"])
        adm = await _mk_role(db_session, "adm", [Permission.KB_ALL.value])
        a = await _mk_user(db_session, "a", own)
        admin = await _mk_user(db_session, "admin", adm)
        doc = await _mk_doc(db_session, owner_user_id=a.id, kb_id=None)
        assert await check_document_access(doc, admin, "read", db_session) is True

    async def test_kbless_no_atom_denied_for_non_admin(
        self, db_session: AsyncSession, _auth_on
    ):
        """A KB-less doc with no owner atom (legacy) must fail closed for a
        non-admin — the ``kb is None`` / no-atom path denies rather than the old
        skip-the-check fall-through."""
        role = await _mk_role(db_session, "own", ["kb.own"])
        b = await _mk_user(db_session, "b", role)
        doc = await _mk_doc(db_session, owner_user_id=None, kb_id=None)
        assert doc.atom_id is None
        assert await check_document_access(doc, b, "read", db_session) is False

    async def test_kb_backed_doc_delegates_to_kb_acl(
        self, db_session: AsyncSession, _auth_on
    ):
        """A KB-backed doc with no owner atom resolves via the KB ACL — the KB
        owner reaches it. (The stranger-denied path runs check_kb_access's
        Postgres-only explicit-grant SQL, exercised on the real DB by
        test_knowledge.py; not re-tested here to keep this file SQLite-safe.)"""
        role = await _mk_role(db_session, "own", ["kb.own"])
        a = await _mk_user(db_session, "a", role)
        kb = await _mk_kb(db_session, "A-KB", owner_id=a.id)
        doc = await _mk_doc(db_session, owner_user_id=None, kb_id=kb.id)
        assert await check_document_access(doc, a, "read", db_session) is True
