"""
Tests für Knowledge Base Funktionalität

Testet:
- Model-Schema (Spalten owner_id, is_public vorhanden)
- API-Endpoints für KB-Management
- Dokumenten-Management
- Permission-basierter Zugriff
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, KnowledgeBase, Role, User

# ============================================================================
# Model / Schema Tests
# ============================================================================

class TestKnowledgeBaseModel:
    """Tests für das KnowledgeBase Model"""

    @pytest.mark.database
    async def test_knowledge_base_has_owner_id_column(self, async_engine):
        """Prüft, dass die owner_id Spalte im Schema existiert"""
        async with async_engine.connect() as conn:
            def check_columns(sync_conn):
                inspector = inspect(sync_conn)
                columns = [col['name'] for col in inspector.get_columns('knowledge_bases')]
                return columns

            columns = await conn.run_sync(check_columns)

        assert 'owner_id' in columns, \
            f"Spalte 'owner_id' fehlt in knowledge_bases. Vorhandene Spalten: {columns}"

    @pytest.mark.database
    async def test_knowledge_base_has_is_public_column(self, async_engine):
        """Prüft, dass die is_public Spalte im Schema existiert"""
        async with async_engine.connect() as conn:
            def check_columns(sync_conn):
                inspector = inspect(sync_conn)
                columns = [col['name'] for col in inspector.get_columns('knowledge_bases')]
                return columns

            columns = await conn.run_sync(check_columns)

        assert 'is_public' in columns, \
            f"Spalte 'is_public' fehlt in knowledge_bases. Vorhandene Spalten: {columns}"

    @pytest.mark.database
    async def test_knowledge_base_schema_complete(self, async_engine):
        """Prüft, dass alle erwarteten Spalten vorhanden sind"""
        expected_columns = {
            'id', 'name', 'description', 'is_active',
            'owner_id', 'is_public', 'created_at', 'updated_at'
        }

        async with async_engine.connect() as conn:
            def check_columns(sync_conn):
                inspector = inspect(sync_conn)
                columns = {col['name'] for col in inspector.get_columns('knowledge_bases')}
                return columns

            actual_columns = await conn.run_sync(check_columns)

        missing = expected_columns - actual_columns
        assert not missing, f"Fehlende Spalten: {missing}"

    @pytest.mark.database
    async def test_create_knowledge_base(self, db_session: AsyncSession, sample_knowledge_base_data):
        """Testet das Erstellen einer Knowledge Base"""
        kb = KnowledgeBase(**sample_knowledge_base_data)
        db_session.add(kb)
        await db_session.commit()
        await db_session.refresh(kb)

        assert kb.id is not None
        assert kb.name == sample_knowledge_base_data["name"]
        assert kb.is_active is True
        assert kb.is_public is False
        assert kb.owner_id is None

    @pytest.mark.database
    async def test_create_knowledge_base_with_owner(
        self,
        db_session: AsyncSession,
        test_user: User,
        sample_knowledge_base_data
    ):
        """Testet das Erstellen einer Knowledge Base mit Owner"""
        kb = KnowledgeBase(
            owner_id=test_user.id,
            **sample_knowledge_base_data
        )
        db_session.add(kb)
        await db_session.commit()
        await db_session.refresh(kb)

        assert kb.owner_id == test_user.id

    @pytest.mark.database
    async def test_create_public_knowledge_base(self, db_session: AsyncSession):
        """Testet das Erstellen einer öffentlichen Knowledge Base"""
        kb = KnowledgeBase(
            name="Public KB",
            description="A public knowledge base",
            is_public=True
        )
        db_session.add(kb)
        await db_session.commit()
        await db_session.refresh(kb)

        assert kb.is_public is True

    @pytest.mark.database
    async def test_knowledge_base_unique_name(self, db_session: AsyncSession, test_knowledge_base):
        """Testet, dass KB-Namen eindeutig sein müssen"""
        from sqlalchemy.exc import IntegrityError

        duplicate_kb = KnowledgeBase(
            name=test_knowledge_base.name,  # Same name
            description="Duplicate"
        )
        db_session.add(duplicate_kb)

        with pytest.raises(IntegrityError):
            await db_session.commit()


class TestDocumentModel:
    """Tests für das Document Model"""

    @pytest.mark.database
    async def test_document_schema_complete(self, async_engine):
        """Prüft, dass alle erwarteten Spalten vorhanden sind"""
        expected_columns = {
            'id', 'filename', 'title', 'file_path', 'file_type',
            'file_size', 'file_hash', 'status', 'error_message',
            'chunk_count', 'page_count', 'knowledge_base_id',
            'created_at', 'processed_at'
        }

        async with async_engine.connect() as conn:
            def check_columns(sync_conn):
                inspector = inspect(sync_conn)
                columns = {col['name'] for col in inspector.get_columns('documents')}
                return columns

            actual_columns = await conn.run_sync(check_columns)

        missing = expected_columns - actual_columns
        assert not missing, f"Fehlende Spalten in documents: {missing}"

    @pytest.mark.database
    async def test_create_document(
        self,
        db_session: AsyncSession,
        test_knowledge_base: KnowledgeBase,
        sample_document_data
    ):
        """Testet das Erstellen eines Dokuments"""
        doc = Document(
            knowledge_base_id=test_knowledge_base.id,
            **sample_document_data
        )
        db_session.add(doc)
        await db_session.commit()
        await db_session.refresh(doc)

        assert doc.id is not None
        assert doc.knowledge_base_id == test_knowledge_base.id
        assert doc.filename == sample_document_data["filename"]

    @pytest.mark.database
    async def test_document_knowledge_base_relationship(
        self,
        db_session: AsyncSession,
        test_document: Document
    ):
        """Testet die Beziehung zwischen Document und KnowledgeBase"""
        result = await db_session.execute(
            select(Document).where(Document.id == test_document.id)
        )
        doc = result.scalar_one()

        assert doc.knowledge_base_id is not None
        assert doc.knowledge_base is not None
        assert doc.knowledge_base.name == "Test Knowledge Base"


class TestRoleModel:
    """Tests für das Role Model"""

    @pytest.mark.database
    async def test_role_schema_complete(self, async_engine):
        """Prüft, dass alle erwarteten Spalten vorhanden sind"""
        expected_columns = {
            'id', 'name', 'description', 'permissions',
            'is_system', 'created_at', 'updated_at'
        }

        async with async_engine.connect() as conn:
            def check_columns(sync_conn):
                inspector = inspect(sync_conn)
                columns = {col['name'] for col in inspector.get_columns('roles')}
                return columns

            actual_columns = await conn.run_sync(check_columns)

        missing = expected_columns - actual_columns
        assert not missing, f"Fehlende Spalten in roles: {missing}"

    @pytest.mark.database
    async def test_create_role(self, db_session: AsyncSession, sample_role_data):
        """Testet das Erstellen einer Rolle"""
        role = Role(**sample_role_data)
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        assert role.id is not None
        assert role.name == sample_role_data["name"]
        assert "kb.all" in role.permissions


class TestUserModel:
    """Tests für das User Model"""

    @pytest.mark.database
    async def test_user_schema_complete(self, async_engine):
        """Prüft, dass alle erwarteten Spalten vorhanden sind"""
        expected_columns = {
            'id', 'username', 'email', 'password_hash',
            'role_id', 'is_active', 'speaker_id',
            'created_at', 'updated_at', 'last_login'
        }

        async with async_engine.connect() as conn:
            def check_columns(sync_conn):
                inspector = inspect(sync_conn)
                columns = {col['name'] for col in inspector.get_columns('users')}
                return columns

            actual_columns = await conn.run_sync(check_columns)

        missing = expected_columns - actual_columns
        assert not missing, f"Fehlende Spalten in users: {missing}"

    @pytest.mark.database
    async def test_create_user(self, db_session: AsyncSession, test_role: Role, sample_user_data):
        """Testet das Erstellen eines Users"""
        user = User(role_id=test_role.id, **sample_user_data)
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        assert user.id is not None
        assert user.username == sample_user_data["username"]
        assert user.role_id == test_role.id

    @pytest.mark.database
    async def test_user_role_relationship(self, db_session: AsyncSession, test_user: User):
        """Testet die Beziehung zwischen User und Role"""
        result = await db_session.execute(
            select(User).where(User.id == test_user.id)
        )
        user = result.scalar_one()

        assert user.role is not None
        assert user.role.name == "TestRole"


# ============================================================================
# Knowledge Base Query Tests
# ============================================================================

class TestKnowledgeBaseQueries:
    """Tests für Knowledge Base Abfragen"""

    @pytest.mark.database
    async def test_list_knowledge_bases(self, db_session: AsyncSession, test_knowledge_base):
        """Testet das Auflisten von Knowledge Bases"""
        result = await db_session.execute(select(KnowledgeBase))
        bases = result.scalars().all()

        assert len(bases) >= 1
        assert any(kb.name == "Test Knowledge Base" for kb in bases)

    @pytest.mark.database
    async def test_list_knowledge_bases_with_owner_id(
        self,
        db_session: AsyncSession,
        test_knowledge_base_with_owner: KnowledgeBase,
        test_user: User
    ):
        """Testet das Filtern nach owner_id"""
        result = await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.owner_id == test_user.id)
        )
        bases = result.scalars().all()

        assert len(bases) == 1
        assert bases[0].owner_id == test_user.id

    @pytest.mark.database
    async def test_list_public_knowledge_bases(self, db_session: AsyncSession):
        """Testet das Filtern nach öffentlichen KBs"""
        # Create a public KB
        public_kb = KnowledgeBase(
            name="Public Test KB",
            is_public=True
        )
        db_session.add(public_kb)
        await db_session.commit()

        result = await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.is_public == True)
        )
        bases = result.scalars().all()

        assert len(bases) >= 1
        assert all(kb.is_public for kb in bases)

    @pytest.mark.database
    async def test_knowledge_base_with_documents(
        self,
        db_session: AsyncSession,
        test_document: Document
    ):
        """Testet das Laden einer KB mit Dokumenten"""
        from sqlalchemy.orm import selectinload

        result = await db_session.execute(
            select(KnowledgeBase)
            .options(selectinload(KnowledgeBase.documents))
            .where(KnowledgeBase.id == test_document.knowledge_base_id)
        )
        kb = result.scalar_one()

        assert kb is not None
        assert len(kb.documents) >= 1
        assert any(doc.id == test_document.id for doc in kb.documents)


# ============================================================================
# Conversation Schema Tests
# ============================================================================

class TestConversationModel:
    """Tests für das Conversation Model mit user_id"""

    @pytest.mark.database
    async def test_conversation_has_user_id_column(self, async_engine):
        """Prüft, dass die user_id Spalte im Schema existiert"""
        async with async_engine.connect() as conn:
            def check_columns(sync_conn):
                inspector = inspect(sync_conn)
                columns = [col['name'] for col in inspector.get_columns('conversations')]
                return columns

            columns = await conn.run_sync(check_columns)

        assert 'user_id' in columns, \
            f"Spalte 'user_id' fehlt in conversations. Vorhandene Spalten: {columns}"


# ============================================================================
# Integration Tests (mit API)
# ============================================================================

class TestKnowledgeBaseAPI:
    """API Integration Tests für Knowledge Bases"""

    @pytest.mark.integration
    async def test_list_knowledge_bases_endpoint(
        self,
        async_client: AsyncClient,
        test_knowledge_base: KnowledgeBase
    ):
        """Testet GET /api/knowledge/bases"""
        response = await async_client.get("/api/knowledge/bases")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.integration
    async def test_create_knowledge_base_endpoint(self, async_client: AsyncClient):
        """Testet POST /api/knowledge/bases"""
        response = await async_client.post(
            "/api/knowledge/bases",
            json={
                "name": "API Test KB",
                "description": "Created via API test",
                "is_public": False
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "API Test KB"
        assert "id" in data
        assert "owner_id" in data
        assert "is_public" in data

    @pytest.mark.integration
    async def test_get_knowledge_base_endpoint(
        self,
        async_client: AsyncClient,
        test_knowledge_base: KnowledgeBase
    ):
        """Testet GET /api/knowledge/bases/{kb_id}"""
        response = await async_client.get(f"/api/knowledge/bases/{test_knowledge_base.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_knowledge_base.id
        assert data["name"] == test_knowledge_base.name

    @pytest.mark.integration
    async def test_get_nonexistent_knowledge_base(self, async_client: AsyncClient):
        """Testet GET /api/knowledge/bases/{kb_id} für nicht-existente KB"""
        response = await async_client.get("/api/knowledge/bases/99999")

        assert response.status_code == 404

    @pytest.mark.integration
    async def test_delete_knowledge_base_endpoint(
        self,
        async_client: AsyncClient,
        test_knowledge_base: KnowledgeBase
    ):
        """Testet DELETE /api/knowledge/bases/{kb_id}"""
        response = await async_client.delete(f"/api/knowledge/bases/{test_knowledge_base.id}")

        assert response.status_code == 200

        # Verify it's deleted
        response = await async_client.get(f"/api/knowledge/bases/{test_knowledge_base.id}")
        assert response.status_code == 404

    @pytest.mark.integration
    async def test_knowledge_stats_endpoint(self, async_client: AsyncClient):
        """Testet GET /api/knowledge/stats"""
        response = await async_client.get("/api/knowledge/stats")

        assert response.status_code == 200
        data = response.json()
        assert "document_count" in data
        assert "knowledge_base_count" in data
        assert "embedding_model" in data


class TestDocumentAPI:
    """API Integration Tests für Dokumente"""

    @pytest.mark.integration
    async def test_list_documents_endpoint(
        self,
        async_client: AsyncClient,
        test_document: Document
    ):
        """Testet GET /api/knowledge/documents"""
        response = await async_client.get("/api/knowledge/documents")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.integration
    async def test_list_documents_by_knowledge_base(
        self,
        async_client: AsyncClient,
        test_document: Document,
        test_knowledge_base: KnowledgeBase
    ):
        """Testet GET /api/knowledge/documents?knowledge_base_id=X"""
        response = await async_client.get(
            f"/api/knowledge/documents?knowledge_base_id={test_knowledge_base.id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.integration
    async def test_get_document_endpoint(
        self,
        async_client: AsyncClient,
        test_document: Document
    ):
        """Testet GET /api/knowledge/documents/{document_id}"""
        response = await async_client.get(f"/api/knowledge/documents/{test_document.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_document.id
        assert data["filename"] == test_document.filename

    @pytest.mark.integration
    async def test_get_nonexistent_document(self, async_client: AsyncClient):
        """Testet GET /api/knowledge/documents/{document_id} für nicht-existentes Dokument"""
        response = await async_client.get("/api/knowledge/documents/99999")

        assert response.status_code == 404


# ============================================================================
# RAG Service: delete_document FK cleanup
# ============================================================================


class TestDeleteDocumentFKCleanup:
    """Tests that delete_document NULLs out chat_uploads.document_id before delete."""

    @pytest.mark.unit
    async def test_delete_document_nulls_chat_upload_fk(self, db_session, test_document):
        """Deleting a document referenced by a ChatUpload should succeed."""
        from models.database import ChatUpload
        from services.rag_service import RAGService

        # Create a ChatUpload that references the document
        upload = ChatUpload(
            session_id="test-session",
            filename="test.pdf",
            file_type="pdf",
            file_size=1024,
            file_hash="abc123",
            status="completed",
            document_id=test_document.id,
            knowledge_base_id=test_document.knowledge_base_id,
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)
        upload_id = upload.id

        # Delete should succeed (previously raised ForeignKeyViolationError)
        rag = RAGService(db_session)
        result = await rag.delete_document(test_document.id)
        assert result is True

        # ChatUpload should still exist but with document_id = NULL
        db_session.expire_all()
        from sqlalchemy import select as sa_select
        stmt = sa_select(ChatUpload).where(ChatUpload.id == upload_id)
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is not None
        assert row.document_id is None
