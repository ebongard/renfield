"""
PaperlessMetadataExtractor — LLM-driven metadata extraction for Paperless-NGX uploads.

Reads a chat-attached document via Docling, asks the LLM to extract the
essential metadata directly from the document text (NOT constrained to
any taxonomy — the LLM's job is reading the doc, not knowing what's in
Paperless), then resolves each extracted value against the user's live
Paperless taxonomy on the server side. Returns a structured result the
caller feeds into ``mcp.paperless.upload_document``.

Why no taxonomy in the prompt: a real household instance has
~900 correspondents, ~400 doc-types, ~1500 tags. Stuffing that list into
every prompt blows the context budget AND pushes the LLM toward
hallucinating taxonomy matches instead of extracting the value the
document actually carries. Server-side fuzzy matching + user-driven
near-match disambiguation is far cheaper and more accurate.

Design reference:
    docs/design/paperless-llm-metadata.md

ASCII flow — single extraction call:

    extract(attachment_id, session_id, user_lang)
        │
        ▼
    load ChatUpload (session-scoped per #442)
        │
        ▼
    OCR / text-layer extraction via DocumentProcessor.extract_text_only
        │
        ▼
    fetch_taxonomy(mcp_manager) — used post-extraction for resolution,
    NOT injected into the prompt
        │
        ▼
    render prompt (paperless_metadata.yaml) with doc_text + learned
    examples only
        │
        ▼
    LLM call (settings.paperless_extraction_model || chat)
        │
        ▼
    validate(response, taxonomy)
        │   1. pydantic parse
        │   2. resolve each singleton field:
        │        - exact / strong-fuzzy hit → canonical value, no
        │          decision needed
        │        - 1-3 near matches → resolution with candidates,
        │          requires user decision
        │        - no match → resolution with empty near_matches
        │          (user picks "neu" or skips)
        │   3. resolve each tag the same way
        │   4. clamp created_date (today-10y ... today+1y)
        │
        ▼
    ExtractionResult(metadata, doc_text, error)
        — metadata.resolutions carries everything needing user input.
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

# Looser thresholds for the "near match" candidate list. _fuzzy_match
# only returns a single high-confidence canonical hit (Levenshtein <= 2);
# _fuzzy_top_candidates expands the radius so the user can pick from a
# small shortlist when no high-confidence hit exists.
_NEAR_MAX_DISTANCE = 6
_NEAR_MAX_RATIO = 0.4
_NEAR_LIMIT = 3

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


class FieldResolution(BaseModel):
    """Server-side resolution of one LLM-extracted value against the
    user's live Paperless taxonomy.

    Three states (read off ``status``):
      - ``exact``: ``canonical`` is set, ``near_matches`` empty.
        No user input needed; the singleton field carries the
        canonical value already.
      - ``near``: ``canonical`` is None, ``near_matches`` has 1-3
        candidate entries from the taxonomy. User picks one OR
        creates the extracted value as new.
      - ``none``: ``canonical`` is None, ``near_matches`` empty.
        Either the user creates the extracted value as new, or
        skips the field entirely.

    For singleton fields we emit at most one resolution per field;
    for tags we emit one per LLM-extracted tag that didn't resolve to
    an exact taxonomy hit.
    """
    field: Literal["correspondent", "document_type", "storage_path", "tag"]
    extracted_value: str
    canonical: str | None = None
    near_matches: list[str] = Field(default_factory=list)

    @property
    def status(self) -> Literal["exact", "near", "none"]:
        if self.canonical is not None:
            return "exact"
        if self.near_matches:
            return "near"
        return "none"

    @property
    def requires_user_decision(self) -> bool:
        return self.status != "exact"


class PaperlessMetadata(BaseModel):
    """Output of the extractor.

    Singleton taxonomy fields (correspondent / document_type /
    storage_path) carry a value ONLY when the validator resolved an
    exact / strong-fuzzy match against the live taxonomy. Anything
    needing user input lives in ``resolutions``.

    ``tags`` carries only exact-resolved tags; LLM-extracted tags that
    didn't match get individual entries in ``resolutions``.
    """
    title: str | None = None
    correspondent: str | None = None
    document_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    storage_path: str | None = None
    created_date: date | None = None
    confidence: dict[str, float] = Field(default_factory=dict)
    resolutions: list[FieldResolution] = Field(default_factory=list)


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
    # Zero candidates OR ambiguous multi-match → caller surfaces a
    # FieldResolution with the top-N candidates instead.
    return None


def _fuzzy_top_candidates(
    llm_value: str,
    taxonomy: list[str],
    *,
    limit: int = _NEAR_LIMIT,
    max_distance: int = _NEAR_MAX_DISTANCE,
    max_ratio: float = _NEAR_MAX_RATIO,
) -> list[str]:
    """Return up to ``limit`` taxonomy entries closest to ``llm_value``.

    Looser than :func:`_fuzzy_match`: shortlists candidates the user can
    pick from when no high-confidence single hit exists. Sorted by
    distance ascending — closest match first.

    Compares both the raw normalised form and the corporate-suffix-
    stripped form, takes the smaller distance. Catches cases like
    "Stadtwerke X GmbH" vs "Stadtwerke X" without forcing them to
    pass the strict 2-edit gate.
    """
    if not llm_value or not taxonomy:
        return []
    normalised = _normalise(llm_value)
    if not normalised:
        return []
    stripped = _strip_corporate_suffix(normalised)

    scored: list[tuple[int, str]] = []
    for entry in taxonomy:
        entry_n = _normalise(entry)
        if not entry_n:
            continue
        entry_stripped = _strip_corporate_suffix(entry_n)
        d_raw = Levenshtein.distance(normalised, entry_n)
        d_stripped = (
            Levenshtein.distance(stripped, entry_stripped)
            if stripped and entry_stripped
            else d_raw
        )
        d = min(d_raw, d_stripped)
        max_len = max(len(normalised), len(entry_n), 1)
        if d > max_distance or d / max_len > max_ratio:
            continue
        scored.append((d, entry))

    scored.sort(key=lambda pair: (pair[0], pair[1]))
    seen: set[str] = set()
    out: list[str] = []
    for _, entry in scored:
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


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
    2. For each singleton field (correspondent / document_type /
       storage_path), resolve the LLM value against the taxonomy:
         - exact / strong-fuzzy hit → set field to canonical, no
           resolution emitted.
         - else → field stays None, a FieldResolution is appended with
           the extracted value + up to 3 near-match candidates.
    3. For each tag, the same resolution: exact hits land in
       ``metadata.tags``, anything else gets one resolution per tag.
    4. ``created_date`` clamped to [today-10y, today+1y].
    5. ``tags`` capped at 5.

    The caller surfaces ``metadata.resolutions`` to the user as a
    decision list; the commit tool turns the user's response into
    final field values + create_* calls.
    """
    # Step 1 — parse. Drop fields the legacy prompt may still emit
    # (new_entry_proposals) so old payloads don't poison the new shape.
    payload = dict(raw_output)
    payload.pop("new_entry_proposals", None)
    payload.pop("resolutions", None)  # server-owned; ignore any LLM input
    try:
        metadata = PaperlessMetadata(**payload)
    except ValidationError as exc:
        raise ValueError(f"Malformed LLM output: {exc}") from exc

    resolutions: list[FieldResolution] = []

    # Step 2 — singleton fields.
    metadata.correspondent, res = _resolve_singleton_field(
        metadata.correspondent, taxonomy.correspondents, field="correspondent",
    )
    if res is not None:
        resolutions.append(res)

    metadata.document_type, res = _resolve_singleton_field(
        metadata.document_type, taxonomy.document_types, field="document_type",
    )
    if res is not None:
        resolutions.append(res)

    metadata.storage_path, res = _resolve_singleton_field(
        metadata.storage_path, taxonomy.storage_paths, field="storage_path",
    )
    if res is not None:
        resolutions.append(res)

    # Step 3 — tags. Each LLM tag resolved independently. Cap input at
    # 5 so the resolution list can't explode if the LLM gets verbose.
    validated_tags: list[str] = []
    seen_canonical: set[str] = set()
    for tag in (metadata.tags or [])[:5]:
        if not tag:
            continue
        canonical = _fuzzy_match(tag, taxonomy.tags)
        if canonical is not None:
            if canonical not in seen_canonical:
                seen_canonical.add(canonical)
                validated_tags.append(canonical)
        else:
            resolutions.append(FieldResolution(
                field="tag",
                extracted_value=tag,
                near_matches=_fuzzy_top_candidates(tag, taxonomy.tags),
            ))
    metadata.tags = validated_tags

    # Step 4 — date clamps.
    if metadata.created_date is not None:
        if metadata.created_date < _date_min() or metadata.created_date > _date_max():
            logger.warning(
                "Dropping created_date out of range: %s",
                metadata.created_date,
            )
            metadata.created_date = None

    metadata.resolutions = resolutions
    return metadata


