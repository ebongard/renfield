"""
KG Relation Validator — defensive post-extraction filter.

The LLM extraction step (``KnowledgeGraphService.extract_and_save`` and
``extract_from_text``) emits relation candidates that occasionally violate
basic invariants the model was *asked* to respect in the prompt but cannot be
trusted to honour deterministically. This module is the deterministic backstop:
it inspects each resolved relation candidate before it reaches
``save_relation`` and drops the ones that fail a fixed rule set.

Empirically (2026-05-26 incident, relations 88-98) this catches 10 of 11
garbage relations. The 11th — ``Anna mag Mango`` where Eduard's preference is
misattributed to Anna — is an *attribution* error the validator cannot see,
because "Anna" genuinely appears in the dialog. That class is fixed at the
prompt layer (speaker-identity binding), not here. See
``tasks/kg-entity-collapse-investigation.md``.

Rules (in order):

1. **No self-loop.** ``subject_id == object_id`` → reject. Primary catch for
   the 3 self-loop incidents (``Anna ist_verheiratet_mit Anna`` etc.).
2. **Source grounding.** Both entity names must be anchored in the source text
   (the dialog or document chunk the relation was extracted from): at least one
   significant word (len>=3) of the name prefix-matches a source token in
   either direction (so "Anna" ↔ "Annas" both hold — German inflection). Prefix
   not substring, so "Ada" does not match "Kanada". Grounds on the
   discriminating token rather than every word, because ``resolve_entity`` can
   canonicalize a spoken "Eduard" into a stored "Eduard van den Bongard" — and
   we must not drop that real chat-learned fact. Catches the hallucination class
   (``Hans Filbinger``, ``Kleinenbroich``) which shares no token with a dialog
   that never mentions them.
3. **Predicate/object-type contract.** A regex-keyed heuristic table (loaded
   from ``prompts/knowledge_graph.yaml``) maps predicate-name patterns to a
   required object entity_type. ``heiratet_*`` / ``ist_mutter_von`` → ``person``;
   ``wohnt_in`` / ``hat_sitz_in`` → ``place``. A predicate that matches no
   pattern is ALLOWED (open vocabulary is the strategic choice — the heuristic
   must not become a back-door closed enum). Catches type mismatches like
   ``Anna heißt_auch Kleinenbroich`` (person-predicate → place object).
4. **Confidence floor.** ``confidence < floor`` → reject. Defense-in-depth
   only: all 11 incident relations were emitted at confidence 1.0, so this
   rule did not catch any of them. It guards a different failure class
   (genuinely low-confidence model output). The floor (0.5) is pinned to the
   lower bound the extraction prompt advertises ("confidence: 0.5-1.0") so it
   never drops a relation the model emitted exactly as instructed.

The validator is intentionally side-effect-free and takes plain values, not a
DB session, so it unit-tests without a database. The caller resolves entities
first, then asks the validator for a verdict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

# Default confidence floor if the yaml config is missing/unreadable. Matches
# the lower bound the extraction prompt advertises to the model ("0.5-1.0") —
# a higher default would silently drop relations the LLM emitted as instructed.
_DEFAULT_CONFIDENCE_FLOOR = 0.5

# Tokeniser for source grounding: split on whitespace + common punctuation so
# "Annas," and "(Kleinenbroich)" reduce to "annas" / "kleinenbroich".
_RE_TOKEN_SPLIT = re.compile(r"[\s.,;:!?()\[\]{}\"'„“”‚‘’/\\—–-]+")


class _EntityLike(Protocol):
    """Minimal shape the validator needs from a resolved KG entity."""

    id: int
    name: str
    entity_type: str


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a single relation candidate."""

    ok: bool
    reason: str | None = None  # rule name that rejected it; None if ok


# Rule-name constants (stable strings for tests + metrics).
REASON_SELF_LOOP = "self_loop"
REASON_NOT_GROUNDED = "not_grounded"
REASON_TYPE_MISMATCH = "predicate_object_type_mismatch"
REASON_LOW_CONFIDENCE = "low_confidence"


def _tokenise(text: str) -> set[str]:
    """Lowercase + split source text into a set of bare tokens."""
    return {t for t in _RE_TOKEN_SPLIT.split(text.lower()) if t}


