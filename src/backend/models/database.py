"""
Datenbank Models
"""
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, Boolean, Column, DateTime, FetchedValue, Float, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

from utils.config import settings

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    # Fallback für Tests ohne pgvector
    PGVECTOR_AVAILABLE = False
    Vector = None

Base = declarative_base()


def _utcnow():
    """Return current UTC time as naive datetime (DB compat, replaces deprecated utcnow)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Conversation(Base):
    """Konversationen / Chat-Historie"""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Ownership (nullable for anonymous/legacy conversations)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    speaker_id = Column(Integer, ForeignKey("speakers.id"), nullable=True, index=True)

    # Conversation state (survives history truncation)
    context_vars = Column(JSON, nullable=True)   # Pinned structured state (entities, focus)
    summary = Column(Text, nullable=True)         # LLM-generated summary of older messages

    # Beziehungen
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    user = relationship("User", back_populates="conversations", foreign_keys=[user_id])
    speaker = relationship("Speaker", foreign_keys=[speaker_id])

class Message(Base):
    """Einzelne Nachrichten"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), index=True)
    role = Column(String)  # 'user' oder 'assistant'
    content = Column(Text)
    timestamp = Column(DateTime, default=_utcnow)
    message_metadata = Column(JSON, nullable=True)  # Umbenannt von 'metadata'

    # Beziehungen
    conversation = relationship("Conversation", back_populates="messages")

class Task(Base):
    """Aufgaben"""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    description = Column(Text, nullable=True)
    task_type = Column(String)  # 'homeassistant', 'n8n', 'research', 'camera'
    status = Column(String, default="pending")  # pending, running, completed, failed
    priority = Column(Integer, default=0)
    parameters = Column(JSON)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)

# --- Speaker Recognition Models ---

class Speaker(Base):
    """Registrierter Sprecher für Speaker Recognition"""
    __tablename__ = "speakers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)       # "Max Mustermann"
    alias = Column(String(50), unique=True, index=True)  # "max" (für Ansprache)
    is_admin = Column(Boolean, default=False)        # Admin-Berechtigung (legacy, use User.role)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Beziehungen
    embeddings = relationship("SpeakerEmbedding", back_populates="speaker", cascade="all, delete-orphan")

    # Link to User account (for voice authentication)
    user = relationship("User", back_populates="speaker", uselist=False, foreign_keys="User.speaker_id")


class SpeakerEmbedding(Base):
    """Voice Embedding für einen Sprecher (mehrere pro Speaker für bessere Erkennung)"""
    __tablename__ = "speaker_embeddings"

    id = Column(Integer, primary_key=True, index=True)
    speaker_id = Column(Integer, ForeignKey("speakers.id"), nullable=False, index=True)
    embedding = Column(Text, nullable=False)         # Base64-encoded numpy array
    sample_duration = Column(Integer, nullable=True)  # Dauer des Samples in Millisekunden
    created_at = Column(DateTime, default=_utcnow)

    # Beziehungen
    speaker = relationship("Speaker", back_populates="embeddings")


class SpeakerVocabularyCorpus(Base):
    """Raw confirmed-speaker transcripts mined for per-user STT bias.

    Privacy: `circle_tier` defaults to 0 (self) — these are private speech
    samples and must never cross-bias other users' STT. Only persisted for
    real (non-auto-enrolled) users with confidence above the recognition
    threshold. The batch tokenizer reads from here and writes summaries
    into `SpeakerVocabulary`.
    """
    __tablename__ = "speaker_vocabulary_corpus"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    language = Column(String(10), nullable=False, default="de")
    circle_tier = Column(SmallInteger, nullable=False, default=0)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)


class SpeakerVocabulary(Base):
    """Per-user term frequencies, periodically rebuilt by the batch tokenizer.

    The Whisper prompt builder's vocab handler queries the top-N terms by
    frequency for the active speaker and folds them into the initial_prompt.
    Cold start (no rows) means the handler returns None → platform default
    fixed-structure prompt is used instead.
    """
    __tablename__ = "speaker_vocabulary"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    term = Column(String(100), nullable=False)
    frequency = Column(Integer, nullable=False, default=0)
    language = Column(String(10), nullable=False, default="de")
    circle_tier = Column(SmallInteger, nullable=False, default=0)
    last_updated = Column(DateTime, default=_utcnow, nullable=False)

    # Composite index used by the prompt-builder's read path (top-N terms by
    # frequency for a given user+language). Declared here so Alembic
    # autogenerate sees it on subsequent revisions and doesn't emit spurious
    # diffs for a "missing" index.
    __table_args__ = (
        UniqueConstraint("user_id", "term", "language", name="uq_speaker_vocab_user_term_lang"),
        Index(
            "ix_speaker_vocab_user_lang_freq",
            "user_id", "language", frequency.desc(),
        ),
    )


# --- Room management, device, and output-device models ---
#
# These moved to ha_glue/models/database.py as part of Phase 1 of the
# Renfield open-source extraction. They are re-exported at the bottom of
# this file for backwards compatibility during the Week 1-4 transition.
# New code should import directly from ha_glue.models.database.


# =============================================================================
# RAG (Retrieval-Augmented Generation) Models
# =============================================================================

class KnowledgeBase(Base):
    """Gruppierung von Dokumenten für RAG"""
    __tablename__ = "knowledge_bases"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    # Ownership (nullable for legacy KBs)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Public KBs are visible to all users with at least kb.shared permission
    is_public = Column(Boolean, default=False, nullable=False)

    # Circles v1: default circle tier for new chunks of this KB.
    # Per-chunk circle_tier on document_chunks may override this default.
    # Back-fill from is_public during pc20260420_circles_v1 migration.
    default_circle_tier = Column(Integer, nullable=False, default=0)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Beziehungen
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")
    owner = relationship("User", back_populates="knowledge_bases", foreign_keys=[owner_id])
    # Note: KBPermission was removed in circles v1 — explicit per-resource shares
    # now live on AtomExplicitGrant (per-chunk granularity, MAX-permissive with
    # circle_tier). Migration code in pc20260420_circles_v1_schema.py preserves
    # legacy permissions by creating one grant per (chunk, user, permission).


class Document(Base):
    """Hochgeladene Dokumente (Metadaten)"""
    __tablename__ = "documents"

    # Unique on (file_hash, knowledge_base_id) closes the concurrent-upload
    # race: two requests that both pass the SELECT-based duplicate check
    # can race to INSERT — this constraint converts the loser into an
    # IntegrityError which the route maps to 409. Migration c3d4e5f6g7h8
    # uses NULLS NOT DISTINCT on Postgres so the global-RAG case
    # (knowledge_base_id IS NULL) is also covered.
    __table_args__ = (
        UniqueConstraint(
            "file_hash", "knowledge_base_id",
            name="uq_documents_file_hash_kb",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True, index=True)

    # File Info
    filename = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)
    file_type = Column(String(50))  # pdf, docx, txt, etc.
    file_size = Column(Integer)     # in bytes
    file_hash = Column(String(64), nullable=True, index=True)  # SHA256 hash for duplicate detection

    # Processing Status
    status = Column(String(50), default="pending", index=True)  # pending, processing, completed, failed
    error_message = Column(Text, nullable=True)

    # Metadata (extrahiert aus Dokument)
    title = Column(String(512), nullable=True)
    author = Column(String(255), nullable=True)
    page_count = Column(Integer, nullable=True)
    chunk_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    processed_at = Column(DateTime, nullable=True)

    # Circles v2 (atoms-per-document): the access-control unit moved from
    # per-chunk to per-document (see docs/design/atoms-granularity.md). The
    # atoms row carries atom_type='kb_document' and owner/policy; chunks no
    # longer carry atom_id themselves but keep a denormalized circle_tier
    # mirrored from this column for fast SQL retrieval filters.
    atom_id = Column(String(36), ForeignKey("atoms.atom_id", ondelete="CASCADE"), nullable=True, index=True)
    circle_tier = Column(Integer, nullable=False, default=0)

    # Beziehungen
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


# Document Chunk Embedding Dimension (configurable, default: nomic-embed-text = 768)
EMBEDDING_DIMENSION = settings.embedding_dimension


