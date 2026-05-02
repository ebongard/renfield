"""
Speaker Vocabulary Service — per-user frequency-ranked STT bias.

Phase B-3 follow-up. Mines confirmed-speaker transcripts to build a per-user
vocabulary frequency table that the Whisper prompt builder folds into the
`initial_prompt` for that user's future utterances. Helps the decoder pick
household-specific names (Frigate, Paperless, Konferenzraum, technical
German compound words) without needing a static configuration list.

Pipeline:

1. **Capture**: After `transcribe_with_speaker` confirms a real (not auto-
   enrolled) speaker above the recognition threshold, the linked `User.id`
   is looked up and the transcript is appended to `SpeakerVocabularyCorpus`.
   Called fire-and-forget — capture failure must not break STT.

2. **Tokenize**: A daily batch task pulls each user's recent corpus rows,
   tokenizes them, and writes the top-N terms to `SpeakerVocabulary` keyed
   on `(user_id, term, language)`. The tokenizer is deliberately simple
   (regex split, lowercase, drop-short, drop-stopwords, drop-numerics).

3. **Bias**: A `build_whisper_initial_prompt` hook handler queries the top-N
   terms for the active speaker and language, fits them into the
   ~200-char prompt budget, and returns it. If the user has no vocab yet
   (cold start), the handler returns None and the platform default takes
   over — no degradation.

Privacy: corpus rows are tier-0 (self) by default. Frequency rows inherit
the same tier. The query path filters by `user_id` only, never crosses
speakers, so no circle_sql filter is needed at v1. If we ever expose vocab
across users (e.g. household-common terms), the tier filter becomes load-
bearing.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.database import (
    Speaker,
    SpeakerVocabulary,
    SpeakerVocabularyCorpus,
    User,
)
# Re-exported at module level so tests can patch the symbol directly. The
# capture + handler functions resolve `AsyncSessionLocal` from this module's
# namespace, which means a `patch("services.speaker_vocabulary_service.AsyncSessionLocal", ...)`
# replacement is honored. Lazy local imports inside the functions would hide
# the symbol from monkeypatching and force tests to know about
# `services.database` internals.
from services.database import AsyncSessionLocal

# Tokenization config
_MIN_TOKEN_LENGTH = 3
_MAX_TOKEN_LENGTH = 64
_TOP_N_TERMS = 200
_PROMPT_TERMS_INCLUDED = 30
_CORPUS_LOOKBACK_DAYS = 60

# Conservative German + English stopwords. Kept short on purpose — the
# tokenizer otherwise drops anything < 3 chars and pure numbers, which
# already eliminates most function words.
_STOPWORDS_DE = frozenset({
    "und", "oder", "aber", "ist", "sind", "war", "waren", "wird", "werden", "wurde",
    "wurden", "habe", "hat", "hatte", "haben", "kann", "kannst", "können", "konnte",
    "muss", "musst", "müssen", "sollte", "sollten", "darf", "dürfen", "möchte",
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "einer", "eines", "kein", "keine", "keinen", "nicht", "doch", "noch", "nur",
    "auch", "auf", "aus", "bei", "für", "mit", "nach", "über", "unter", "vor",
    "von", "zu", "zur", "zum", "in", "im", "an", "am", "als", "wie", "wo",
    "ich", "du", "er", "sie", "wir", "ihr", "mein", "dein", "sein", "ihr",
    "mich", "dich", "uns", "euch", "sich", "diese", "diesem", "diesen", "dieses",
})
_STOPWORDS_EN = frozenset({
    "the", "and", "but", "for", "with", "from", "this", "that", "these", "those",
    "there", "here", "have", "has", "had", "are", "was", "were", "been", "being",
    "you", "your", "yours", "they", "them", "their", "what", "when", "where",
    "which", "who", "whom", "would", "could", "should", "into", "onto", "about",
})

_TOKEN_RE = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9-]{2,}")


def _tokenize(text: str, language: str) -> list[str]:
    """Conservative tokenizer: regex split, lowercase, filter stopwords.

    `_TOKEN_RE` requires a leading letter, so pure-numeric runs are filtered
    at the regex level — no separate `isdigit()` guard needed downstream.
    """
    stopwords = _STOPWORDS_DE if language.startswith("de") else _STOPWORDS_EN
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0).lower()
        if not (_MIN_TOKEN_LENGTH <= len(token) <= _MAX_TOKEN_LENGTH):
            continue
        if token in stopwords:
            continue
        out.append(token)
    return out


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

async def capture_transcript(
    *,
    speaker_id: int,
    text: str,
    language: str,
    is_new_speaker: bool,
) -> None:
    """Append a confirmed-speaker transcript to the corpus.

    No-op if the speaker is auto-enrolled (we don't accumulate vocab for
    "Unbekannter Sprecher #N") or if no User links to the speaker. Opens a
    short-lived session so the caller's session lifecycle isn't entangled.
    Failures are swallowed — capture must not break STT.
    """
    if is_new_speaker or not text.strip():
        return
    try:
        async with AsyncSessionLocal() as session:
            # Resolve the linked user. Speaker has no user FK; we look up
            # the user that links to this speaker via User.speaker_id.
            result = await session.execute(
                select(User.id).where(User.speaker_id == speaker_id).limit(1)
            )
            user_id = result.scalar_one_or_none()
            if user_id is None:
                return  # Identified speaker, but no user account linked

            row = SpeakerVocabularyCorpus(
                user_id=user_id,
                text=text.strip(),
                language=language,
                circle_tier=0,
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        logger.debug(f"speaker_vocabulary_service.capture_transcript failed: {e}")


# ---------------------------------------------------------------------------
# Batch tokenizer
# ---------------------------------------------------------------------------

async def rebuild_vocabulary(*, db_session) -> dict[str, int]:
    """Rebuild the per-user vocabulary table from recent corpus rows.

    Idempotent: deletes old vocab rows for each user/language with new corpus
    activity, then inserts the top-N terms. Languages with no corpus activity
    in the lookback window are left alone.

    Returns counters {users_processed, terms_written, corpus_rows_consumed}.
    """
    cutoff = datetime.utcnow() - timedelta(days=_CORPUS_LOOKBACK_DAYS)
    result = await db_session.execute(
        select(SpeakerVocabularyCorpus.user_id, SpeakerVocabularyCorpus.language, SpeakerVocabularyCorpus.text)
        .where(SpeakerVocabularyCorpus.created_at >= cutoff)
    )
    rows = result.all()

    by_key: dict[tuple[int, str], Counter] = {}
    for user_id, language, text in rows:
        counter = by_key.setdefault((user_id, language), Counter())
        counter.update(_tokenize(text, language))

    users_processed = 0
    terms_written = 0

    # Per-user commits so a failure on user N doesn't roll back users 1..N-1.
    # The rebuild is meant to be idempotent — partial progress is fine.
    for (user_id, language), counter in by_key.items():
        try:
            # Replace existing vocab for (user, language) — idempotent rebuild.
            await db_session.execute(
                delete(SpeakerVocabulary)
                .where(SpeakerVocabulary.user_id == user_id)
                .where(SpeakerVocabulary.language == language)
            )

            top = counter.most_common(_TOP_N_TERMS)
            if not top:
                await db_session.commit()
                continue
            now = datetime.utcnow()
            rows_to_insert = [
                {
                    "user_id": user_id,
                    "term": term,
                    "frequency": freq,
                    "language": language,
                    "circle_tier": 0,
                    "last_updated": now,
                }
                for term, freq in top
            ]
            # ON CONFLICT DO NOTHING is defense-in-depth; the DELETE above
            # should already have cleared the keyspace for this (user, lang).
            stmt = pg_insert(SpeakerVocabulary).values(rows_to_insert)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["user_id", "term", "language"]
            )
            await db_session.execute(stmt)
            await db_session.commit()
            users_processed += 1
            terms_written += len(rows_to_insert)
        except Exception as e:
            await db_session.rollback()
            logger.warning(
                f"speaker_vocabulary rebuild failed for user_id={user_id} lang={language}: {e}"
            )

    return {
        "users_processed": users_processed,
        "terms_written": terms_written,
        "corpus_rows_consumed": len(rows),
    }


# ---------------------------------------------------------------------------
# Hook handler — build_whisper_initial_prompt
# ---------------------------------------------------------------------------

async def vocab_initial_prompt_handler(
    *,
    user_id: int | None,
    room_id: int | None,
    language: str,
) -> Optional[str]:
    """Hook handler that returns a vocab-biased prompt or None.

    Falls through to the platform default whenever:
    - user_id is None (no identity to bias toward)
    - the user has no vocab rows yet (cold start)
    - DB access fails for any reason
    """
    if user_id is None:
        return None
    try:
        # Open a short-lived session so this handler doesn't depend on a
        # caller-supplied db_session.
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SpeakerVocabulary.term)
                .where(SpeakerVocabulary.user_id == user_id)
                .where(SpeakerVocabulary.language == language)
                .order_by(SpeakerVocabulary.frequency.desc())
                .limit(_PROMPT_TERMS_INCLUDED)
            )
            terms = [t for (t,) in result.all()]

            if not terms:
                return None

            # Resolve the speaker name for the prefix.
            user_result = await session.execute(
                select(User.first_name, User.username).where(User.id == user_id)
            )
            user_row = user_result.first()
            speaker_label = ""
            if user_row:
                first_name, username = user_row
                speaker_label = (first_name or username or "").strip()

        labels = _PROMPT_LABELS.get(language, _PROMPT_LABELS["de"])
        head = f"{labels['speaker']}: {speaker_label}. " if speaker_label else ""
        prompt = f"{head}{labels['frequent']}: {', '.join(terms)}."
        return prompt[:220].rstrip()
    except Exception as e:
        logger.debug(f"vocab_initial_prompt_handler failed: {e}")
        return None


_PROMPT_LABELS = {
    "de": {"speaker": "Sprecher", "frequent": "Häufige Begriffe"},
    "en": {"speaker": "Speaker", "frequent": "Frequent terms"},
}


# Speaker model is imported only for symbol-availability — referenced by the
# capture path's User-by-speaker_id query. Keeps the migration dependency
# graph honest.
_ = Speaker
