"""
Simba category/type classifier.

Suggests where a document belongs in the Simba tax-portal taxonomy (category +
document type) from its extracted OCR text, so the chat attachment menu can
PREFILL the picker. Best-effort: returns (None, None) on empty text / no model /
invalid output. Never raises — a missing suggestion just means the user picks
manually. Mirrors the lightweight single-call pattern of
schicht_a_extractor.generate_document_title.
"""
from __future__ import annotations

import json

from loguru import logger

from utils.config import settings
from utils.llm_client import (
    extract_response_content,
    get_classification_chat_kwargs,
    get_default_client,
)

# Enough context to classify a document; keeps the call cheap + fast.
_MAX_TEXT_CHARS = 4000


def _taxonomy_block(categories: dict[str, list[str]]) -> str:
    return "\n".join(f"- {cat}: {', '.join(types)}" for cat, types in categories.items())


def _match(value: object, options: list[str]) -> str | None:
    """Case-insensitive exact match of an LLM-returned value against the allowlist."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    for opt in options:
        if opt.lower() == v:
            return opt
    return None


def _parse_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except (ValueError, TypeError):
                return None
    return None


async def classify_simba(
    text: str,
    categories: dict[str, list[str]],
    *,
    lang: str = "de",
    llm_client=None,
) -> tuple[str | None, str | None]:
    """Classify `text` into (category, type) from the `categories` taxonomy.

    Returns (category, type) where each is a validated taxonomy value or None.
    The category is only returned if it matches an allowlist entry; the type is
    only returned if it's a valid type FOR that category (so a plausible category
    with an unknown type still prefills the category and lets the user pick the
    type).
    """
    text = (text or "").strip()
    if not text or not categories:
        return None, None
    model = settings.ollama_chat_model or settings.ollama_model
    if not model:
        return None, None

    system = (
        "Du ordnest ein Dokument in GENAU EINE Kategorie und EINEN Dokumenttyp des "
        "Simba-Steuerportals ein. Wähle AUSSCHLIESSLICH aus der vorgegebenen Liste "
        '(exakte Schreibweise). Antworte nur mit JSON: {"category": "...", "type": "..."}. '
        "Bei Unsicherheit die plausibelste Kombination wählen."
    )
    user = (
        f"Verfügbare Kategorien und Typen:\n{_taxonomy_block(categories)}\n\n"
        f"Dokumentinhalt (Auszug):\n{text[:_MAX_TEXT_CHARS]}\n\nJSON:"
    )
    try:
        client = llm_client or get_default_client()
        kwargs = get_classification_chat_kwargs(model)
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options={"temperature": 0.1},
            **kwargs,
        )
        payload = _parse_json(extract_response_content(response) or "")
        if not isinstance(payload, dict):
            return None, None
        cat = _match(payload.get("category"), list(categories.keys()))
        if not cat:
            return None, None
        typ = _match(payload.get("type"), categories.get(cat, []))
        return cat, typ
    except Exception as e:  # noqa: BLE001 — a failed suggestion must never break the menu
        logger.warning(f"Simba classification failed: {e}")
        return None, None