class DocumentChunk(Base):
    """
    Text-Chunks mit Embedding-Vektor für RAG

    Jedes Dokument wird in kleinere Chunks aufgeteilt,
    die einzeln in der Vektordatenbank indexiert werden.
    """
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)

    # Content
    content = Column(Text, nullable=False)

    # Embedding Vector (768 dimensions for nomic-embed-text)
    # Uses pgvector extension for vector similarity search
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )

    # Parent-Child Chunking (parent_chunk_id references larger parent chunk)
    parent_chunk_id = Column(Integer, ForeignKey("document_chunks.id"), nullable=True, index=True)

    # Chunk Metadata
    chunk_index = Column(Integer)           # Position im Dokument (0-basiert)
    page_number = Column(Integer, nullable=True)
    section_title = Column(String(512), nullable=True)
    chunk_type = Column(String(50), default="paragraph")  # paragraph, table, code, formula, parent

    # Full-text search vector. Post-pc20260529 this is a Postgres GENERATED
    # STORED column that unions to_tsvector across all FTS_LANGUAGES
    # (DE/EN/FR/IT/ES/NL). READ-ONLY from the app: Postgres rejects any
    # INSERT/UPDATE that supplies a value with `cannot insert a non-DEFAULT
    # value into column "search_vector"`. The `FetchedValue()` marker
    # tells SQLAlchemy to leave the column out of INSERTs/UPDATEs entirely
    # so ORM-level row creation works against the GENERATED schema.
    # Sqlite-shimmed test runs ignore the marker (it's a Postgres-only
    # semantic). The DDL itself lives in the alembic migration; this
    # declaration only governs ORM behavior.
    search_vector = Column(TSVECTOR, FetchedValue(), nullable=True)

    # Additional Metadata (JSON für Flexibilität)
    chunk_metadata = Column(JSON, nullable=True)  # Umbenannt von 'metadata' (SQLAlchemy reserved)

    # Circles v2 (atoms-per-document): chunks no longer carry atom_id. The
    # access-control unit is the parent Document — retrieval joins chunks →
    # documents and filters on documents.atom_id / documents.circle_tier.
    # circle_tier is kept here as a denormalized mirror of
    # Document.circle_tier so existing pgvector queries that filter on the
    # chunk-level tier continue to work without the JOIN for hot paths.
    # AtomService.update_tier on a kb_document atom cascades into this
    # column via UPDATE document_chunks SET circle_tier=? WHERE document_id=?.
    # (Dropped in pc20260423_atoms_per_document migration.)
    circle_tier = Column(Integer, nullable=False, default=0)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)

    # Beziehungen
    document = relationship("Document", back_populates="chunks")
    parent_chunk = relationship("DocumentChunk", remote_side=[id], foreign_keys=[parent_chunk_id])

    # Vector-search index is created at migration time, NOT by SQLAlchemy
    # `create_all`. Currently HNSW with halfvec cast (production runs
    # 2560-dim embeddings via qwen3-embedding:4b — pgvector 0.8.1 limits
    # regular `vector` to 2000-dim, so the index uses
    # `embedding::halfvec(<dim>)` where <dim> matches the active
    # embedding_dimension at the time of the latest resize migration —
    # 2560 in production today):
    #
    #   CREATE INDEX idx_document_chunks_embedding_hnsw
    #   ON document_chunks
    #   USING hnsw ((embedding::halfvec(2560)) halfvec_cosine_ops)
    #   WITH (m = 16, ef_construction = 64);
    #
    # Originally created as IVFFlat by b2c3d4e5f6g7_add_rag_tables.py and
    # converted by j0k1l2m3n4o5_add_fk_indexes_and_hnsw.py. Index gets
    # auto-dropped + recreated whenever the column type changes (see
    # cce1984705df_resize_embedding_vectors_768_to_2560.py).


# =============================================================================
# Chat Upload Model
# =============================================================================

# Upload Status Constants
UPLOAD_STATUS_PROCESSING = "processing"
UPLOAD_STATUS_COMPLETED = "completed"
UPLOAD_STATUS_FAILED = "failed"

UPLOAD_STATUSES = [UPLOAD_STATUS_PROCESSING, UPLOAD_STATUS_COMPLETED, UPLOAD_STATUS_FAILED]


class ChatUpload(Base):
    """Dokument-Upload direkt im Chat (ohne RAG-Indexierung)"""
    __tablename__ = "chat_uploads"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    filename = Column(String(255), nullable=False)
    file_type = Column(String(50))
    file_size = Column(Integer)
    file_hash = Column(String(64), nullable=True, index=True)
    extracted_text = Column(Text, nullable=True)
    status = Column(String(50), default=UPLOAD_STATUS_PROCESSING, index=True)
    error_message = Column(Text, nullable=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True)
    file_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class PaperlessPendingConfirm(Base):
    """Transient state between paperless-confirm tool turns.

    ``forward_attachment_to_paperless`` writes one of these rows during
    the cold-start window (first N uploads per user) when it needs the
    user's approval before firing the final Paperless upload.
    ``paperless_commit_upload`` reads the row, fires the upload on
    "ja" / deletes it on "nein", and cleans up.

    Abandoned rows (user walks away, never answers) get swept after
    24 h by the PR 4 cleanup job. See
    ``docs/design/paperless-llm-metadata.md`` § Confirm flow state machine.
    """
    __tablename__ = "paperless_pending_confirms"

    confirm_token = Column(String(36), primary_key=True)  # uuid4
    attachment_id = Column(
        Integer,
        ForeignKey("chat_uploads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Width matches ChatUpload.session_id — smaller truncates/fails on
    # real Postgres when session ids exceed 64 chars.
    session_id = Column(String(128), nullable=False, index=True)
    # Nullable so AUTH_ENABLED=false (single-user dev) works: the tool
    # gets user_id=None from the executor and stores NULL here rather
    # than crashing on the FK constraint. Cold-start counter
    # increments skip for NULL users.
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Raw LLM response (pre-fuzzy, pre-validation). Needed for diff
    # computation when the user approves and the post-fuzzy differs.
    llm_output = Column(JSON, nullable=False)
    # Post-fuzzy, post-validation. This is what the user sees and
    # approves in the confirm preview.
    post_fuzzy_output = Column(JSON, nullable=False)
    # FieldResolution[] (column name kept for legacy reasons) — one per
    # extracted field that did NOT resolve to an exact taxonomy hit.
    # Carries extracted_value + near-match candidates the user picks
    # from in the confirm preview. The commit tool reads this back to
    # turn the user's response into final field values + create_*
    # calls.
    proposals = Column(JSON, nullable=False, default=list, server_default="[]")
    # Caps the ambiguous-response loop so a user who keeps typing
    # "hmmm" can't pin the row indefinitely.
    edit_rounds = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=_utcnow)


class PaperlessExtractionExample(Base):
    """Correction-feedback source. Primary fill: confirm-time diffs
    written by ``paperless_commit_upload`` when the user approves an
    extraction with non-trivial new-entry proposals. Secondary fill:
    the PR 4 sweeper reading Paperless-UI edits within 1 h of upload.

    PR 3 reads the table to augment future extraction prompts with
    the N most relevant past corrections (embedding similarity over
    ``doc_text``). See
    ``docs/design/paperless-llm-metadata.md`` § Correction feedback loop.
    """
    __tablename__ = "paperless_extraction_examples"

    id = Column(Integer, primary_key=True)
    doc_text = Column(Text, nullable=False)
    llm_output = Column(JSON, nullable=False)
    user_approved = Column(JSON, nullable=False)
    # 'confirm_diff' (primary), 'paperless_ui_sweep' (secondary, PR 4),
    # or 'seed' for any manually-seeded starter examples.
    source = Column(String(32), nullable=False, index=True)
    # Set by PR 4's no-re-edit filter when a ui_sweep correction turns
    # out to be taxonomy drift rather than an extraction error. The
    # prompt-augmentation reader ignores superseded rows.
    superseded = Column(Boolean, nullable=False, default=False, server_default="false")
    # PR 3: doc_text embedding for similarity retrieval. Nullable so
    # rows persisted before the embedding step succeeds (or pre-PR 3
    # rows) are still kept for the raw diff signal — they just won't
    # surface via the retriever.
    doc_text_embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True,
    )
    # PR 3: owner-only scoping. Nullable to support AUTH_ENABLED=false
    # dev setups where the agent has no user_id. Retrieval filters on
    # this column so user A's corrections never surface in user B's
    # extraction prompt.
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=_utcnow)