def _name_is_grounded(name: str, source_tokens: set[str]) -> bool:
    """True if ``name`` is anchored in the source text.

    The name we receive is the *resolved* entity's canonical name, which can be
    longer than the surface form actually spoken — ``resolve_entity`` merges a
    dialog mention "Eduard" into an existing entity stored as "Eduard van den
    Bongard" via embedding similarity. If we required EVERY word of the
    canonical name to appear, that legitimate relation would be dropped because
    "van"/"den"/"bongard" aren't in the current short exchange. KG-from-chat is
    a must-have, so we ground on the discriminating token instead:

      A name is grounded if AT LEAST ONE of its significant words (length >= 3)
      prefix-matches a source token (either direction, so "Anna" ↔ "Annas"
      both hold — covers German inflection). Names whose words are all short
      (<3 chars) are considered ungroundable (not grounded).

    This still rejects the hallucination class: "Hans Filbinger" and
    "Kleinenbroich" share NO token with a dialog that never mentions them, so
    no word anchors and the relation is dropped. The deliberate trade-off is
    mild over-acceptance (a partly-hallucinated name like "Anna Schmidt" grounds
    on "Anna") — the safe direction when the alternative is silently losing real
    chat-learned facts. Self-loop + predicate-type rules and the prompt's own
    grounding instruction are the other layers; this rule is the net, not the
    whole defense.
    """
    name_lower = name.strip().lower()
    if not name_lower:
        return False

    words = [w for w in _RE_TOKEN_SPLIT.split(name_lower) if w]
    if not words:
        return False

    significant = [w for w in words if len(w) >= 3]
    if not significant:
        # All words are 1-2 chars (initials etc.): no significant token to
        # anchor on → not grounded.
        return False

    # At least one significant word must prefix-match a source token in either
    # direction. Prefix (not substring) avoids "ada" matching "kanada" while
    # still accepting "anna" vs "annas".
    for word in significant:
        for tok in source_tokens:
            if tok.startswith(word) or word.startswith(tok):
                return True
    return False


def load_heuristics(prompt_manager: Any) -> dict[str, Any]:
    """Load the validation config block from prompts/knowledge_graph.yaml.

    Shape:
        relation_validation:
          confidence_floor: 0.6
          predicate_object_type:
            - {pattern: "^(heiratet|ist_verheiratet|...)", object_type: person}
            - {pattern: "^(wohnt_in|befindet_sich_in|...)", object_type: place}

    Returns a normalised dict with compiled regexes. Missing/malformed config
    degrades gracefully: confidence floor defaults, type rules become empty
    (so rule 3 passes everything — fail-open, not fail-closed).
    """
    cfg = prompt_manager.get_config("knowledge_graph", "relation_validation") or {}

    floor = cfg.get("confidence_floor", _DEFAULT_CONFIDENCE_FLOOR)
    try:
        floor = float(floor)
    except (TypeError, ValueError):
        floor = _DEFAULT_CONFIDENCE_FLOOR

    rules: list[tuple[re.Pattern[str], str]] = []
    for entry in cfg.get("predicate_object_type", []) or []:
        pattern = entry.get("pattern")
        object_type = entry.get("object_type")
        if not pattern or not object_type:
            continue
        try:
            rules.append((re.compile(pattern, re.IGNORECASE), object_type))
        except re.error as e:
            logger.warning(f"KG validator: bad predicate regex '{pattern}': {e}")

    return {"confidence_floor": floor, "type_rules": rules}


def validate_relation(
    *,
    subject: _EntityLike,
    obj: _EntityLike,
    predicate: str,
    confidence: float,
    source_text: str,
    heuristics: dict[str, Any],
) -> ValidationResult:
    """Validate one resolved relation candidate. Pure, no I/O.

    Args:
        subject: resolved subject entity (needs .id, .name, .entity_type)
        obj: resolved object entity
        predicate: the relation predicate string
        confidence: clamped confidence (caller already coerces to float)
        source_text: the dialog/chunk the relation was extracted from
        heuristics: output of ``load_heuristics``

    Returns ValidationResult(ok=True) if the relation passes all rules, else
    ok=False with ``reason`` set to the failing rule's constant.
    """
    # Rule 1 — no self-loop.
    if subject.id == obj.id:
        return ValidationResult(False, REASON_SELF_LOOP)

    # Rule 2 — source grounding. Both names must be in the source text.
    source_tokens = _tokenise(source_text)
    if not _name_is_grounded(subject.name, source_tokens):
        return ValidationResult(False, REASON_NOT_GROUNDED)
    if not _name_is_grounded(obj.name, source_tokens):
        return ValidationResult(False, REASON_NOT_GROUNDED)

    # Rule 3 — predicate/object-type contract. First matching rule wins;
    # no match → allowed (open vocabulary).
    pred_lower = predicate.strip().lower()
    for pattern, required_type in heuristics.get("type_rules", []):
        if pattern.search(pred_lower):
            if obj.entity_type != required_type:
                return ValidationResult(False, REASON_TYPE_MISMATCH)
            break  # predicate matched a rule and the type is correct

    # Rule 4 — confidence floor (defense-in-depth).
    floor = heuristics.get("confidence_floor", _DEFAULT_CONFIDENCE_FLOOR)
    if confidence < floor:
        return ValidationResult(False, REASON_LOW_CONFIDENCE)

    return ValidationResult(True)
