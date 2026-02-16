"""
Tests für das Verschieben von Dokumenten zwischen Knowledge Bases.

Testet:
- Einzelnes Dokument verschieben
- Bulk-Move mehrere Dokumente
- Ziel-KB existiert nicht → Fehler
- Dokument bereits in Ziel-KB → übersprungen
- Chunks bleiben intakt nach Move
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, DocumentChunk, KnowledgeBase
from services.rag_service import RAGService


@pytest.fixture
async def source_kb(db_session: AsyncSession) -> KnowledgeBase:
    """Create source knowledge base"""
    kb = KnowledgeBase(name="Source KB", description="Source", is_active=True)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def target_kb(db_session: AsyncSession) -> KnowledgeBase:
    """Create target knowledge base"""
    kb = KnowledgeBase(name="Target KB", description="Target", is_active=True)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def inactive_kb(db_session: AsyncSession) -> KnowledgeBase:
    """Create inactive knowledge base"""
    kb = KnowledgeBase(name="Inactive KB", description="Inactive", is_active=False)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def docs_in_source(db_session: AsyncSession, source_kb: KnowledgeBase) -> list[Document]:
    """Create multiple documents in source KB"""
    docs = []
    for i in range(3):
        doc = Document(
            filename=f"doc_{i}.pdf",
            title=f"Document {i}",
            file_path=f"/tmp/doc_{i}.pdf",
            file_type="pdf",
            file_size=1000 + i,
            status="completed",
            chunk_count=2,
            knowledge_base_id=source_kb.id,
        )
        db_session.add(doc)
        docs.append(doc)
    await db_session.commit()
    for doc in docs:
        await db_session.refresh(doc)
    return docs


@pytest.fixture
async def doc_with_chunks(
    db_session: AsyncSession, source_kb: KnowledgeBase
) -> Document:
    """Create a document with chunks in source KB"""
    doc = Document(
        filename="chunked_doc.pdf",
        title="Chunked Document",
        file_path="/tmp/chunked_doc.pdf",
        file_type="pdf",
        file_size=5000,
        status="completed",
        chunk_count=2,
        knowledge_base_id=source_kb.id,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # Add chunks
    for i in range(2):
        chunk = DocumentChunk(
            document_id=doc.id,
            content=f"Chunk {i} content for testing",
            chunk_index=i,
            chunk_type="paragraph",
        )
        db_session.add(chunk)
    await db_session.commit()
    return doc


class TestMoveDocuments:
    """Tests für RAGService.move_documents()"""

    @pytest.mark.database
    async def test_move_single_document(
        self, db_session: AsyncSession, docs_in_source, target_kb
    ):
        """Einzelnes Dokument verschieben"""
        rag = RAGService(db_session)
        doc = docs_in_source[0]

        moved = await rag.move_documents([doc.id], target_kb.id)

        assert moved == 1
        await db_session.refresh(doc)
        assert doc.knowledge_base_id == target_kb.id

    @pytest.mark.database
    async def test_move_multiple_documents(
        self, db_session: AsyncSession, docs_in_source, target_kb
    ):
        """Bulk-Move mehrere Dokumente"""
        rag = RAGService(db_session)
        doc_ids = [d.id for d in docs_in_source]

        moved = await rag.move_documents(doc_ids, target_kb.id)

        assert moved == 3
        for doc in docs_in_source:
            await db_session.refresh(doc)
            assert doc.knowledge_base_id == target_kb.id

    @pytest.mark.database
    async def test_move_target_kb_not_found(
        self, db_session: AsyncSession, docs_in_source
    ):
        """Ziel-KB existiert nicht → ValueError"""
        rag = RAGService(db_session)

        with pytest.raises(ValueError, match="nicht gefunden"):
            await rag.move_documents([docs_in_source[0].id], 99999)

    @pytest.mark.database
    async def test_move_target_kb_inactive(
        self, db_session: AsyncSession, docs_in_source, inactive_kb
    ):
        """Ziel-KB ist nicht aktiv → ValueError"""
        rag = RAGService(db_session)

        with pytest.raises(ValueError, match="nicht aktiv"):
            await rag.move_documents([docs_in_source[0].id], inactive_kb.id)

    @pytest.mark.database
    async def test_move_already_in_target(
        self, db_session: AsyncSession, docs_in_source, source_kb
    ):
        """Dokument bereits in Ziel-KB → wird übersprungen"""
        rag = RAGService(db_session)
        doc = docs_in_source[0]

        # Move to the KB it's already in
        moved = await rag.move_documents([doc.id], source_kb.id)

        assert moved == 0
        await db_session.refresh(doc)
        assert doc.knowledge_base_id == source_kb.id

    @pytest.mark.database
    async def test_move_mixed_already_and_new(
        self, db_session: AsyncSession, docs_in_source, target_kb
    ):
        """Mischung: einige schon in Ziel, andere nicht"""
        rag = RAGService(db_session)

        # Move first doc to target first
        docs_in_source[0].knowledge_base_id = target_kb.id
        await db_session.commit()

        doc_ids = [d.id for d in docs_in_source]
        moved = await rag.move_documents(doc_ids, target_kb.id)

        # Only 2 should be moved (doc 0 was already there)
        assert moved == 2

    @pytest.mark.database
    async def test_move_nonexistent_documents(
        self, db_session: AsyncSession, target_kb
    ):
        """Nicht existierende Dokument-IDs → ValueError"""
        rag = RAGService(db_session)

        with pytest.raises(ValueError, match="Keine der angegebenen"):
            await rag.move_documents([99998, 99999], target_kb.id)

    @pytest.mark.database
    async def test_move_chunks_stay_intact(
        self, db_session: AsyncSession, doc_with_chunks, target_kb
    ):
        """Chunks bleiben nach dem Move dem Dokument zugeordnet"""
        rag = RAGService(db_session)

        moved = await rag.move_documents([doc_with_chunks.id], target_kb.id)
        assert moved == 1

        # Verify chunks still belong to document
        result = await db_session.execute(
            select(DocumentChunk).where(
                DocumentChunk.document_id == doc_with_chunks.id
            )
        )
        chunks = list(result.scalars().all())
        assert len(chunks) == 2

        # Verify document is in target KB
        await db_session.refresh(doc_with_chunks)
        assert doc_with_chunks.knowledge_base_id == target_kb.id
