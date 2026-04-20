"""
Atom types — concrete dataclasses + payload TypedDicts for the circles v1
unified-atom abstraction.

Atom IDs are UUID4 (per eng-review failure-mode finding: must be UUID4 to
prevent collision in federated request_id minting). Stored as 36-char string
for cross-dialect portability between PostgreSQL (production) and SQLite
(test harness).

Per source type the payload shape differs (TypedDicts below). Pre-fetched
search results return AtomMatch (atom + retrieval score + snippet + rank).
Federation provenance returns Provenance (display label only; no chunk text
on the wire — see Provenance.redacted_for_remote).

NAMING CONVENTIONS (locked per PR #402 review OPTIONAL #14):

  circle_tier        DB COLUMN name on source tables (document_chunks.circle_tier,
                     kg_entities.circle_tier, etc.). The denormalized integer
                     copy of policy["tier"] used for SQL filter pushdown.
                     Always integer 0..N where lower = more private.

  policy["tier"]     JSON KEY inside the dimension-agnostic atoms.policy column.
                     Always integer 0..N. The canonical access value; the
                     `circle_tier` column is just a denormalized copy.

  Atom.tier          DATACLASS PROPERTY on the Atom dataclass — convenience
                     accessor that returns int(self.policy.get("tier", 0)).

  AtomResponse.tier  API RESPONSE FIELD — same int as Atom.tier, exposed
                     alongside the full `policy` dict for client convenience.

  max_visible_tier   PARAMETER NAME on AtomStore.query — the deepest tier
                     index the asker can reach in the relevant atom owner's
                     circles (computed by CircleResolver.get_max_visible_tier).

The word "tier" alone always refers to the integer access value (regardless
of context: DB column, JSON key, dataclass property, parameter, or response
field). The word "circle_tier" specifically refers to the denormalized DB
column. There is no `tier_index` anywhere in the v1 code.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, TypedDict


# =============================================================================
# Per-source payload shapes
# =============================================================================


class AtomPayloadKBChunk(TypedDict, total=False):
    """Document-chunk payload (atom_type='kb_chunk', source_table='document_chunks')."""
    chunk_id: int
    document_id: int
    content: str
    chunk_index: int
    page_number: int | None
    section_title: str | None
    chunk_type: str
    parent_chunk_id: int | None
    document_filename: str
    document_title: str | None


class AtomPayloadKGNode(TypedDict, total=False):
    """KG entity payload (atom_type='kg_node', source_table='kg_entities')."""
    entity_id: int
    name: str
    entity_type: str  # person, place, organization, thing, event, concept
    description: str | None
    mention_count: int


class AtomPayloadKGEdge(TypedDict, total=False):
    """KG relation payload (atom_type='kg_edge', source_table='kg_relations')."""
    relation_id: int
    subject_id: int
    subject_name: str
    predicate: str
    object_id: int
    object_name: str
    confidence: float


class AtomPayloadConversationMemory(TypedDict, total=False):
    """Conversation memory payload (atom_type='conversation_memory')."""
    memory_id: int
    content: str
    category: str  # preference / fact / instruction / context / procedural
    importance: float
    confidence: float
    access_count: int
    source: str  # user_stated / llm_inferred / system_confirmed


# =============================================================================
# Core dataclasses
# =============================================================================


@dataclass(frozen=True)
class Atom:
    """
    Unified atom — one piece of information that wears a circle policy.

    The `policy` JSON shape depends on the deployment's dimension_config:
      Home (default):   {"tier": int}                   — depth-ladder access
      Multi-tenant:     {"tier": int, "tenant": str}    — ladder + set membership
      Project-matrix:   {"tier": int, "project": str}   — ladder + set membership
      Combined:         {"tier": int, "tenant": str, "project": str}

    The `payload` shape depends on atom_type — see AtomPayload* TypedDicts.

    IMMUTABILITY NOTE (per PR #402 review OPTIONAL #17):
    @dataclass(frozen=True) prevents reassignment of attributes (so you can't do
    `atom.policy = {...}`), but the dict instances stored in `policy` and `payload`
    are MUTABLE — `atom.policy["tier"] = 9` would silently mutate the dataclass.
    Use the `from_mutable` classmethod constructor (or build atoms via
    AtomService.upsert_atom) to get an Atom with read-only views over policy
    and payload. Direct construction with mutable dicts is allowed for
    convenience (most call sites build an atom and immediately persist it),
    but if you need defensive immutability across boundaries, use from_mutable.
    """
    atom_id: str            # UUID4 as 36-char string
    atom_type: str          # one of {'kb_chunk', 'kg_node', 'kg_edge', 'conversation_memory'}
    owner_user_id: int
    policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def tier(self) -> int:
        """Convenience: read the tier from policy. Returns 0 (self) if not present."""
        return int(self.policy.get("tier", 0))

    @classmethod
    def from_mutable(
        cls,
        atom_id: str,
        atom_type: str,
        owner_user_id: int,
        policy: dict[str, Any],
        created_at: datetime,
        updated_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> "Atom":
        """
        Construct an Atom from mutable input dicts, deep-copying policy and
        payload so subsequent mutations of the source dicts don't affect this
        atom. Use this when an atom crosses a trust boundary (e.g., AtomService
        builds it from a raw row and hands it off to a long-lived consumer).

        For internal builders that immediately persist the atom (write-and-discard
        pattern), the regular dataclass constructor is fine — sharing dict
        references is a non-issue when the atom is discarded after the persist.

        NOTE: deep-copy is the practical defense; true immutability via
        MappingProxyType wrapping isn't worth fighting the dataclass field
        type system for in v1. Don't mutate `atom.policy` or `atom.payload`
        after construction — use AtomService.update_tier for policy changes.
        """
        import copy
        return cls(
            atom_id=atom_id,
            atom_type=atom_type,
            owner_user_id=owner_user_id,
            policy=copy.deepcopy(policy),
            created_at=created_at,
            updated_at=updated_at,
            payload=copy.deepcopy(payload or {}),
        )


@dataclass(frozen=True)
class AtomMatch:
    """A retrieved atom + its retrieval-side metadata."""
    atom: Atom
    score: float            # retrieval relevance (0..1)
    snippet: str            # short text for display in result list
    rank: int               # ordinal in the result set (1-based)


@dataclass(frozen=True)
class Provenance:
    """
    Source attribution for a synthesized answer.

    Distinct from AtomMatch in that it's intentionally LIGHTWEIGHT — used in
    federated query responses where the responder must NOT leak source text
    on the wire (only synthesized answer + display label).

    Use redacted_for_remote() before serializing for federation.
    """
    atom_id: str
    atom_type: str
    display_label: str      # e.g., "from Granny's recipes (2024-03)"
    score: float

    def redacted_for_remote(self) -> "Provenance":
        """
        Returns a copy safe to ship over MCP federation.

        - atom_id replaced with a per-call random UUID4 (per PR #402 review
          OPTIONAL #15: a constant zero UUID would let receivers dedupe by
          atom_id across queries — actually worse than no redaction. Random
          UUID4 per call breaks the correlation entirely while keeping the
          shape valid for receivers expecting a UUID-formatted string).
        - atom_type stays (informational; reveals shape, not content).
        - display_label stays (intentionally human-readable, e.g., "from
          Granny's recipes" — no chunk text).
        - score rounded to 1 decimal to reduce inference bandwidth (defends
          against side-channel ranking inference).
        """
        import uuid as _uuid
        return replace(
            self,
            atom_id=str(_uuid.uuid4()),
            score=round(self.score, 1),
        )


# =============================================================================
# Access context (asker + memberships + dimension config)
# =============================================================================


@dataclass(frozen=True)
class DimensionSpec:
    """
    Per-dimension access shape.

    shape = 'ladder' (depth-ordered, e.g., self/trusted/household/extended/public)
    shape = 'set'    (orthogonal membership, e.g., tenant_id, project_id)
    """
    shape: str
    values: list[str] | None = None  # ordered values for ladder; None for set

    @property
    def public_index(self) -> int | None:
        """For ladder dimensions, the index that means 'visible to anyone'."""
        if self.shape != "ladder" or not self.values:
            return None
        return len(self.values) - 1


@dataclass(frozen=True)
class AccessContext:
    """
    Per-asker context for policy evaluation.

    Built once per query (not per atom) and passed to PolicyEvaluator.satisfies.

    `memberships` is keyed by atom owner: for each owner the asker has
    membership in, a dict of dimension -> value (already deserialized from
    circle_memberships.value JSON). Empty dict means "this asker is not in
    any of that owner's circles" (only public atoms accessible from that owner).
    """
    asker_id: int
    dimensions: dict[str, DimensionSpec]
    memberships: dict[int, dict[str, Any]]  # owner_id -> {dimension: value}
