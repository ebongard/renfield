"""
PaperlessMetadataExtractor — LLM-driven metadata extraction for Paperless-NGX uploads.

Reads a chat-attached document via Docling, fetches the user's current
Paperless taxonomy from the MCP server, asks the LLM to pick the best
metadata fields from that taxonomy (with worked examples baked into the
prompt), validates via pydantic + fuzzy-match + taxonomy-membership, and
returns a structured result the caller can feed into
``mcp.paperless.upload_document``.

Design reference:
    docs/design/paperless-llm-metadata.md

This is PR 2a of the feature: the atomic extraction unit, independently
testable, no dependency on the confirm flow. PR 2b wires this into the
``forward_attachment_to_paperless`` tool + cold-start-only confirm state
machine.

ASCII flow — single extraction call:

    extract(attachment_id, session_id, user_lang)
        │
        ▼
    load ChatUpload (session-scoped per #442)
        │
        ▼
    OCR / text-layer extraction via DocumentProcessor.extract_text_only
    (Docling HybridChunker + EasyOCR fallback; vision-model path is a
    PR 2b refinement once the agent-client wiring supports it)
        │
        ▼
    fetch_taxonomy(mcp_manager)
        │   calls mcp.paperless.list_* tools, prunes top-20 correspondents
        │   and top-20 tags by recency from the `/api/documents/?ordering=-modified`
        │   window provided by PR 1's MCP server.
        │
        ▼
    render prompt (paperless_metadata.yaml) with taxonomy + doc_text
        │
        ▼
    LLM call (settings.paperless_extraction_model || vision || chat)
        │
        ▼
    validate(response)
        │   1. pydantic parse
        │   2. rapidfuzz near-match against taxonomy (rewrites silently
        │      on one-candidate-within-threshold, flags ambiguous)
        │   3. strict membership check (drops misses that weren't
        │      flagged as new_entry_proposals)
        │   4. clamp created_date (today-10y ... today+1y)
        │   5. cap tags at 5
        │
        ▼
    ExtractionResult(metadata, proposals, confidence, source_text)
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from rapidfuzz.distance import Levenshtein

from models.database import ChatUpload
from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import (
    extract_response_content,
    get_classification_chat_kwargs,
    get_default_client,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Top-N cuts for taxonomy pruning. Full doc_types + storage_paths are
# always included; correspondents + tags are truncated by recency.
_TOP_CORRESPONDENTS = 20
_TOP_TAGS = 20

# Fuzzy-match thresholds. Levenshtein distance <= this many edits counts
# as a near-match. Short strings at distance 2 can match too-loosely,
# so we also cap at <= 20% of the longer string's length.
#
# Levenshtein alone doesn't catch corporate-suffix variations like
# "Stadtwerke Korschenbroich GmbH" → "Stadtwerke Korschenbroich" (5
# deletions, over threshold). _CORPORATE_SUFFIXES strips those
# suffixes before comparison so the design doc's motivating case
# actually works.
_FUZZY_MAX_DISTANCE = 2
_FUZZY_MAX_RATIO = 0.2

# Case-insensitive; matched at the tail after normalisation. Each entry
# is a standalone token that may appear at the end of a correspondent
# string, optionally followed by punctuation. The list covers the
# German + US/UK forms a household Paperless is likely to see; add more
# as needed.
_CORPORATE_SUFFIX_PATTERN = re.compile(
    r"\s*(?:"
    r"gmbh|ag|kg|kgaa|se|ohg|ug|mbh|e\.v\.|ev|"           # German
    r"inc\.?|llc\.?|llp\.?|ltd\.?|plc\.?|corp\.?|co\.?|&\s*co\.?|"  # US / UK
    r"s\.?\s*a\.?|s\.?\s*l\.?|s\.?\s*r\.?\s*l\.?|"         # Romance
    r"b\.?\s*v\.?|n\.?\s*v\.?"                              # Dutch
    r")\s*[.,]?\s*$",
    re.IGNORECASE,
)

# Per-field confidence gate for new_entry_proposals. Below this the LLM
# is too uncertain to justify surfacing a create-proposal to the user —
# drop the proposal silently. Matches the design doc's § Validation
# step 3 wording.
_PROPOSAL_CONFIDENCE_MIN = 0.6

# Taxonomy cache TTL (in memory, per-process). 10 min balances freshness
# with API-call cost. Invalidated on successful create_* via the MCP
# server's own cache flush; cross-pod consistency is a v2 concern.
_TAXONOMY_CACHE_TTL_S = 600

# OCR text char cap fed to the LLM. Enough for the first few pages of a
# typical document; longer is rarely informative and costs context budget.
_MAX_DOC_CHARS = 12_000

# created_date sanity clamps. Design § Validation step 4 says "10 years
# past, 1 year future." Computed fresh per call so tests that freeze
# the clock see the right window.
_DATE_PAST_YEARS = 10
_DATE_FUTURE_DAYS = 365


# ---------------------------------------------------------------------------
# Module-level taxonomy cache
# ---------------------------------------------------------------------------
#
# Lives at module scope (not on the extractor instance) so that
# per-request PaperlessMetadataExtractor instances share the warm
# cache. Typical request creates a fresh extractor → previously the
# cache reset every time; now the 10-min TTL actually works.
#
# Shape: {"fetched_at": float_epoch, "taxonomy": PaperlessTaxonomy}
# Empty dict == no cache yet.
_TAXONOMY_CACHE: dict[str, Any] = {}


def _invalidate_taxonomy_cache() -> None:
    """Flush the cache. Called when a create_* succeeds (the commit
    tool in PR 2b) so freshly-added entries show up in the next
    extraction without waiting for the TTL.
    """
    _TAXONOMY_CACHE.clear()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class NewEntryProposal(BaseModel):
    """Singleton proposal for a not-in-taxonomy value. Three new tags
    become three separate proposals, not one entry with a list."""
    field: Literal["correspondent", "document_type", "tag", "storage_path"]
    value: str
    reasoning: str


class PaperlessMetadata(BaseModel):
    """Output of the extractor.

    Every taxonomy field is either ``None`` (LLM couldn't decide or
    nothing matched) or a string guaranteed to be present in the user's
    live Paperless taxonomy (because the validator checks exactly that).
    ``new_entry_proposals`` carries LLM's confidence-gated suggestions
    for values it thinks deserve a new taxonomy entry.
    """
    title: str | None = None
    correspondent: str | None = None
    document_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    storage_path: str | None = None
    created_date: date | None = None
    confidence: dict[str, float] = Field(default_factory=dict)
    new_entry_proposals: list[NewEntryProposal] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """What the caller (PR 2b's forward tool) sees.

    On full success: ``metadata`` holds validated + fuzzy-normalised
    values, ``doc_text`` carries the OCR text used (so the caller can
    persist it into ``paperless_extraction_examples`` once the user
    confirms), ``error`` is ``None``.

    On any failure path: ``metadata`` is an empty ``PaperlessMetadata``
    and ``error`` carries a short string the caller can surface.
    Partial success (some fields dropped, some kept) still returns
    ``error=None`` — the caller uses whatever's filled.
    """
    metadata: PaperlessMetadata
    doc_text: str = ""
    error: str | None = None


class PaperlessTaxonomy(BaseModel):
    """Snapshot of the user's Paperless taxonomy at extraction time."""
    correspondents: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    storage_paths: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers — text normalisation, fuzzy matching, taxonomy pruning
# ---------------------------------------------------------------------------


def _normalise(value: str) -> str:
    """Casefold + whitespace-strip + Unicode NFKC.

    Applied to both LLM output and taxonomy entries before the fuzzy
    comparison. Catches trivial variations ("Stadtwerke  Köln" vs
    "stadtwerke köln") that would otherwise blow past the edit-distance
    budget on Umlauts or spacing.
    """
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _strip_corporate_suffix(value: str) -> str:
    """Drop one trailing corporate suffix (GmbH, Inc, LLC, …) from a
    normalised string.

    The LLM often emits "Stadtwerke Korschenbroich GmbH" when the
    taxonomy has "Stadtwerke Korschenbroich" — a Levenshtein distance
    of 5 that would blow past the fuzzy threshold. Stripping the
    suffix before comparison brings distance to 0, trivial hit.

    Only strips one suffix (non-greedy tail) and only when it's the
    last token. "Foo GmbH Bar" stays unchanged (legitimately
    different entity).
    """
    if not value:
        return value
    return _CORPORATE_SUFFIX_PATTERN.sub("", value).strip()


def _fuzzy_match(llm_value: str, taxonomy: list[str]) -> str | None:
    """Return the canonical taxonomy entry near-matching ``llm_value``.

    Three passes, each resolving to the canonical spelling or falling
    through to the next:

    1. Exact match after normalisation.
    2. Exact match after corporate-suffix stripping on both sides
       (catches "Stadtwerke Korschenbroich GmbH" → "Stadtwerke
       Korschenbroich", the design doc's canonical case).
    3. Levenshtein distance ≤ 2 AND edit-ratio ≤ 0.2 against every
       normalised taxonomy entry. Exactly one candidate within
       threshold → canonical. Zero or multiple (ambiguous) → None.

    Rationale for the two thresholds on pass 3: a raw Levenshtein of 2
    would match "Bob" to "Bo" (33% distance) which isn't a near-miss,
    it's a different word. The ratio cap prevents tiny-string false
    positives while leaving longer strings room to absorb small typos.
    """
    if not llm_value or not taxonomy:
        return None

    normalised_llm = _normalise(llm_value)
    if not normalised_llm:
        return None

    # Pass 1 — exact after casefold + NFKC. Catches the common case
    # where the LLM emits a taxonomy entry verbatim.
    for entry in taxonomy:
        if _normalise(entry) == normalised_llm:
            return entry

    # Pass 2 — exact match after corporate-suffix stripping. The
    # stripping is symmetric: drop suffixes from both sides so
    # "Stadtwerke GmbH" in the taxonomy still matches "Stadtwerke"
    # from the LLM and vice versa.
    stripped_llm = _strip_corporate_suffix(normalised_llm)
    if stripped_llm and stripped_llm != normalised_llm:
        for entry in taxonomy:
            stripped_entry = _strip_corporate_suffix(_normalise(entry))
            if stripped_entry == stripped_llm:
                return entry
    # Also try: LLM emits "Stadtwerke", taxonomy has "Stadtwerke GmbH".
    # Suffix strip on the taxonomy side catches this.
    for entry in taxonomy:
        normalised_entry = _normalise(entry)
        stripped_entry = _strip_corporate_suffix(normalised_entry)
        if stripped_entry and stripped_entry != normalised_entry:
            if stripped_entry == normalised_llm:
                return entry

    # Fuzzy pass with both absolute and ratio constraints.
    candidates: list[str] = []
    for entry in taxonomy:
        normalised_entry = _normalise(entry)
        if not normalised_entry:
            continue
        distance = Levenshtein.distance(normalised_llm, normalised_entry)
        if distance > _FUZZY_MAX_DISTANCE:
            continue
        max_len = max(len(normalised_llm), len(normalised_entry))
        if max_len == 0:
            continue
        ratio = distance / max_len
        if ratio > _FUZZY_MAX_RATIO:
            continue
        candidates.append(entry)

    if len(candidates) == 1:
        return candidates[0]
    # Zero candidates OR ambiguous multi-match → caller handles as
    # proposal (ambiguity surfaces in the confirm UI).
    return None


def prune_taxonomy(
    *,
    correspondents: list[str],
    document_types: list[str],
    tags: list[str],
    storage_paths: list[str],
    recent_correspondent_ids: list[str] | None = None,
    recent_tag_ids: list[str] | None = None,
    top_correspondents: int = _TOP_CORRESPONDENTS,
    top_tags: int = _TOP_TAGS,
) -> PaperlessTaxonomy:
    """Prune long taxonomy lists so the prompt stays within the local
    model's reliable-attention window.

    - correspondents: keep ``top_correspondents`` by recency-of-use.
      ``recent_correspondent_ids`` is an ordered list from the MCP
      server's most-recently-modified-documents query (PR 1 exposes
      this via the ordering of items returned from its helpers); the
      first N unique ones win.
    - tags: same pattern.
    - document_types, storage_paths: included in full. Typical
      household Paperless has < 30 of each and they're small strings,
      so pruning doesn't save meaningful tokens.

    When ``recent_*_ids`` is ``None`` (cold start, no usage signal
    yet), the first ``top_*`` entries of the raw list are kept in their
    natural order — safe default for fresh installs.
    """
    def _pick_by_recency(all_entries: list[str], recent_ids: list[str] | None, cap: int) -> list[str]:
        if not recent_ids:
            return all_entries[:cap]
        seen: set[str] = set()
        ordered: list[str] = []
        for entry in recent_ids:
            if entry in seen or entry not in all_entries:
                continue
            seen.add(entry)
            ordered.append(entry)
            if len(ordered) >= cap:
                break
        # Pad with any remaining entries that didn't show up in recency
        # — keeps the prompt focused on actually-seen entries while not
        # dropping rare-but-present ones entirely if the cap has room.
        if len(ordered) < cap:
            for entry in all_entries:
                if entry in seen:
                    continue
                ordered.append(entry)
                seen.add(entry)
                if len(ordered) >= cap:
                    break
        return ordered

    return PaperlessTaxonomy(
        correspondents=_pick_by_recency(correspondents, recent_correspondent_ids, top_correspondents),
        document_types=document_types,
        tags=_pick_by_recency(tags, recent_tag_ids, top_tags),
        storage_paths=storage_paths,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _date_min() -> date:
    """Computed fresh per call so tests that freeze the clock see the
    right floor. Design § Validation step 4: ten years past."""
    return date.today() - timedelta(days=_DATE_PAST_YEARS * 365)


def _date_max() -> date:
    """Computed fresh per call so tests that freeze the clock see the
    right ceiling. Design § Validation step 4: one year future."""
    return date.today() + timedelta(days=_DATE_FUTURE_DAYS)


def validate_extraction(
    raw_output: dict,
    taxonomy: PaperlessTaxonomy,
) -> PaperlessMetadata:
    """Full validation pipeline. Caller passes the parsed LLM JSON dict.

    1. Pydantic shape check — malformed → ValidationError raises up.
    2. Fuzzy match against each taxonomy dimension. One-candidate wins
       silently (rewrites llm value to canonical); zero or ambiguous
       drops the field to ``None`` — unless the LLM simultaneously
       emitted a ``new_entry_proposal`` for that field, in which case
       the proposal survives for user review.
    3. Strict taxonomy membership — after fuzzy, assert the value is in
       the list. Defence-in-depth; should never drop here.
    4. ``created_date`` clamped to [today-10y, today+1y].
    5. ``tags`` truncated at 5.
    """
    # Step 1 — parse. Any shape error raises; caller catches.
    try:
        metadata = PaperlessMetadata(**raw_output)
    except ValidationError as exc:
        # Add the raw dict to the exception note so debugging shows what
        # the LLM actually emitted. pydantic's own error is verbose
        # enough that appending is more useful than replacing.
        raise ValueError(f"Malformed LLM output: {exc}") from exc

    # Confidence-gate the proposals first. Design § Validation step 3:
    # proposals below _PROPOSAL_CONFIDENCE_MIN (0.6) are dropped
    # silently — the LLM is too uncertain to justify surfacing a
    # create-proposal to the user. Check confidence per-field: a low
    # confidence on the FIELD (e.g. confidence.correspondent = 0.2)
    # drops any proposals for that field.
    high_confidence_proposals: list[NewEntryProposal] = []
    for proposal in metadata.new_entry_proposals:
        # The LLM emits confidence keyed by field name (singular for
        # fields, plural "tags" for the list). Proposal.field is always
        # singular ("correspondent", "document_type", "tag",
        # "storage_path") per the Literal typing.
        confidence_key = proposal.field
        # For tag proposals, the LLM's confidence is usually keyed
        # "tags" in its response. Accept either.
        confidence = metadata.confidence.get(confidence_key)
        if confidence is None and proposal.field == "tag":
            confidence = metadata.confidence.get("tags")
        if confidence is None:
            # LLM didn't emit confidence for this field. Be permissive
            # — drop-silent would lose useful signal when the model
            # forgets the confidence block. Let the proposal survive.
            high_confidence_proposals.append(proposal)
            continue
        if confidence >= _PROPOSAL_CONFIDENCE_MIN:
            high_confidence_proposals.append(proposal)
        else:
            logger.debug(
                "Dropping low-confidence proposal (%.2f < %.2f): %s=%r",
                confidence, _PROPOSAL_CONFIDENCE_MIN,
                proposal.field, proposal.value,
            )
    metadata.new_entry_proposals = high_confidence_proposals

    # Group the surviving proposals by field for quick lookup during
    # fuzzy fallback — if the LLM flagged "correspondent" as proposal,
    # we don't need to silently drop the field value too.
    proposed_fields = {p.field for p in metadata.new_entry_proposals}

    # Step 2 + 3 — fuzzy + strict membership.
    metadata.correspondent = _validate_singleton_field(
        metadata.correspondent,
        taxonomy.correspondents,
        field_name="correspondent",
        has_proposal=("correspondent" in proposed_fields),
    )
    metadata.document_type = _validate_singleton_field(
        metadata.document_type,
        taxonomy.document_types,
        field_name="document_type",
        has_proposal=("document_type" in proposed_fields),
    )
    metadata.storage_path = _validate_singleton_field(
        metadata.storage_path,
        taxonomy.storage_paths,
        field_name="storage_path",
        has_proposal=("storage_path" in proposed_fields),
    )

    # Tags are list-valued; each element goes through fuzzy-then-strict
    # independently, misses are dropped silently (proposals work at
    # field-not-element granularity — a tag proposal covers one missing
    # tag, not the whole list).
    validated_tags: list[str] = []
    for tag in metadata.tags:
        canonical = _fuzzy_match(tag, taxonomy.tags)
        if canonical is not None:
            validated_tags.append(canonical)
        else:
            logger.debug("Dropping tag not in taxonomy: %r", tag)
    metadata.tags = validated_tags[:5]  # Step 5 — cap

    # Step 4 — date clamps. Both bounds are computed fresh per call so
    # tests with a frozen clock see the window they expect.
    if metadata.created_date is not None:
        if metadata.created_date < _date_min() or metadata.created_date > _date_max():
            logger.warning(
                "Dropping created_date out of range: %s",
                metadata.created_date,
            )
            metadata.created_date = None

    return metadata


def _validate_singleton_field(
    value: str | None,
    taxonomy: list[str],
    *,
    field_name: str,
    has_proposal: bool,
) -> str | None:
    """Apply fuzzy+strict validation to one taxonomy-constrained field.

    If the LLM also emitted a new_entry_proposal for this field, we keep
    ``None`` as the field value (the proposal carries the intent).
    Otherwise we log the dropped value for later debugging.
    """
    if value is None:
        return None

    canonical = _fuzzy_match(value, taxonomy)
    if canonical is not None:
        return canonical

    # Not in taxonomy. If there's a matching proposal, that's intended —
    # silently return None (the caller surfaces the proposal in the
    # confirm UI). If not, the LLM hallucinated; log for debugging.
    if not has_proposal:
        logger.debug(
            "Dropping %s not in taxonomy (no proposal): %r",
            field_name,
            value,
        )
    return None


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_prompt(
    *,
    doc_text: str,
    taxonomy: PaperlessTaxonomy,
    lang: str = "de",
) -> tuple[str, str]:
    """Build (system, user) messages for the LLM call.

    Taxonomy lists render as comma-separated inline strings; the prompt
    template doesn't try to pretty-print them because LLMs tolerate
    either shape and CSV keeps the token count down.
    """
    system = prompt_manager.get(
        "paperless_metadata", "system",
        default="Extract Paperless metadata.", lang=lang,
    )
    user = prompt_manager.get(
        "paperless_metadata", "user",
        default="{document_text}",
        lang=lang,
        correspondents=", ".join(taxonomy.correspondents) or "(none)",
        document_types=", ".join(taxonomy.document_types) or "(none)",
        tags=", ".join(taxonomy.tags) or "(none)",
        storage_paths=", ".join(taxonomy.storage_paths) or "(none)",
        document_text=doc_text[:_MAX_DOC_CHARS],
    )
    return system, user


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class PaperlessMetadataExtractor:
    """Orchestrates the full extraction pipeline for one attachment.

    Construct once per request with the MCP manager (for taxonomy
    fetches) and an LLM client (for the extraction call). Call
    ``extract(attachment_id, session_id, lang)`` to run the pipeline
    and get back an ``ExtractionResult``.

    Stateless across calls — no per-extractor cache; the taxonomy cache
    lives on the module level and can be shared across extractor
    instances within the same process.
    """

    def __init__(
        self,
        *,
        mcp_manager: Any = None,
        llm_client: Any = None,
        document_processor: Any = None,
    ):
        self.mcp_manager = mcp_manager
        self._llm_client = llm_client
        self._document_processor = document_processor
        # Taxonomy cache is at module level (_TAXONOMY_CACHE) so repeated
        # per-request extractor instances share the warm cache — the
        # 10-minute TTL only makes sense across instances.

    # -- Extraction entry point --

    async def extract(
        self,
        *,
        attachment_id: int,
        session_id: str | None,
        lang: str = "de",
    ) -> ExtractionResult:
        """Run the full pipeline on a ChatUpload and return structured
        metadata. Never raises on expected failure paths — sets
        ``error`` on the result instead.
        """
        # 1. Load the ChatUpload, honour the session-scoping guard from #442.
        upload = await self._load_upload(attachment_id, session_id)
        if upload is None:
            return ExtractionResult(
                metadata=PaperlessMetadata(),
                error=f"Attachment {attachment_id} not found",
            )

        # 2. Extract text (Docling; vision-model path is PR 2b refinement).
        doc_text = await self._extract_text(upload.file_path)
        if not doc_text:
            return ExtractionResult(
                metadata=PaperlessMetadata(),
                error="Konnte Dokument nicht lesen (OCR lieferte keinen Text).",
            )

        # 3. Fetch + prune taxonomy.
        taxonomy = await self._fetch_taxonomy()
        if taxonomy is None:
            # Paperless unreachable or MCP down. Treat as recoverable —
            # caller falls back to bare upload.
            return ExtractionResult(
                metadata=PaperlessMetadata(),
                doc_text=doc_text,
                error="Paperless-Taxonomie nicht erreichbar; Metadaten-Extraktion uebersprungen.",
            )

        # 4. Render prompt + call LLM.
        system, user = render_prompt(
            doc_text=doc_text, taxonomy=taxonomy, lang=lang,
        )
        try:
            raw = await self._call_llm(system, user)
        except Exception as exc:
            logger.warning("LLM call for metadata extraction failed: %s", exc)
            return ExtractionResult(
                metadata=PaperlessMetadata(),
                doc_text=doc_text,
                error=f"LLM-Extraktion fehlgeschlagen: {exc}",
            )

        parsed = _parse_llm_json(raw)
        if parsed is None:
            return ExtractionResult(
                metadata=PaperlessMetadata(),
                doc_text=doc_text,
                error="LLM lieferte keine gueltige JSON-Antwort.",
            )

        # 5. Validate + fuzzy-match + clamp.
        try:
            metadata = validate_extraction(parsed, taxonomy)
        except ValueError as exc:
            logger.warning("Metadata validation failed: %s", exc)
            return ExtractionResult(
                metadata=PaperlessMetadata(),
                doc_text=doc_text,
                error=f"Metadaten-Validierung fehlgeschlagen: {exc}",
            )

        return ExtractionResult(metadata=metadata, doc_text=doc_text, error=None)

    # -- Helpers (overridable for testing) --

    async def _load_upload(self, attachment_id: int, session_id: str | None):
        """Lookup with session-scoping (see #442).

        ``AsyncSessionLocal`` stays a local import because importing
        ``services.database`` at module level triggers engine creation,
        which forces every caller (including unit tests) to have a
        Postgres-ready env. Session-scoped fetch avoids that cost.
        """
        from sqlalchemy import select

        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            query = select(ChatUpload).where(ChatUpload.id == attachment_id)
            if session_id is not None:
                query = query.where(ChatUpload.session_id == session_id)
            result = await db.execute(query)
            upload = result.scalar_one_or_none()

        if upload is None:
            return None
        if not upload.file_path or not Path(upload.file_path).is_file():
            logger.warning("ChatUpload %s file missing on disk: %r", attachment_id, upload.file_path)
            return None
        return upload

    async def _extract_text(self, file_path: str) -> str:
        """Docling-based OCR / text-layer extraction.

        Vision-model path is deferred to PR 2b: it requires coordinating
        with the agent client to send image bytes + text, which the
        existing llm_client surface doesn't yet support cleanly. Docling
        alone covers typed documents (the 80% case) and falls back to
        EasyOCR for scans via the existing ``rag_ocr_auto_detect``
        settings on ``DocumentProcessor``.
        """
        if self._document_processor is None:
            from services.document_processor import DocumentProcessor
            self._document_processor = DocumentProcessor()
        text = await self._document_processor.extract_text_only(
            file_path, max_chars=_MAX_DOC_CHARS,
        )
        return text or ""

    async def _fetch_taxonomy(self) -> PaperlessTaxonomy | None:
        """Query the MCP server's list_* tools for the current taxonomy.

        Results are cached in-process for ``_TAXONOMY_CACHE_TTL_S``.
        Cache invalidation on successful ``create_*`` is a PR 2b concern
        (the commit tool clears the cache when it fires a create).
        """
        if self.mcp_manager is None:
            logger.warning("No MCP manager wired; cannot fetch Paperless taxonomy")
            return None

        now = time.time()
        cached = _TAXONOMY_CACHE.get("entry")
        if cached and (now - cached["fetched_at"]) < _TAXONOMY_CACHE_TTL_S:
            return cached["taxonomy"]

        # The MCP server exposes list_correspondents, list_document_types,
        # list_tags, list_storage_paths (v1.3.0+). Taxonomy cache on the
        # MCP server side is shared across tools — one round-trip per
        # dimension on a cold cache, zero on warm.
        try:
            correspondents = await self._list_via_mcp("list_correspondents")
            document_types = await self._list_via_mcp("list_document_types")
            tags = await self._list_via_mcp("list_tags")
            storage_paths = await self._list_via_mcp(
                "list_storage_paths", field="paths", value_key="path",
            )
        except Exception as exc:
            logger.warning("Taxonomy fetch failed: %s", exc)
            return None

        taxonomy = prune_taxonomy(
            correspondents=correspondents,
            document_types=document_types,
            tags=tags,
            storage_paths=storage_paths,
        )
        _TAXONOMY_CACHE["entry"] = {"fetched_at": now, "taxonomy": taxonomy}
        return taxonomy

    async def _list_via_mcp(
        self,
        tool_name: str,
        *,
        field: str = "items",
        value_key: str = "name",
    ) -> list[str]:
        """Thin wrapper: call ``mcp.paperless.{tool_name}`` and return
        the list of names.

        Requires ``renfield-mcp-paperless`` v1.3.0 or later, which
        exposes ``list_correspondents`` / ``list_document_types`` /
        ``list_tags`` / ``list_storage_paths`` as read-only wrappers
        around the MCP server's already-populated taxonomy caches.

        On any failure path (tool not found, transport error, JSON
        parse failure, unexpected payload shape), returns an empty
        list AND logs at WARNING so the "empty taxonomy in prod"
        failure mode is visible in logs rather than silently degraded.
        The extractor falls through with fewer dimensions rather than
        crashing the whole pipeline.
        """
        full_name = f"mcp.paperless.{tool_name}"
        try:
            result = await self.mcp_manager.execute_tool(full_name, {})
        except Exception as exc:
            logger.warning("MCP tool %s unreachable: %s", full_name, exc)
            return []

        if not result or not result.get("success"):
            # Distinguish "unknown tool" (MCP server too old) from
            # "tool ran but errored" so the ops signal is clear.
            err = (result or {}).get("message") or "no message"
            err_str = err if isinstance(err, str) else str(err)
            if "unknown" in err_str.lower() or "not found" in err_str.lower():
                logger.warning(
                    "MCP tool %s not available — is the MCP server at "
                    "v1.3.0 or later? (got: %s)",
                    full_name, err_str[:120],
                )
            else:
                logger.warning("MCP tool %s failed: %s", full_name, err_str[:120])
            return []

        inner_msg = result.get("message")
        payload: Any = None
        if isinstance(inner_msg, str):
            try:
                payload = json.loads(inner_msg)
            except json.JSONDecodeError:
                logger.warning(
                    "MCP tool %s returned non-JSON message: %r",
                    full_name, inner_msg[:120],
                )
                return []
        elif isinstance(inner_msg, dict):
            payload = inner_msg

        if not isinstance(payload, dict):
            logger.warning(
                "MCP tool %s returned unexpected payload shape: %s",
                full_name, type(payload).__name__,
            )
            return []

        items = payload.get(field) or []
        if not isinstance(items, list):
            logger.warning(
                "MCP tool %s field %r is not a list: %s",
                full_name, field, type(items).__name__,
            )
            return []

        names: list[str] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get(value_key)
                if isinstance(name, str):
                    names.append(name)
            elif isinstance(item, str):
                names.append(item)
        return names

    async def _call_llm(self, system: str, user: str) -> str:
        """Single LLM call with the classification kwargs (low
        temperature, deterministic). Returns raw response text.
        """
        model = (
            settings.paperless_extraction_model
            or settings.ollama_vision_model
            or settings.ollama_chat_model
        )
        if not model:
            raise RuntimeError("No extraction model configured")

        client = self._llm_client or get_default_client()
        classification_kwargs = get_classification_chat_kwargs(model)

        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options={"temperature": 0.1, "num_predict": 800},
            **classification_kwargs,
        )
        return extract_response_content(response) or ""


# ---------------------------------------------------------------------------
# JSON parsing — robust to markdown fences and small noise
# ---------------------------------------------------------------------------


def _parse_llm_json(raw: str) -> dict | None:
    """Parse an LLM response to a dict.

    Tolerates markdown code fences (```json ... ```) and leading/
    trailing prose around the JSON object. Returns ``None`` if nothing
    parseable is found — caller treats that as extraction failure and
    falls back to bare upload.
    """
    if not raw:
        return None
    text = raw.strip()

    # Strip code fences if present.
    if text.startswith("```"):
        # Remove opening fence line (```, ```json, etc.)
        newline_idx = text.find("\n")
        if newline_idx >= 0:
            text = text[newline_idx + 1:]
        # Remove closing fence
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Find the balanced JSON object. Use the first '{' through the
    # matching '}' — naive but sufficient for single-object responses,
    # and the prompt explicitly asks for one.
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last <= first:
        return None
    json_fragment = text[first:last + 1]

    try:
        parsed = json.loads(json_fragment)
    except json.JSONDecodeError as exc:
        logger.debug("Could not parse LLM JSON: %s | raw=%r", exc, raw[:200])
        return None

    if not isinstance(parsed, dict):
        return None

    # created_date often comes as a string from the LLM. Pydantic
    # handles str-to-date coercion, so pass through unchanged.
    return parsed