class PaperlessUploadTracking(Base):
    """Records every successful Paperless upload so the PR 4 UI-edit
    sweeper can detect when the user later edits the metadata in the
    Paperless UI.

    Row is inserted after ``mcp.paperless.upload_document`` returns
    a document_id (both the confirm-flow path in ``paperless_commit_tool``
    and the silent-past-cap path in ``chat_upload_tool``). Once the
    sweeper has compared the stored ``original_metadata`` against the
    live Paperless state, ``swept_at`` is set and the row becomes
    inert.
    """
    __tablename__ = "paperless_upload_tracking"

    id = Column(Integer, primary_key=True)
    chat_upload_id = Column(
        Integer,
        ForeignKey("chat_uploads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    paperless_document_id = Column(Integer, nullable=False)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    uploaded_at = Column(DateTime, nullable=False, default=_utcnow)
    # Exactly what we sent to Paperless — the diff baseline.
    original_metadata = Column(JSON, nullable=False)
    # OCR extract the extractor saw. Persisted so a later ui_sweep row
    # carries the same doc_text shape as a confirm_diff row would.
    doc_text = Column(Text, nullable=True)
    # NULL until the sweeper processes this row.
    swept_at = Column(DateTime, nullable=True, index=True)


# Document Processing Status Constants
DOC_STATUS_PENDING = "pending"
DOC_STATUS_PROCESSING = "processing"
DOC_STATUS_COMPLETED = "completed"
DOC_STATUS_FAILED = "failed"

DOC_STATUSES = [DOC_STATUS_PENDING, DOC_STATUS_PROCESSING, DOC_STATUS_COMPLETED, DOC_STATUS_FAILED]


# Chunk Type Constants
CHUNK_TYPE_PARAGRAPH = "paragraph"
CHUNK_TYPE_TABLE = "table"
CHUNK_TYPE_CODE = "code"
CHUNK_TYPE_FORMULA = "formula"
CHUNK_TYPE_HEADING = "heading"
CHUNK_TYPE_LIST = "list"
CHUNK_TYPE_IMAGE_CAPTION = "image_caption"

CHUNK_TYPES = [
    CHUNK_TYPE_PARAGRAPH,
    CHUNK_TYPE_TABLE,
    CHUNK_TYPE_CODE,
    CHUNK_TYPE_FORMULA,
    CHUNK_TYPE_HEADING,
    CHUNK_TYPE_LIST,
    CHUNK_TYPE_IMAGE_CAPTION,
]


# =============================================================================
# Authentication & Authorization Models (RPBAC)
# =============================================================================

class Role(Base):
    """
    User role with associated permissions.

    Roles define a set of permissions that can be assigned to users.
    System roles (is_system=True) cannot be deleted.
    """
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=True)

    # Permissions as JSON array of permission strings
    # Example: ["ha.full", "kb.shared", "cam.view", "chat.own"]
    permissions = Column(JSON, default=list, nullable=False)

    # System roles cannot be deleted
    is_system = Column(Boolean, default=False, nullable=False)

    # Priority for conflict resolution (lower = higher priority)
    # Admin=10, Familie=50, Gast=90, new roles=100
    priority = Column(Integer, default=100, nullable=False, server_default="100")

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    users = relationship("User", back_populates="role")

    def has_permission(self, permission: str) -> bool:
        """Check if this role has a specific permission."""
        from models.permissions import Permission, has_permission
        try:
            perm = Permission(permission)
            return has_permission(self.permissions or [], perm)
        except ValueError:
            return permission in (self.permissions or [])



class User(Base):
    """
    User account for authentication and authorization.

    Users are assigned a role which determines their permissions.
    Users can optionally be linked to a Speaker for voice authentication.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)

    # Role assignment
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)

    # Account status
    is_active = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False, server_default="false")

    # User preferences
    preferred_language = Column(String(10), default="de", nullable=False)
    media_follow_enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    personality_style = Column(String(20), default="freundlich", nullable=False, server_default="freundlich")
    personality_prompt = Column(Text, nullable=True)  # Free-text personality fine-tuning

    # Cold-start counter for the Paperless LLM-metadata confirm flow.
    # Increments on each successful upload that went through the confirm
    # step; once >= N (10), the confirm is skipped and extraction runs
    # silently. See docs/design/paperless-llm-metadata.md § 5.
    paperless_confirms_used = Column(Integer, nullable=False, default=0, server_default="0")

    # Optional link to Speaker for voice authentication
    speaker_id = Column(Integer, ForeignKey("speakers.id"), nullable=True, unique=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    last_login = Column(DateTime, nullable=True)

    # Relationships
    role = relationship("Role", back_populates="users")
    speaker = relationship("Speaker", back_populates="user", foreign_keys=[speaker_id])

    # Owned resources (will be added as relationships are defined)
    knowledge_bases = relationship("KnowledgeBase", back_populates="owner", foreign_keys="KnowledgeBase.owner_id")
    conversations = relationship("Conversation", back_populates="user", foreign_keys="Conversation.user_id")

    def has_permission(self, permission: str) -> bool:
        """Check if this user has a specific permission via their role."""
        if not self.role:
            return False
        return self.role.has_permission(permission)

    def get_permissions(self) -> list:
        """Get all permissions for this user."""
        if not self.role:
            return []
        return self.role.permissions or []



# =============================================================================
# Knowledge Base Permission Levels — circles v1 retired the per-KB
# `kb_permissions` table; per-resource grants now live in AtomExplicitGrant
# (chunk-level). These string constants stay because route validators and
# the kb_shares_service helper still reference them.
# =============================================================================

KB_PERM_READ = "read"
KB_PERM_WRITE = "write"
KB_PERM_ADMIN = "admin"

KB_PERMISSION_LEVELS = [KB_PERM_READ, KB_PERM_WRITE, KB_PERM_ADMIN]


# =============================================================================
# System Settings (Key-Value Store)
# =============================================================================

class SystemSetting(Base):
    """
    Key-Value Store for runtime system settings.

    Used for settings that can be changed at runtime without restarting
    the server, like wake word configuration.

    Keys follow a namespace pattern: "category.setting_name"
    Values are stored as JSON strings for type flexibility.
    """
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)  # JSON-encoded value
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationship to user who last updated
    updater = relationship("User", foreign_keys=[updated_by])


# ==========================================================================
# Intent Correction Feedback
# ==========================================================================

class IntentCorrection(Base):
    """
    Stores user corrections for wrong intent classifications, agent tool choices,
    and complexity detection. Embeddings enable semantic similarity search for
    few-shot prompt injection — the system learns from its mistakes.

    feedback_type:
      - "intent": Wrong intent classification (Single-Intent path)
      - "agent_tool": Wrong tool choice in Agent Loop
      - "complexity": Wrong simple/complex classification
    """
    __tablename__ = "intent_corrections"

    id = Column(Integer, primary_key=True, index=True)
    message_text = Column(Text, nullable=False)
    feedback_type = Column(String(20), nullable=False, index=True)
    original_value = Column(String(100), nullable=False)
    corrected_value = Column(String(100), nullable=False)
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )
    context = Column(JSON, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User", foreign_keys=[user_id])


# ==========================================================================
# Proactive Notifications
# ==========================================================================

# Notification Status Constants
NOTIFICATION_PENDING = "pending"
NOTIFICATION_DELIVERED = "delivered"
NOTIFICATION_ACKNOWLEDGED = "acknowledged"
NOTIFICATION_DISMISSED = "dismissed"

NOTIFICATION_STATUSES = [
    NOTIFICATION_PENDING,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DISMISSED,
]

# Notification Urgency Constants
URGENCY_CRITICAL = "critical"
URGENCY_INFO = "info"
URGENCY_LOW = "low"

URGENCY_LEVELS = [URGENCY_CRITICAL, URGENCY_INFO, URGENCY_LOW]


class Notification(Base):
    """
    Proaktive Benachrichtigungen — empfangen via Webhook (z.B. von HA-Automationen),
    gespeichert in der DB und an verbundene Geräte ausgeliefert (WS + TTS).
    """
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(100), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    urgency = Column(String(20), default=URGENCY_INFO)
    # room_id is a loose reference to ha_glue.rooms.id — no ForeignKey constraint
    # so that platform-only deployments (without the ha_glue schema) can still
    # create this table. Ha-glue code that needs the Room object does a runtime
    # lookup via the hook system instead of a SQLAlchemy relationship.
    room_id = Column(Integer, nullable=True, index=True)
    room_name = Column(String(100), nullable=True)
    source = Column(String(50), default="ha_automation")
    source_data = Column(JSON, nullable=True)
    status = Column(String(20), default=NOTIFICATION_PENDING, index=True)
    delivered_to = Column(JSON, nullable=True)
    acknowledged_by = Column(String(100), nullable=True)
    tts_delivered = Column(Boolean, default=False)
    dedup_key = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    delivered_at = Column(DateTime, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    # Phase 2: Intelligence columns
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True,
    )
    enriched = Column(Boolean, default=False)
    original_message = Column(Text, nullable=True)
    urgency_auto = Column(Boolean, default=False)

    # Privacy-aware TTS delivery
    privacy = Column(String(20), default="public")
    target_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Relationships — room relationship removed (layering rule: platform
    # must not depend on ha_glue). Use the hook system to resolve room_id
    # to a Room object when ha_glue is loaded.
    target_user = relationship("User", foreign_keys=[target_user_id])


class NotificationSuppression(Base):
    """
    Feedback-Learning: Benutzer unterdrückt ähnliche Benachrichtigungen.
    Speichert Event-Pattern + Embedding für semantischen Abgleich.
    """
    __tablename__ = "notification_suppressions"

    id = Column(Integer, primary_key=True, index=True)
    event_pattern = Column(String(255), nullable=False, index=True)
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True,
    )
    source_notification_id = Column(Integer, ForeignKey("notifications.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reason = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)

    source_notification = relationship("Notification", foreign_keys=[source_notification_id])
    user = relationship("User", foreign_keys=[user_id])


# Reminder Status Constants
REMINDER_PENDING = "pending"
REMINDER_FIRED = "fired"
REMINDER_CANCELLED = "cancelled"


class Reminder(Base):
    """
    Timer-basierte Erinnerungen ("in 30 Minuten", "um 18:00").
    """
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    message = Column(Text, nullable=False)
    trigger_at = Column(DateTime, nullable=False, index=True)
    # room_id is a loose reference to ha_glue.rooms.id — see the Notification
    # class for the rationale (no ForeignKey, no relationship).
    room_id = Column(Integer, nullable=True)
    room_name = Column(String(100), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    session_id = Column(String(255), nullable=True)
    status = Column(String(20), default=REMINDER_PENDING)
    notification_id = Column(Integer, ForeignKey("notifications.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    fired_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    notification = relationship("Notification", foreign_keys=[notification_id])


# ==========================================================================
# Conversation Memory (Long-term)
# ==========================================================================

# Memory Category Constants
MEMORY_CATEGORY_PREFERENCE = "preference"   # User preferences ("Ich mag Jazz")
MEMORY_CATEGORY_FACT = "fact"               # Personal facts ("Mein Hund heißt Bello")
MEMORY_CATEGORY_CONTEXT = "context"         # Ephemeral context (decays over time)
MEMORY_CATEGORY_INSTRUCTION = "instruction" # Standing instructions ("Sprich mich mit Du an")
MEMORY_CATEGORY_PROCEDURAL = "procedural"   # Behavioral rules ("Immer auf Deutsch antworten")

MEMORY_CATEGORIES = [
    MEMORY_CATEGORY_PREFERENCE,
    MEMORY_CATEGORY_FACT,
    MEMORY_CATEGORY_CONTEXT,
    MEMORY_CATEGORY_INSTRUCTION,
    MEMORY_CATEGORY_PROCEDURAL,
]

# Memory Source Constants
MEMORY_SOURCE_USER_STATED = "user_stated"       # Explicitly told by user
MEMORY_SOURCE_LLM_INFERRED = "llm_inferred"     # Extracted by LLM from conversation
MEMORY_SOURCE_SYSTEM = "system_confirmed"        # Confirmed by system (e.g. from tool data)

MEMORY_SOURCES = [
    MEMORY_SOURCE_USER_STATED,
    MEMORY_SOURCE_LLM_INFERRED,
    MEMORY_SOURCE_SYSTEM,
]

# Memory Scope Constants
MEMORY_SCOPE_USER = "user"       # Visible only to the owning user
MEMORY_SCOPE_TEAM = "team"       # Visible to team members
MEMORY_SCOPE_GLOBAL = "global"   # Visible to all users


class ConversationMemory(Base):
    """
    Long-term memory extracted from conversations.

    Stores facts, preferences, instructions, and context that the assistant
    should remember across sessions. Uses pgvector embeddings for semantic
    retrieval of relevant memories.
    """
    __tablename__ = "conversation_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    content = Column(Text, nullable=False)
    category = Column(String(20), nullable=False, default=MEMORY_CATEGORY_FACT, index=True)

    # Source tracking
    source_session_id = Column(String(255), nullable=True, index=True)
    source_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
    source = Column(String(20), nullable=False, default=MEMORY_SOURCE_LLM_INFERRED)  # user_stated / llm_inferred / system_confirmed

    # Scoping
    scope = Column(String(10), nullable=False, default=MEMORY_SCOPE_USER)  # user / team / global
    team_id = Column(String(100), nullable=True)  # Team identifier for team-scoped memories

    # Confidence and behavioral triggers
    confidence = Column(Float, nullable=False, default=1.0)  # Decays for unaccessed llm_inferred
    trigger_pattern = Column(String(255), nullable=True)  # Regex for procedural memory activation

    # Embedding for semantic search
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )

    # Importance and lifecycle
    importance = Column(Float, default=0.5)
    expires_at = Column(DateTime, nullable=True)
    access_count = Column(Integer, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, index=True)

    # Circles v1: FK to atoms registry + denormalized circle_tier for SQL filter perf.
    # Back-fill from scope='user'->0(self), 'team'->2(household), 'global'->4(public)
    # in pc20260420_circles_v1 migration. team_id remains on the row (parked for v2
    # named-circles per Finding 1.2C).
    atom_id = Column(String(36), ForeignKey("atoms.atom_id", ondelete="CASCADE"), nullable=True, index=True)
    circle_tier = Column(Integer, nullable=False, default=0)

    # Full-text search vector (Postgres GENERATED STORED column from
    # pc20260528). READ-ONLY from the app side — Postgres maintains it
    # via the multilingual union in services.fts_languages.
    #
    # Why not a SQLAlchemy `Computed(...)` clause: sqlite rejects
    # `to_tsvector` in generated columns ("non-deterministic functions
    # prohibited") even though sqlite stubs the type as text. The
    # dialect-conditional alternative (custom @compiles directive) is
    # more machinery than the migration-only contract requires.
    #
    # Contract: the GENERATED column is owned by alembic, not by
    # create_all. Dev-DB bootstrap via `Base.metadata.create_all`
    # produces a plain nullable TSVECTOR column initially; the
    # pc20260528 migration then DROPs that column unconditionally and
    # re-ADDs it as GENERATED (safe because the column is fully
    # derived from `content` — Postgres repopulates it for every row).
    # Pure-create_all setups with no migrations would have the column
    # silently NULL post-insert and the lexical retriever would
    # return 0 results; the migration is the only supported path to
    # a working lexical retriever.
    #
    # FetchedValue() tells SQLAlchemy to exclude this column from ORM
    # INSERT/UPDATE statements — without it, INSERTs would emit
    # `search_vector = NULL` and Postgres would raise
    # `cannot insert a non-DEFAULT value into column "search_vector"`
    # because the column is GENERATED. Sqlite ignores the marker (it's
    # Postgres-only semantic) so the existing sqlite-shimmed unit tests
    # are unaffected.
    search_vector = Column(TSVECTOR, FetchedValue(), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])
    source_message = relationship("Message", foreign_keys=[source_message_id])


class EpisodicMemory(Base):
    """
    Episodic memory — records of past interactions (what happened, when, with what tools).

    Created automatically after each agent interaction. Used for contextual recall
    ("last time you asked about release X...") and batch-summarized into semantic
    facts when episode count exceeds threshold.
    """
    __tablename__ = "episodic_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    session_id = Column(String(255), nullable=True, index=True)

    # Episode content
    summary = Column(Text, nullable=False)          # Human-readable summary of what happened
    topic = Column(String(50), nullable=True, index=True)  # Domain topic (release_status, jira_search, etc.)
    entities = Column(JSON, nullable=True)           # {release_id: "...", jira_key: "...", ...}
    tools_used = Column(JSON, nullable=True)         # ["mcp.release.get_release", "mcp.jira.search"]
    outcome = Column(String(20), nullable=True)      # "success" / "error" / "no_result"

    # Embedding for semantic search
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )

    # Importance and lifecycle
    importance = Column(Float, default=0.5)
    access_count = Column(Integer, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, index=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        Index('ix_episodic_user_active', 'user_id', 'is_active'),
        Index('ix_episodic_user_topic', 'user_id', 'topic'),
    )

    user = relationship("User", foreign_keys=[user_id])


# ProceduralSkill / Atom-type discriminators — defined here (in front of
# the ORM classes that reference them as Column defaults) instead of at
# the file's tail-end constant block. Column(default=X) evaluates X at
# class-construction time, so the constants must exist before the class
# body runs. The tier integers + ATOM_TYPE_* + SKILL_SOURCE_* tables at
# the bottom of this file are the canonical exports — these forward-
# declarations are kept in sync with them and re-asserted below.
SKILL_SOURCE_AUTO_EXTRACTED = "auto_extracted"
SKILL_SOURCE_SEED = "seed"
SKILL_SOURCE_USER_CREATED = "user_created"

# Skill lifecycle (v2.10 — replaces is_active/pinned booleans).
# State machine:
#     [draft] --approve--> [approved]      seed/user_created skills land here
#        |                    |            directly; auto_extracted lands in
#        | reject             | archive    [draft] for human review.
#        v                    v
#     [rejected] <--reopen--> [archived]   reopen returns to [draft].
#
# Only [approved] participates in agent retrieval. [draft] surfaces in the
# admin Skills Inbox. [rejected] and [archived] are excluded from retrieval
# but kept for audit + the would-have-injected shadow query.
SKILL_STATUS_DRAFT = "draft"
SKILL_STATUS_APPROVED = "approved"
SKILL_STATUS_REJECTED = "rejected"
SKILL_STATUS_ARCHIVED = "archived"
SKILL_STATUSES = [
    SKILL_STATUS_DRAFT,
    SKILL_STATUS_APPROVED,
    SKILL_STATUS_REJECTED,
    SKILL_STATUS_ARCHIVED,
]


class ProceduralSkill(Base):
    """
    Procedural skill — agent-learned how-to recipe for a class of tasks.

    Self-learning Phase 1: the agent extracts a Skill after a complex turn
    (≥ ``settings.skill_extract_min_tool_calls`` successful tool calls).
    On a future similar request, the SkillService retrieves the top-K
    similar skills by embedding cosine and injects them into the agent
    prompt as procedural memory (parallel to how memory_context and
    tool_corrections are injected today).

    Lifecycle:
      - source="auto_extracted": created by SkillExtractor after a turn.
      - source="seed":           loaded from src/backend/seed_skills/*.md
                                  at boot. user_id=NULL, circle_tier=4 (public).
                                  Bypasses the atom registry (no per-user owner).
      - source="user_created":   authored via /api/skills.
      - success_count / failure_count: bumped by the agent loop based on
        the outcome of a turn that used the skill. Used by the curator
        (Phase 4) to merge / archive / demote.

    Circles:
      atom_id is nullable. Auto-extracted and user-created skills register
      with AtomService.upsert_atom (atom_type=procedural_skill) and get a
      real atom_id + per-user policy. Seeds bypass the atom registry —
      circle_tier=4 means "visible to everyone", retrieval treats the NULL
      user_id as "system-owned, no access check needed".
    """
    __tablename__ = "procedural_skills"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    title = Column(String(255), nullable=False)
    body_md = Column(Text, nullable=False)
    trigger_examples = Column(JSON, nullable=False, default=list)
    tool_sequence = Column(JSON, nullable=False, default=list)

    source = Column(String(20), nullable=False, default=SKILL_SOURCE_AUTO_EXTRACTED)
    learned_from_conversation_id = Column(
        Integer, ForeignKey("conversations.id"), nullable=True
    )
    version = Column(Integer, nullable=False, default=1)

    success_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime, nullable=True)
    # Lifecycle. See SKILL_STATUS_* constants + the ASCII diagram above.
    status = Column(
        String(20),
        nullable=False,
        default=SKILL_STATUS_DRAFT,
        index=True,
    )
    # Owner can pin an approved skill so the curator's stale-archive job
    # never touches it. Pin is orthogonal to status — only meaningful for
    # status='approved' rows.
    pinned = Column(Boolean, nullable=False, default=False)

    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True,
    )

    atom_id = Column(
        String(36),
        ForeignKey("atoms.atom_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    circle_tier = Column(Integer, nullable=False, default=0)

    # Curator audit trail (Phase 4). When the curator merges two
    # near-duplicate skills, the loser's row is kept (audit) with
    # is_active=False AND merged_into_id pointing at the winner. NULL on
    # all non-archived rows and on archived-but-not-merged rows
    # (e.g. auto-demoted by record_outcome).
    merged_into_id = Column(
        Integer,
        ForeignKey("procedural_skills.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    user = relationship("User", foreign_keys=[user_id])
    learned_from = relationship(
        "Conversation", foreign_keys=[learned_from_conversation_id]
    )
    merged_into = relationship(
        "ProceduralSkill",
        foreign_keys=[merged_into_id],
        remote_side="ProceduralSkill.id",
    )


# AgentTrajectory.outcome discriminators.
TRAJECTORY_OUTCOME_SUCCESS = "success"        # final_answer + no error step
TRAJECTORY_OUTCOME_TOOL_FAIL = "tool_fail"    # tool steps had failures but eventually answered
TRAJECTORY_OUTCOME_ABORT = "abort"            # loop exhausted / circuit broken / timeout
TRAJECTORY_OUTCOME_USER_CORRECTED = "user_corrected"  # post-hoc correction via /api/feedback


class AgentTrajectory(Base):
    """
    Full trace of a single agent turn, captured for offline training data.

    Self-learning Phase 2: rather than just learning "did this skill help?"
    (success_count vs failure_count on ProceduralSkill), we keep the full
    {user message, tool calls, tool results, final answer, outcome} trace
    of every turn the agent runs. Exported as JSONL via /api/trajectories
    for downstream LoRA fine-tuning of the local chat / agent model.

    Retention is bounded by ``settings.trajectory_retention_days`` (default
    30); the cleanup scheduler in lifecycle.py deletes older rows unless
    ``flagged_for_retention=True`` (e.g., the turn produced an
    auto-extracted skill — we want to keep those forever as gold examples).

    Privacy: ``redacted_payload`` is left nullable in v1 because PII
    redaction is a Phase 4 concern. Producers populate ``raw_payload`` and
    callers MUST NOT export rows whose ``redacted_payload`` is NULL
    once Phase 4 lands.

    Schema is deliberately denormalized JSONB (one row per turn) rather
    than a relational join over messages + steps: the trace is the unit
    of training, and exporting JSONL is the only read path that matters.
    """
    __tablename__ = "agent_trajectories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)

    # The complete trace. Keys: user_message, system_role, steps[],
    # final_answer, lang, tools_available[]. See TrajectoryService.save
    # for the schema.
    raw_payload = Column(JSON, nullable=False)
    # PII-scrubbed export-ready payload. Phase 4 will populate this in a
    # follow-up scheduled job; v1 leaves it NULL.
    redacted_payload = Column(JSON, nullable=True)

    # Quick-filter fields denormalized out of raw_payload for indexed lookups.
    outcome = Column(String(20), nullable=False, default=TRAJECTORY_OUTCOME_SUCCESS, index=True)
    tool_count = Column(Integer, nullable=False, default=0)
    distinct_tool_count = Column(Integer, nullable=False, default=0)
    token_count = Column(Integer, nullable=True)  # input+output for the turn

    # Linkage to ProceduralSkill if this turn produced or used a skill.
    extracted_skill_id = Column(
        Integer, ForeignKey("procedural_skills.id", ondelete="SET NULL"), nullable=True
    )
    # IDs of injected skills (JSON list); used for offline skill-effectiveness analytics.
    used_skill_ids = Column(JSON, nullable=True)

    flagged_for_retention = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)

    __table_args__ = (
        Index("idx_trajectories_user_created", "user_id", "created_at"),
        Index("idx_trajectories_outcome_created", "outcome", "created_at"),
    )

    user = relationship("User", foreign_keys=[user_id])
    conversation = relationship("Conversation", foreign_keys=[conversation_id])
    extracted_skill = relationship("ProceduralSkill", foreign_keys=[extracted_skill_id])


class ToolOutcomeStat(Base):
    """
    Rolling success/failure counters per (user, tool) pair.

    Self-learning Phase 3: every ``tool_result`` step in the agent loop
    bumps either ``success_count`` or ``failure_count`` on the row keyed
    by (user_id, tool_name). At prompt-build time the agent reads the
    stats for the current asker and injects a ``{tool_health_warnings}``
    block when a tool's success rate has dropped below
    ``settings.tool_health_warn_success_rate`` AND it has been used at
    least ``settings.tool_health_warn_min_uses`` times — keeps the LLM
    from confidently picking a tool that's been broken in production.

    Stats are PER-USER (not global) so a permission-gated tool that's
    perfectly fine for Alice but always errors for Bob (who lacks the
    grant) doesn't pollute Alice's prompt.

    The ``last_failure_summary`` text helps the LLM disambiguate
    "this tool's broken for me right now" vs "this tool returns weird
    data shapes for this kind of query" without re-reading the full
    trace — one-sentence summary captured from the latest fail.
    """
    __tablename__ = "tool_outcome_stats"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    tool_name = Column(String(128), nullable=False, index=True)

    success_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime, nullable=True)
    last_failure_at = Column(DateTime, nullable=True)
    last_failure_summary = Column(Text, nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "tool_name", name="uq_tool_outcome_user_tool"),
        Index("idx_tool_outcome_user_tool", "user_id", "tool_name"),
    )

    user = relationship("User", foreign_keys=[user_id])


# SkillCuratorRun.run_type + status discriminators.
CURATOR_RUN_TYPE_SCHEDULED = "scheduled"
CURATOR_RUN_TYPE_MANUAL = "manual"
CURATOR_RUN_STATUS_RUNNING = "running"
CURATOR_RUN_STATUS_SUCCESS = "success"
CURATOR_RUN_STATUS_PARTIAL = "partial"
CURATOR_RUN_STATUS_FAILED = "failed"


class SkillCuratorRun(Base):
    """
    Audit row for one invocation of the skill curator job.

    Self-learning admin console (v2.10): the AdminCuratorPage shows a history
    of curator runs (scheduled + manual) with the counters needed to answer
    "is the curator actually doing anything useful right now?" The "Run Now"
    button on that page writes a row with run_type='manual' and
    triggered_by_user_id set; the scheduled invocation in lifecycle.py
    writes run_type='scheduled' and leaves triggered_by_user_id NULL.

    A row is created at start (status='running', finished_at NULL) and
    updated on completion. On crash the row is left in 'running' state —
    the admin page treats any 'running' row older than ~10 min as 'failed'
    in the UI so a stuck row doesn't masquerade as in-progress forever.
    """
    __tablename__ = "skill_curator_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=_utcnow, nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)
    run_type = Column(String(20), nullable=False, default=CURATOR_RUN_TYPE_SCHEDULED)
    triggered_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String(20), nullable=False, default=CURATOR_RUN_STATUS_RUNNING)
    skills_examined = Column(Integer, nullable=False, default=0)
    duplicate_pairs_found = Column(Integer, nullable=False, default=0)
    duplicate_pairs_merged = Column(Integer, nullable=False, default=0)
    stale_skills_archived = Column(Integer, nullable=False, default=0)
    duration_seconds = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)

    triggered_by = relationship("User", foreign_keys=[triggered_by_user_id])


class SkillWouldHaveInjectedLog(Base):
    """
    Shadow log: rows the retrieval *would* have returned if the draft-gate
    were not in place.

    During the v2.10 rollout, SkillService.find_similar() runs a dual query:
    the production retrieval filters to status='approved' only; a parallel
    shadow query relaxes the filter to also include draft/rejected/archived
    candidates. The shadow-only matches are logged here so we can measure
    how much recall the human-in-the-loop draft-gate costs in practice.

    After the rollout window (~30 days) this table can be truncated; it is
    not part of any production read path.
    """
    __tablename__ = "skill_would_have_injected_log"

    id = Column(Integer, primary_key=True, index=True)
    skill_id = Column(
        Integer,
        ForeignKey("procedural_skills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    similarity_score = Column(Float, nullable=False)
    # Snapshot of skill.status at the moment of the shadow query — so a
    # later approve/reject doesn't make the log row look like it
    # corresponds to a different gate decision.
    status_at_query = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)

    skill = relationship("ProceduralSkill", foreign_keys=[skill_id])
    user = relationship("User", foreign_keys=[user_id])


# Memory History — Audit trail for memory modifications
MEMORY_ACTION_CREATED = "created"
MEMORY_ACTION_UPDATED = "updated"
MEMORY_ACTION_DELETED = "deleted"
MEMORY_ACTIONS = [MEMORY_ACTION_CREATED, MEMORY_ACTION_UPDATED, MEMORY_ACTION_DELETED]

MEMORY_CHANGED_BY_SYSTEM = "system"
MEMORY_CHANGED_BY_USER = "user"
MEMORY_CHANGED_BY_RESOLUTION = "contradiction_resolution"


# ==========================================================================
# Knowledge Graph (Entity-Relation Triples)
# ==========================================================================

# Entity Type Constants
KG_ENTITY_TYPES = ["person", "place", "organization", "thing", "event", "concept"]

class KGEntity(Base):
    """Named entity extracted from conversations for the Knowledge Graph."""
    __tablename__ = "kg_entities"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    entity_type = Column(String(50), nullable=False)  # person, place, organization, thing, event, concept
    description = Column(Text, nullable=True)
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )
    mention_count = Column(Integer, default=1)
    first_seen_at = Column(DateTime, default=_utcnow)
    last_seen_at = Column(DateTime, default=_utcnow)
    is_active = Column(Boolean, default=True, index=True)
    # FK to atoms registry + denormalized circle_tier for SQL filter perf.
    # Back-fill from old scope='personal'->0(self), yaml-defined->2(household)
    # in pc20260420_circles_v1 migration.
    atom_id = Column(String(36), ForeignKey("atoms.atom_id", ondelete="CASCADE"), nullable=True, index=True)
    circle_tier = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index('ix_kg_entities_user_active', 'user_id', 'is_active'),
        Index('idx_kg_entities_owner_tier', 'user_id', 'circle_tier'),
    )

    user = relationship("User", foreign_keys=[user_id])
    subject_relations = relationship(
        "KGRelation", foreign_keys="KGRelation.subject_id",
        back_populates="subject", cascade="all, delete-orphan"
    )
    object_relations = relationship(
        "KGRelation", foreign_keys="KGRelation.object_id",
        back_populates="object", cascade="all, delete-orphan"
    )


class KGRelation(Base):
    """Directed relation between two entities in the Knowledge Graph."""
    __tablename__ = "kg_relations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    subject_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False, index=True)
    predicate = Column(String(100), nullable=False)
    object_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False, index=True)
    confidence = Column(Float, default=0.8)
    source_session_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    is_active = Column(Boolean, default=True, index=True)

    # Circles v1: FK to atoms + denormalized circle_tier.
    # Back-fill from MIN(subject.circle_tier, object.circle_tier) in
    # pc20260420_circles_v1 migration. Cascade rule for runtime tier changes
    # lives in AtomService.update_tier (when a kg_node tier changes, all
    # incident relations recompute their tier in the same transaction).
    atom_id = Column(String(36), ForeignKey("atoms.atom_id", ondelete="CASCADE"), nullable=True, index=True)
    circle_tier = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index('idx_kg_relations_subj_tier', 'subject_id', 'circle_tier'),
        Index('idx_kg_relations_obj_tier', 'object_id', 'circle_tier'),
    )

    subject = relationship("KGEntity", foreign_keys=[subject_id], back_populates="subject_relations")
    object = relationship("KGEntity", foreign_keys=[object_id], back_populates="object_relations")
    user = relationship("User", foreign_keys=[user_id])


class MemoryHistory(Base):
    """Audit trail for memory modifications (create/update/delete)."""
    __tablename__ = "memory_history"

    id = Column(Integer, primary_key=True, index=True)
    memory_id = Column(Integer, ForeignKey("conversation_memories.id"), nullable=False, index=True)
    action = Column(String(20), nullable=False, index=True)
    old_content = Column(Text, nullable=True)
    old_category = Column(String(20), nullable=True)
    old_importance = Column(Float, nullable=True)
    new_content = Column(Text, nullable=True)
    new_category = Column(String(20), nullable=True)
    new_importance = Column(Float, nullable=True)
    changed_by = Column(String(30), nullable=False, default=MEMORY_CHANGED_BY_SYSTEM)
    created_at = Column(DateTime, default=_utcnow)

    memory = relationship("ConversationMemory", foreign_keys=[memory_id])


# Memory v2 Shadow Log — Lane B/2 phase-A comparison substrate.
#
# Records every turn the dispatcher routes through v2-shadow mode.
# Stores both v1's outcome (what the user actually saw — committed) and
# v2's outcome (what would have happened — rolled back via savepoint).
# Enables a daily diff report measuring v1-vs-v2 parity before flipping
# memory_extraction_v2_authoritative=True.
#
# Operational rule (per the locked plan's "Forgetting (new) — DROPPED"
# decision and the v2 shadow protocol): this table grows monotonically;
# no auto-cleanup. Operators can prune via a one-shot SQL after the
# Phase B flip lands.

class MemoryV2ShadowLog(Base):
    """Per-turn v1/v2 outcome record for Phase A shadow-mode validation."""
    __tablename__ = "memory_v2_shadow_log"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)
    # ON DELETE SET NULL must mirror the alembic migration. Without
    # ondelete= here, create_all() (used by CI test fixtures) emits a
    # RESTRICT FK that diverges from the production migration and
    # fails user-deletion with IntegrityError.
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    session_id = Column(String(255), nullable=True, index=True)
    lang = Column(String(10), nullable=True)

    # v1: what actually shipped to the user (authoritative).
    v1_outcome = Column(String(20), nullable=True)  # e.g. "saved_3" or "noop_should_extract_false"
    v1_extracted_count = Column(Integer, nullable=True)
    v1_latency_seconds = Column(Float, nullable=True)

    # v2: what would have happened (rolled back via savepoint).
    v2_outcome = Column(String(20), nullable=True)  # "noop" | "add" | "update" | "delete" | "fallback" | "error"
    v2_ops_json = Column(Text, nullable=True)       # full MemoryOpsList serialization
    v2_extracted_count = Column(Integer, nullable=True)
    v2_fallback_reason = Column(String(40), nullable=True)  # parse_error|llm_timeout|id_reject|schema_reject|null
    v2_latency_seconds = Column(Float, nullable=True)
    v2_error = Column(String(80), nullable=True)  # type(exc).__name__ when shadow swallowed an exception

    user = relationship("User", foreign_keys=[user_id])


# ==========================================================================
# BLE Presence Detection (moved to ha_glue/models/database.py — re-exported below)
# ==========================================================================


# System Setting Keys
SETTING_WAKEWORD_KEYWORD = "wakeword.keyword"
SETTING_WAKEWORD_THRESHOLD = "wakeword.threshold"
SETTING_WAKEWORD_COOLDOWN_MS = "wakeword.cooldown_ms"
SETTING_NOTIFICATION_WEBHOOK_TOKEN = "notification.webhook_token"

SYSTEM_SETTING_KEYS = [
    SETTING_WAKEWORD_KEYWORD,
    SETTING_WAKEWORD_THRESHOLD,
    SETTING_WAKEWORD_COOLDOWN_MS,
    SETTING_NOTIFICATION_WEBHOOK_TOKEN,
]


# ==========================================================================
# Paperless Document Audit (moved to ha_glue/models/database.py)
# Radio Favorites (moved to ha_glue/models/database.py)
# ==========================================================================


# ==========================================================================
# Circles v1 — concentric privacy access model
# ==========================================================================
#
# Schema overview (per design doc and pc20260420_circles_v1_schema.py):
#
#   Circle              per-user dimension config + default capture policy
#   CircleMembership    (owner, member, dimension, value) — F-Generalize
#   Atom                polymorphic registry: one row per piece of info
#                       (chunk / kg_node / kg_edge / conversation_memory)
#                       with a circle policy (JSON) and FK to source row
#   AtomExplicitGrant   per-resource exception grant (subsumes legacy
#                       KBPermission; applied alongside circle tier via
#                       MAX-permissive semantics)
#
# The five tier indices map to DESIGN.md tier visual language:
#   0 = self        (deepest crimson  #a5162f)
#   1 = trusted     (brand crimson    #e63e54)
#   2 = household   (cream            #f0e6d3)
#   3 = extended    (light turquoise  #71fbd0)
#   4 = public      (deep turquoise   #00937c)

# Tier integer constants — keep in sync with the alembic migration and
# CircleResolver. Public surface for callers that need to reference tiers
# without hard-coding integers.
TIER_SELF = 0
TIER_TRUSTED = 1
TIER_HOUSEHOLD = 2
TIER_EXTENDED = 3
TIER_PUBLIC = 4

# Atom type discriminators — one per source table the polymorphic registry
# wraps. Keep in sync with PolymorphicAtomStore source dispatch.
# 'kb_chunk' was retired in pc20260423_atoms_per_document; historical alembic
# migrations hard-code that string literal where they still need it.
ATOM_TYPE_KB_DOCUMENT = "kb_document"
ATOM_TYPE_KG_NODE = "kg_node"
ATOM_TYPE_KG_EDGE = "kg_edge"
ATOM_TYPE_CONVERSATION_MEMORY = "conversation_memory"
ATOM_TYPE_PROCEDURAL_SKILL = "procedural_skill"

# ProceduralSkill.source discriminators are declared up-file (in front of
# the ORM class body that references them as Column defaults). Asserting
# the canonical values here so a future drift between the two declaration
# sites fails at import time instead of silently desyncing.
assert SKILL_SOURCE_AUTO_EXTRACTED == "auto_extracted"
assert SKILL_SOURCE_SEED == "seed"
assert SKILL_SOURCE_USER_CREATED == "user_created"

# Atom explicit grant permission levels — mirrors the legacy KB_PERM_*
# values for migration parity.
ATOM_GRANT_READ = "read"
ATOM_GRANT_WRITE = "write"
ATOM_GRANT_ADMIN = "admin"


class Circle(Base):
    """
    Per-user circle configuration: dimension definitions + default capture policy.

    One row per user that has circles enabled. The dimension_config is a JSON
    blob that defines the access dimensions for this user's circles (e.g.,
    {"tier": {"shape": "ladder", "values": [...]}}). For households, only
    the 'tier' dimension is configured; for enterprise deployments (per Reva),
    additional 'tenant' or 'project' dimensions can be added.

    The default_capture_policy says what circle membership new atoms are
    captured at by default for THIS user — privacy-positive default is
    {"tier": 0} (self).
    """
    __tablename__ = "circles"

    owner_user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    dimension_config = Column(JSON, nullable=False)
    default_capture_policy = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    owner = relationship("User", foreign_keys=[owner_user_id])


class CircleMembership(Base):
    """
    Per-user-per-dimension membership entry in another user's circles.

    Generalized for F-Generalize: dimension is 'tier' for ladder access (depth-
    based: self/trusted/household/extended/public) or 'tenant'/'project' for
    orthogonal-set access (multi-tenant SaaS, matrix orgs).

    Composite PK on (circle_owner, member, dimension) lets a member be in
    multiple dimensions of the same owner's circles simultaneously
    (e.g., dimension='tier' value=2 AND dimension='project' value='falcon').
    """
    __tablename__ = "circle_memberships"

    circle_owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    member_user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    dimension = Column(String(32), primary_key=True)  # 'tier' | 'tenant' | 'project'
    value = Column(JSON, nullable=False)  # int for ladder, str for set
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("idx_memberships_member", "member_user_id", "circle_owner_id"),
    )

    owner = relationship("User", foreign_keys=[circle_owner_id])
    member = relationship("User", foreign_keys=[member_user_id])
    granter = relationship("User", foreign_keys=[granted_by])


class Atom(Base):
    """
    Polymorphic registry: one row per piece of information that wears a circle.

    Acts as the unified identity layer over heterogeneous source tables
    (document_chunks, kg_entities, kg_relations, conversation_memories).
    Source rows carry a denormalized circle_tier for SQL filter performance,
    but `atoms.policy` is the canonical access policy.

    UUID atom_id is stored as String(36) for portable cross-dialect storage
    (sqlite test harness + postgres production).

    Source-row writers MUST go through AtomService.upsert_atom — direct
    INSERTs to source tables are forbidden by code review and a CI lint.
    """
    __tablename__ = "atoms"

    atom_id = Column(String(36), primary_key=True)
    atom_type = Column(String(32), nullable=False, index=True)
    source_table = Column(String(64), nullable=False)
    source_id = Column(String(64), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    policy = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("atom_type", "source_table", "source_id", name="uq_atoms_source"),
        Index("idx_atoms_owner", "owner_user_id"),
    )

    owner = relationship("User", foreign_keys=[owner_user_id])
    explicit_grants = relationship(
        "AtomExplicitGrant",
        back_populates="atom",
        cascade="all, delete-orphan",
    )


class AtomExplicitGrant(Base):
    """
    Per-resource exception grant — subsumes the legacy KBPermission table.

    Applied alongside circle-tier access via MAX-permissive semantics
    (see CircleResolver.can_access_atom): an asker has access to an atom if
    EITHER an explicit grant exists for them OR their tier reaches the atom's
    circle_tier. Solves the "share THIS one document with THIS one person
    without changing their tier" use case (the Notion/Drive/Dropbox pattern).
    """
    __tablename__ = "atom_explicit_grants"

    atom_id = Column(String(36), ForeignKey("atoms.atom_id", ondelete="CASCADE"), primary_key=True)
    granted_to_user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    permission_level = Column(String(16), nullable=False, default=ATOM_GRANT_READ)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("idx_grants_grantee", "granted_to_user_id"),
    )

    atom = relationship("Atom", back_populates="explicit_grants")
    grantee = relationship("User", foreign_keys=[granted_to_user_id])
    granter = relationship("User", foreign_keys=[granted_by])


# ---------------------------------------------------------------------------
# Wissensbasis longitudinal substrate (platform-level provenance primitives)
# ---------------------------------------------------------------------------


class WBFieldProvenance(Base):
    """
    Snapshot-at-observation row pinning the exact value an external system
    returned for one field at one fetch time.

    Substrate for BaFin audit replay ("what did we know about REL-100 six
    months ago"). FK uses ON DELETE CASCADE; the legal_hold discrimination
    is enforced in Python by ``services/atom_purge_service.py``, which
    copies legal_hold=TRUE rows to ``WBFieldProvenanceArchive`` before
    issuing the atoms DELETE.

      - legal_hold = TRUE  → archived before atom purge (BaFin audit)
      - legal_hold = FALSE → deleted by CASCADE with the atom (GDPR Art. 17)

    Direct ``DELETE FROM atoms`` calls bypass this discrimination and are
    blocked by the lint at ``tests/backend/test_no_direct_atom_delete.py``.

    Writers go through Reva's truth-engine accumulator; the agent's
    ``on_pre_agent_context`` hook starts a ContextVar collector, every
    tool result extracts ``FieldProvenance`` records via the
    ``compact_mcp_result`` hook, and ``on_post_agent`` flushes them as
    a single batched INSERT in a background task (fire-and-forget — see
    ``wb_snapshot_writes_total`` Prometheus counter for completeness
    measurement).
    """
    __tablename__ = "wb_field_provenance"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    atom_id = Column(String(36), ForeignKey("atoms.atom_id", ondelete="CASCADE"), nullable=True)
    source_type = Column(String(32), nullable=False)
    source_id = Column(String(512), nullable=False)
    field_path = Column(String(256), nullable=False)
    snapshot_value_json = Column(JSON, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False)
    req_id = Column(String(8), nullable=True)
    legal_hold = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("idx_wb_fp_lookup", "source_type", "source_id", "field_path"),
        Index("idx_wb_fp_atom_id", "atom_id"),
    )


class WBFieldProvenanceArchive(Base):
    """
    Append-only audit archive for snapshots that outlive their atom.

    Populated by ``services/atom_purge_service.py`` before it issues a
    ``DELETE FROM atoms``: legal_hold=TRUE rows in ``wb_field_provenance``
    are copied here (atom_id stripped — the atom is about to be gone),
    then the atom DELETE cascades through and wipes the live table.

    The archive survives until a future legal-hold-release path
    (out of scope; ships when BaFin retention windows expire).
    """
    __tablename__ = "wb_field_provenance_archive"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    original_atom_id = Column(String(36), nullable=True)
    source_type = Column(String(32), nullable=False)
    source_id = Column(String(512), nullable=False)
    field_path = Column(String(256), nullable=False)
    snapshot_value_json = Column(JSON, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False)
    req_id = Column(String(8), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=False)
    archive_reason = Column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_wb_fpa_lookup", "source_type", "source_id", "field_path"),
    )


class WBEventLog(Base):
    """
    Ordered event stream extracted from tool results that carry
    activity logs / phase histories.

    Substrate for vN+1 process-conformance evaluation: comparing actual
    release execution against an idealized markdown model. Ships empty
    in this sprint — ingestion writers land alongside the conformance
    evaluator in vN+1.
    """
    __tablename__ = "wb_event_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_type = Column(String(32), nullable=False)
    source_id = Column(String(512), nullable=False)
    event_type = Column(String(64), nullable=False)
    event_at = Column(DateTime(timezone=True), nullable=False)
    payload_json = Column(JSON, nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    req_id = Column(String(8), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_id", "event_type", "event_at",
            name="uq_wb_el_dedup",
        ),
        Index("idx_wb_el_source_order", "source_type", "source_id", "event_at"),
    )


class WBRetrospectiveAnnotation(Base):
    """
    Per-atom retrospective annotation ("what went well", "improvement",
    "blocker", "followup").

    Substrate for vN+1 retrospective aggregation. Ships empty — capture
    surface (UI + agent skill) lands alongside the aggregator. Cascades
    with atom purge: retrospective notes are non-audit, can be discarded
    on GDPR Art. 17 deletion.
    """
    __tablename__ = "wb_retrospective_annotation"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    atom_id = Column(String(36), ForeignKey("atoms.atom_id", ondelete="CASCADE"), nullable=False)
    annotation_key = Column(String(64), nullable=False)
    annotation_value = Column(Text, nullable=False)
    author_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("idx_wb_ra_atom", "atom_id"),
    )


class PeerUser(Base):
    """
    A paired remote Renfield peer — the asker-side record of another
    Renfield we can federate queries to.

    One row per (local_user, remote_pubkey). The remote pubkey is the
    stable identifier; display_name is cosmetic (set by the local user,
    changeable). transport_config carries {endpoint_url, transport,
    tls_fingerprint, relay_via}. revoked_at is set when the local user
    unpairs — the row stays for audit, but federated queries to it return
    401 afterwards.

    Design ref: second-brain-circles v2 pairing protocol, § Pairing
    Handshake State Machine. F2 of the federation lanes.
    """
    __tablename__ = "peer_users"

    id = Column(Integer, primary_key=True, index=True)
    circle_owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # 32-byte Ed25519 pubkey, stored as 64-char hex for SQL-friendliness.
    remote_pubkey = Column(String(64), nullable=False)
    remote_display_name = Column(String(255), nullable=False)
    # The remote's user_id on their own Renfield — cosmetic; real identity
    # is the pubkey. Useful for display ("Mom @ mom's-renfield").
    remote_user_id = Column(Integer, nullable=True)

    # {endpoint_url, transport, tls_fingerprint, relay_via}
    transport_config = Column(JSON, nullable=False, default=dict)

    paired_at = Column(DateTime, default=_utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        # Same local user can't pair twice with the same remote pubkey.
        # Re-pairing after revocation goes through unrevoke or delete+create.
        Index("uq_peer_users_owner_pubkey", "circle_owner_id", "remote_pubkey", unique=True),
        # Verify-signature lookups pivot on remote_pubkey across all owners.
        Index("idx_peer_users_pubkey", "remote_pubkey"),
    )

    owner = relationship("User", foreign_keys=[circle_owner_id])


class FederationQueryLog(Base):
    """
    Asker-side audit row for each federated query.

    One row per `FederationQueryAsker.query_peer` lifecycle: initiate →
    poll → finalize. Written by `_execute_federation_streaming` in
    mcp_client after the asker's terminal yield, so revoked peers,
    HTTP errors, and signature failures are all captured.

    Privacy: rows are strictly scoped to `user_id` (the asker). The
    responder has no visibility into this log — responder-side audit
    (who asked me) is a separate future feature. `query_text` and
    `answer_excerpt` are stored because a user-facing "what did I ask"
    feed needs them; both are truncated at write to bound row size.

    Retention: a lifecycle task prunes rows older than
    `FEDERATION_AUDIT_RETENTION_DAYS` (default 90). `peer_user_id` is
    nullable + FK-ON-DELETE-SET-NULL so unpairing doesn't cascade-delete
    historical queries; `peer_pubkey_snapshot` + `peer_display_name_snapshot`
    preserve who we asked even after the peer_users row is gone.

    Design ref: F4d of the v2 federation lanes.
    """
    __tablename__ = "federation_query_log"

    id = Column(Integer, primary_key=True, index=True)
    # No single-column index on user_id — the composite
    # `idx_fed_audit_user_initiated` covers `WHERE user_id = ?` queries
    # via the leading-column rule.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Nullable because peer_users can be deleted while audit survives.
    peer_user_id = Column(
        Integer, ForeignKey("peer_users.id", ondelete="SET NULL"), nullable=True,
    )
    # Snapshots — the peer's identity AT THE TIME of the query. If the
    # display name changes later or the row is deleted, this still shows
    # what the user saw when they asked.
    peer_pubkey_snapshot = Column(String(64), nullable=False)
    peer_display_name_snapshot = Column(String(255), nullable=False)

    # The asker's own request_id (uuid-ish string from /initiate).
    request_id = Column(String(64), nullable=True, index=True)

    # The user's question. Truncated at write to MAX_QUERY_TEXT_LEN chars.
    query_text = Column(Text, nullable=False)

    initiated_at = Column(DateTime, default=_utcnow, nullable=False)
    finalized_at = Column(DateTime, nullable=True)

    # Locked vocabulary: success | expired | failed | unknown. Wider than
    # FEDERATION_PROGRESS_LABELS on purpose — this captures the asker's
    # terminal determination, not the responder's progress label.
    final_status = Column(String(16), nullable=False)
    # Whether the responder's Ed25519 signature AND pair-anchor both
    # validated. False on terminal paths that never reached verification
    # (HTTP error, revoked peer, timeout) as well as on explicit failure.
    verified_signature = Column(Boolean, nullable=False, default=False)

    # Truncated final answer for at-a-glance display. None on failure paths.
    answer_excerpt = Column(Text, nullable=True)
    # Error text on non-success paths. None on success.
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        # Primary list query: "my queries, newest first".
        Index("idx_fed_audit_user_initiated", "user_id", "initiated_at"),
        # Per-peer filter: "show me everything I've asked Mom".
        Index("idx_fed_audit_user_peer", "user_id", "peer_pubkey_snapshot"),
    )

    user = relationship("User", foreign_keys=[user_id])
    peer = relationship("PeerUser", foreign_keys=[peer_user_id])


# ==========================================================================
# Backwards-compat re-exports from ha_glue.models.database
# ==========================================================================
#
# Phase 1 Week 1 — the HA-specific models were moved to
# `ha_glue/models/database.py` to establish a clean platform vs ha-glue
# boundary for the open-source extraction. These re-exports keep the
# legacy `from models.database import Room` import path working so
# consumer files (api/routes/rooms.py, services/presence_service.py,
# etc.) don't need to change in the same commit.
#
# TODO(phase1-week4): remove these re-exports once every consumer has
# migrated to `from ha_glue.models.database import ...` AND the CI lint
# rule that forbids platform → ha_glue imports is in place. See
# `docs/architecture/renfield-platform-boundary.md` in the parent Reva
# repo for the rollout plan.
#
# Implementation note — module-level `__getattr__` instead of a tail
# try/except block. The earlier shape of this file used a top-level
# `from ha_glue.models.database import Room, ...` wrapped in
# try/except. That has a circular-init failure mode: if a consumer
# imports `from ha_glue.models.database import X` BEFORE anything has
# touched `models.database`, then ha_glue.models.database starts
# loading, hits its own `from models.database import Base`, models.database
# starts loading, hits the tail re-export, tries to import from a
# half-loaded ha_glue.models.database, raises ImportError on the
# missing class, the except swallows it, and models.database finishes
# loading WITHOUT the re-exported names. Subsequent
# `from models.database import Room` then fails.
#
# Module-level `__getattr__` (PEP 562) avoids this entirely. The
# import from ha_glue.models.database happens lazily, on the first
# attribute access — by which time both modules are fully loaded and
# there's no partial-init state to trip over.
#
# Platform-only deployments (no ha_glue): a missing `ha_glue` package
# raises a clean `ModuleNotFoundError` at the consumer's import site,
# clearly naming the missing package. No silent failures.

_HA_GLUE_REEXPORT_NAMES = frozenset({
    "DEFAULT_CAPABILITIES",
    "DEVICE_TYPE_SATELLITE",
    "DEVICE_TYPE_WEB_BROWSER",
    "DEVICE_TYPE_WEB_KIOSK",
    "DEVICE_TYPE_WEB_PANEL",
    "DEVICE_TYPE_WEB_TABLET",
    "DEVICE_TYPES",
    "OUTPUT_TYPE_AUDIO",
    "OUTPUT_TYPE_VISUAL",
    "OUTPUT_TYPES",
    "CameraEvent",
    "HomeAssistantEntity",
    "PaperlessAuditResult",
    "PresenceEvent",
    "RadioFavorite",
    "Room",
    "RoomDevice",
    "RoomOutputDevice",
    "RoomSatellite",
    "UserBleDevice",
})


def __getattr__(name: str):
    """Lazily re-export ha-glue model classes for backwards compatibility.

    Triggered only when a caller does `from models.database import X`
    or `models.database.X` for a name that isn't already in the module
    namespace. Defers the `from ha_glue.models.database import ...`
    until both modules are fully loaded, sidestepping the partial-init
    cycle that a tail-of-file try/except produces.
    """
    if name not in _HA_GLUE_REEXPORT_NAMES:
        raise AttributeError(f"module 'models.database' has no attribute {name!r}")
    from ha_glue.models import database as _hg
    try:
        return getattr(_hg, name)
    except AttributeError as exc:
        # ha_glue is loaded but the symbol isn't there — propagate as
        # AttributeError, not as a misleading "package missing" error.
        raise AttributeError(
            f"models.database compat re-export: name {name!r} not found in "
            f"ha_glue.models.database (module loaded but symbol missing)"
        ) from exc