def _resolve_singleton_field(
    value: str | None,
    taxonomy: list[str],
    *,
    field: str,
) -> tuple[str | None, FieldResolution | None]:
    """Resolve one LLM-extracted singleton against the taxonomy.

    Returns ``(canonical_value, None)`` when an exact / strong-fuzzy
    hit exists — the field is set, no user input needed.

    Returns ``(None, FieldResolution(...))`` when no high-confidence
    hit exists. The resolution carries the extracted value plus up to
    3 near-match candidates the user picks from (or chooses "neu").

    ``value`` of None / empty → ``(None, None)``: nothing to ask the
    user about.
    """
    if not value:
        return None, None

    canonical = _fuzzy_match(value, taxonomy)
    if canonical is not None:
        return canonical, None

    return None, FieldResolution(
        field=field,
        extracted_value=value,
        near_matches=_fuzzy_top_candidates(value, taxonomy),
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_prompt(
    *,
    doc_text: str,
    taxonomy: PaperlessTaxonomy | None = None,  # accepted for back-compat; unused
    lang: str = "de",
    learned_examples: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Build (system, user) messages for the LLM call.

    The taxonomy is intentionally NOT injected into the prompt — see
    module docstring. The parameter remains in the signature so existing
    call sites and tests don't break, but it's discarded here.

    *learned_examples* are confirm-diff entries fetched by the example
    retriever. They get rendered into a small block between the seed
    examples and the input document so the LLM can mimic the correction
    patterns. ``None`` or empty list = no learned-example block
    (placeholder collapses).
    """
    del taxonomy  # accepted for back-compat; resolution is server-side
    system = prompt_manager.get(
        "paperless_metadata", "system",
        default="Extract Paperless metadata.", lang=lang,
    )
    user = prompt_manager.get(
        "paperless_metadata", "user",
        default="{document_text}",
        lang=lang,
        document_text=doc_text[:_MAX_DOC_CHARS],
        learned_examples=_format_learned_examples(learned_examples or [], lang=lang),
    )
    return system, user


# Per-example doc snippet length. Bigger than the seed examples on
# purpose — these are real corrections and the document context helps
# the LLM see why the field was changed. Two examples × 600 chars =
# ~1.2k extra tokens worst case, which fits comfortably in the
# extraction model's context.
_LEARNED_DOC_SNIPPET_CHARS = 600


def _format_learned_examples(
    examples: list[dict[str, Any]],
    *,
    lang: str,
) -> str:
    """Render learned (confirm-diff) examples as an in-context block.

    Empty list returns an empty string so the prompt placeholder
    collapses cleanly. We deliberately do NOT include confidence or
    new_entry_proposals — confirm-diffs only carry final approved
    fields, and showing fake confidences would teach the wrong pattern.
    """
    if not examples:
        return ""

    if lang == "de":
        header = "Frühere Korrekturen des Nutzers (LLM-Vorschlag → bestätigt):"
        doc_label = "Dokument"
        llm_label = "LLM-Vorschlag"
        approved_label = "Bestätigt"
    else:
        header = "Past corrections by this user (LLM proposal → confirmed):"
        doc_label = "Document"
        llm_label = "LLM proposal"
        approved_label = "Confirmed"

    parts: list[str] = [header, ""]
    for ex in examples:
        raw = ex.get("doc_text") or ""
        snippet = raw[:_LEARNED_DOC_SNIPPET_CHARS]
        if snippet and len(raw) > _LEARNED_DOC_SNIPPET_CHARS:
            snippet += "..."
        # json.dumps for the snippet so embedded quotes, newlines, and
        # potential worked-example-injection text (``\n---\nConfirmed:
        # ...``) get properly escaped instead of breaking the prompt
        # structure. Without this, an attacker who controls the source
        # document could synthesize a fake "Bestätigt" line that fools
        # the LLM into emitting attacker-chosen metadata.
        snippet_json = json.dumps(snippet, ensure_ascii=False)
        # JSON shape matches the response format the LLM is asked to
        # emit, sans confidence/new_entry_proposals (those only apply
        # to fresh extractions, not historical confirms).
        llm_json = json.dumps(_strip_example_noise(ex.get("llm_output") or {}), ensure_ascii=False)
        approved_json = json.dumps(ex.get("user_approved") or {}, ensure_ascii=False)
        parts.append("---")
        parts.append(f"{doc_label}: {snippet_json}")
        parts.append(f"{llm_label}: {llm_json}")
        parts.append(f"{approved_label}: {approved_json}")
        parts.append("")
    return "\n".join(parts)


def _strip_example_noise(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop noisy fields from a stored llm_output before showing it as
    a worked example.

    - ``confidence`` / ``resolutions`` / ``new_entry_proposals`` reflect
      the historical extraction's state, not the corrected outcome —
      keeping them would reinforce uncertainty rather than the
      correction itself. ``new_entry_proposals`` is dropped for legacy
      payloads written before the resolution shape landed.
    - ``_doc_text`` is the pending-confirm scratchpad copy of the full
      (up to 8 KB) document text. Leaving it in would double the
      document inside the prompt (once as the snippet, once inside the
      LLM-proposal JSON) and leak the untruncated text.
    """
    out = dict(payload)
    out.pop("confidence", None)
    out.pop("resolutions", None)
    out.pop("new_entry_proposals", None)
    out.pop("_doc_text", None)
    return out


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
        user_id: int | None = None,
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

        # 4. Fetch learned examples from past confirm-diffs (PR 3).
        # Failure / empty result is silent — the seed examples in the
        # YAML still cover the cold-start case. user_id scopes to the
        # asker's own corrections; other households are invisible.
        learned_examples = await self._fetch_learned_examples(doc_text, user_id)

        # 5. Render prompt + call LLM.
        system, user = render_prompt(
            doc_text=doc_text,
            taxonomy=taxonomy,
            lang=lang,
            learned_examples=learned_examples,
        )
        try:
            raw = await self._call_llm(system, user)
        except Exception as exc:
            logger.warning(f"LLM call for metadata extraction failed: {exc}")
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
            logger.warning(f"Metadata validation failed: {exc}")
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
            logger.warning(f"ChatUpload {attachment_id} file missing on disk: {upload.file_path!r}")
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

    async def _fetch_learned_examples(
        self, doc_text: str, user_id: int | None,
    ) -> list[dict[str, Any]]:
        """Pull past confirm-diffs similar to *doc_text* for prompt
        augmentation. Lazy import to keep the retriever optional in
        test envs and to avoid pulling pgvector/Ollama deps at module
        import time."""
        try:
            from services.paperless_example_retriever import fetch_relevant_examples
            return await fetch_relevant_examples(doc_text, user_id=user_id, limit=2)
        except Exception as exc:
            # Should never happen — the retriever already swallows its
            # own errors. Defensive belt-and-braces.
            logger.warning(f"Learned-example retrieval skipped: {exc}")
            return []

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
            logger.warning(f"Taxonomy fetch failed: {exc}")
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
            logger.warning(f"MCP tool {full_name} unreachable: {exc}")
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
                logger.warning(f"MCP tool {full_name} failed: {err_str[:120]}")
            return []

        inner_msg = result.get("message")
        payload: Any = None
        if isinstance(inner_msg, str):
            try:
                payload = json.loads(inner_msg)
            except json.JSONDecodeError:
                logger.warning(
                    f"MCP tool {full_name} returned non-JSON message: {inner_msg[:120]!r}"
                )
                return []
        elif isinstance(inner_msg, dict):
            payload = inner_msg

        if not isinstance(payload, dict):
            logger.warning(
                f"MCP tool {full_name} returned unexpected payload shape: "
                f"{type(payload).__name__}"
            )
            return []

        items = payload.get(field) or []
        if not isinstance(items, list):
            logger.warning(
                f"MCP tool {full_name} field {field!r} is not a list: "
                f"{type(items).__name__}"
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

        Model resolution: explicit paperless_extraction_model override first,
        then ollama_chat_model — NOT the vision model. This is a text-only
        call (the upstream `_extract_text` hands us Docling-extracted text),
        so a vision model adds no capability and in practice some qwen3-vl
        builds ignore ``think=False``, trapping the JSON answer inside the
        thinking buffer and producing an empty content field. Bug seen in
        prod 2026-04-24 with qwen3-vl:8b; the bare-upload fallback hid the
        failure from the user. The vision-model path is a PR 2b refinement
        (scanned-doc image input); when it ships it will pick its own model
        explicitly rather than reusing this text path.
        """
        model = (
            settings.paperless_extraction_model
            or settings.ollama_chat_model
            or settings.ollama_vision_model
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
