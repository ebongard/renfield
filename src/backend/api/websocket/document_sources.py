"""Open targets for the chat's provenance chips (`CHAT_DOCUMENT_LINKS_ENABLED`).

A chip names a document the answer drew on. Until this module it only named
knowledge-base documents found through `internal.knowledge_search`, and its
link led to a list row. With the flag on, `turn_sources` additionally

* turns Paperless tool results into sources (`paperless_sources`),
* gives the direct RAG path sources (it had none),
* attaches a SERVER-built open target to every source.

🛑 Nothing here reads a URL, a host or a base from a tool result or from model
output. A link is assembled by `paperless_document_url` from the configured
public base and an id that was validated as an integer -- and from nothing
else. A `url` key inside a tool result is ignored like any other unknown key.

🛑 The Paperless base is the PUBLIC one or none (`public_only=True`). The
backend-facing `PAPERLESS_API_URL` is a cluster-internal service name on one
instance; a chip link built from it would be persisted into the conversation
and never resolve in a browser. Without `PAPERLESS_PUBLIC_URL` a Paperless
source is still named, it just has no link.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from utils.log_safety import error_text_without_content

PAPERLESS_SEARCH_TOOL = "mcp.paperless.search_documents"
PAPERLESS_GET_TOOL = "mcp.paperless.get_document"

#: How many `search_documents` hits of ONE TURN become chips.
#:
#: A search answers with up to 100 hits (the tool's default `max_results`) and
#: an agent turn runs several searches; chipping them all would bury the
#: documents the answer is about, and the chip row collapses behind "+N" after
#: six (`SourceChips.MAX_VISIBLE`). Three leaves room for the documents the
#: agent explicitly fetched with `get_document` -- those are never capped,
#: fetching one is the strongest signal the turn has that the answer uses it.
#:
#: Trade-off, stated plainly: the cap is positional, not semantic. Paperless
#: orders by `-created` unless the agent asks otherwise, so "the first three"
#: are the three newest matches, which need not be the ones the answer quotes.
#: The alternative -- matching titles against the answer text -- would make the
#: chip depend on what the model wrote, which is exactly what this feature must
#: not do. A document the answer names but no chip shows is still reachable:
#: asking for it makes the agent fetch it, and a fetched document always chips.
PAPERLESS_SEARCH_SOURCE_CAP = 3

#: A title is display text from a third system; bound it before it is persisted
#: into `message_metadata`.
_TITLE_MAX_CHARS = 200

#: Document ids are Postgres `integer` primary keys, here and in Paperless.
_MAX_DOCUMENT_ID = 2**31 - 1


def _document_id(value: Any) -> int | None:
    """The value as a document id, or None.

    Only a real JSON integer counts. A numeric STRING is refused on purpose:
    both tools answer with integers, so a string in that place is a payload
    that does not have the documented shape, and nothing is derived from it.
    `bool` is an `int` in Python and is excluded explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > _MAX_DOCUMENT_ID:
        return None
    return value


def _title(value: Any) -> str:
    """Bounded display text; empty when the result carried none (the chip then
    falls back to a translated generic label)."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_TITLE_MAX_CHARS]


def _paperless_url(paperless_document_id: Any) -> str | None:
    # Late import: `api.routes.knowledge` pulls the whole document route in.
    from api.routes.knowledge import paperless_document_url

    return paperless_document_url(paperless_document_id, public_only=True)


def _paperless_source(doc: Any) -> dict | None:
    """One Paperless document of a tool result as a source, or None."""
    if not isinstance(doc, dict):
        return None
    doc_id = _document_id(doc.get("id"))
    if doc_id is None:
        return None
    source: dict = {
        "kind": "paperless",
        "paperless_document_id": doc_id,
        "title": _title(doc.get("title")),
    }
    url = _paperless_url(doc_id)
    if url:
        source["paperless_url"] = url
    return source


def paperless_sources(tool_results: list) -> list[dict]:
    """The Paperless documents of this turn's tool results, as sources.

    Order (stable, depends only on the tool results):
      1. every document fetched with `get_document`, in call order;
      2. `search_documents` hits, at most `PAPERLESS_SEARCH_SOURCE_CAP` for the
         whole turn, taken RANK-WISE across the searches: the first hit of each
         search, then the second of each, and so on. A turn that refines its
         query three times would otherwise spend the whole cap on the first,
         usually worst, search.
    One chip per document id.

    🛑 Access: Paperless is read through ONE backend token and is not
    circle-filtered; the gate is the `mcp.paperless.read` permission, checked
    when the tool ran. Every source here is a document whose id and title that
    tool result ALREADY handed to this user's turn (the agent read them, the
    answer may quote them) -- a chip shows a subset of it and adds nothing.
    """
    from api.websocket.chat_handler import _parse_mcp_raw_data

    fetched: list[dict] = []
    searches: list[list[dict]] = []
    for tool_name, data in tool_results:
        if tool_name not in (PAPERLESS_GET_TOOL, PAPERLESS_SEARCH_TOOL):
            continue
        # MCP results arrive as content blocks ([{"type": "text", "text": JSON}]).
        parsed = _parse_mcp_raw_data(data) if isinstance(data, list) else data
        if not isinstance(parsed, dict):
            continue
        if tool_name == PAPERLESS_GET_TOOL:
            source = _paperless_source(parsed)
            if source:
                fetched.append(source)
            continue
        results = parsed.get("results")
        if isinstance(results, list):
            hits = [s for s in map(_paperless_source, results) if s]
            if hits:
                searches.append(hits)

    sources: list[dict] = []
    seen: set[int] = set()

    def _add(source: dict) -> bool:
        doc_id = source["paperless_document_id"]
        if doc_id in seen:
            return False
        seen.add(doc_id)
        sources.append(source)
        return True

    for source in fetched:
        _add(source)

    taken = 0
    for rank in range(max((len(hits) for hits in searches), default=0)):
        for hits in searches:
            if taken >= PAPERLESS_SEARCH_SOURCE_CAP:
                return sources
            if rank < len(hits) and _add(hits[rank]):
                taken += 1
    return sources


def _result_document_ids(search_results: list | None) -> list[int]:
    """Document ids of RAG search results, in rank order, each once."""
    ids: list[int] = []
    for result in search_results or []:
        doc = result.get("document") if isinstance(result, dict) else None
        doc_id = _document_id(doc.get("id")) if isinstance(doc, dict) else None
        if doc_id is not None and doc_id not in ids:
            ids.append(doc_id)
    return ids


async def _visible_meta(doc_ids: set[int], user_id: int | None) -> dict[Any, dict]:
    """`_visible_document_meta` on a session of its own: title, tier and
    Paperless id of ONLY the documents `user_id` may see."""
    from services.database import AsyncSessionLocal
    from services.knowledge_tool import _visible_document_meta

    async with AsyncSessionLocal() as db:
        return await _visible_document_meta(db, doc_ids, user_id)


def _with_open_target(source: dict, meta_row: dict | None) -> dict:
    """`source` with the server-built Paperless link of its document, if any.

    A `paperless_url` that arrived inside the source is DROPPED first: the only
    link a source may carry is the one built here.
    """
    out = {k: v for k, v in source.items() if k != "paperless_url"}
    url = _paperless_url((meta_row or {}).get("paperless_document_id"))
    if url:
        out["paperless_url"] = url
    return out


async def turn_sources(
    tool_results: list,
    rag_search_results: list | None,
    user_id: int | None,
) -> list[dict]:
    """All sources of one turn, flag ON. Called once, before the turn is
    persisted, so the `done` frame and `message_metadata.sources` carry the
    same list.

    Order: knowledge-base sources of the agent's `knowledge_search`, then those
    of the direct RAG path (`rag_search_results`, the path had no sources
    before), then Paperless documents. A Paperless document that is ALSO a
    knowledge-base source of this turn appears once, as the knowledge-base
    chip -- that one carries the tier and both targets.

    🛑 `user_id` is the server-side identity of the turn. Document metadata is
    resolved through `_visible_document_meta`, i.e. `documents_circles_filter`:
    - a `knowledge_search` source keeps its chip either way (the tool already
      filtered it) but gets a Paperless link only if the document is visible;
    - a RAG-path document gets a chip ONLY if it is visible. The retrieval
      filtered by `user_id` too, but a follow-up turn reuses CACHED results and
      access may have changed since.
    A failed lookup degrades to: no links, no RAG-path chips. It never fails
    the turn.
    """
    from api.websocket.chat_handler import _extract_agent_sources

    kb_sources = _extract_agent_sources(tool_results)
    rag_ids = _result_document_ids(rag_search_results)

    wanted = {i for i in (_document_id(s.get("document_id")) for s in kb_sources) if i}
    wanted.update(rag_ids)
    meta: dict[Any, dict] = {}
    if wanted:
        try:
            meta = await _visible_meta(wanted, user_id)
        except Exception as e:
            logger.warning(
                "chat sources: document lookup failed (chips keep the in-app link, "
                f"the RAG path gets none): {error_text_without_content(e)}"
            )

    sources = [_with_open_target(s, meta.get(s.get("document_id"))) for s in kb_sources]
    seen = {s.get("document_id") for s in sources}
    for doc_id in rag_ids:
        row = meta.get(doc_id)
        if row is None or doc_id in seen:
            continue
        seen.add(doc_id)
        sources.append(_with_open_target({
            "document_id": doc_id,
            "filename": row.get("filename") or "",
            "title": row.get("title") or f"Dokument {doc_id}",
            "tier": row.get("tier"),
        }, row))

    filed = {
        meta[doc_id].get("paperless_document_id") for doc_id in seen if doc_id in meta
    }
    sources.extend(
        s for s in paperless_sources(tool_results)
        if s["paperless_document_id"] not in filed
    )
    return sources


# --------------------------------------------------------------------------- #
# Read side: persisted metadata, served to whoever reads the conversation
# --------------------------------------------------------------------------- #
PAPERLESS_READ_PERMISSION = "mcp.paperless.read"

#: The keys of `message_metadata` the history route hands to a client -- the
#: ones `historyToUiMessage` (ChatContext.tsx) reads, and nothing else.
#:
#: 🛑 This is an ALLOWLIST on purpose. The column is the turn's working record,
#: and most of it is not for a reader: `action_summary` is the agent's copy of
#: the tool result (Paperless ids and titles of up to ten hits, or the whole
#: `knowledge_search` payload with retrieved text) and exists for the next
#: PROMPT; `action_result`, `speaker`, `room_context`, `scanner_job` are of the
#: same kind. A new key is invisible to clients until it is added here -- with
#: a decision about who may read it.
#:
#: `attachments` and `wb_entities` have no writer in this repository (a plugin
#: persists the latter); they are listed because the client reads them.
CLIENT_METADATA_KEYS = ("agent_role", "artifacts", "attachments", "wb_entities")

#: What a source of each kind may carry out, and the type each value must have.
_KNOWLEDGE_SOURCE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filename": str, "title": str, "tier": int,
}
_PAPERLESS_SOURCE_FIELDS: dict[str, type | tuple[type, ...]] = {"title": str}


def _may_read_paperless(reader: Any) -> bool:
    """Does this reader hold the permission the two Paperless reads require?

    The same check the tool call makes (`mcp_servers.yaml` maps both tools to
    `mcp.paperless.read`; `has_mcp_permission` honours `mcp.*` and the
    server-wide `mcp.paperless`). Any failure to read the permissions denies.
    """
    if reader is None:
        return False
    try:
        from models.permissions import has_mcp_permission

        return has_mcp_permission(list(reader.get_permissions() or []), PAPERLESS_READ_PERMISSION)
    except Exception as e:
        logger.warning(
            f"chat sources: reader permissions unreadable, Paperless sources withheld: "
            f"{error_text_without_content(e)}"
        )
        return False


def _stored_sources(meta: dict) -> list:
    """The `sources` of one stored metadata; anything but a list is no sources
    (a value of another shape is dropped, never passed through)."""
    sources = meta.get("sources")
    return sources if isinstance(sources, list) else []


def _typed_fields(source: dict, fields: dict) -> dict:
    return {
        key: source[key] for key, kind in fields.items()
        if isinstance(source.get(key), kind) and not isinstance(source.get(key), bool)
    }


def _source_for_reader(
    source: Any, visible: dict, paperless_ok: bool, check_rights: bool,
) -> dict | None:
    """One stored source as the reader may see it, rebuilt key by key; None
    when it is not theirs or not a source this module would have written.

    * The kind is `"paperless"` or absent. Anything else is dropped: the client
      would render it as a knowledge-base chip.
    * A `document_id` -- on ANY kind -- must be a document the reader's circles
      cover. A Paperless source never carries one out.
    * A Paperless source needs a valid `paperless_document_id` and the reader's
      Paperless permission.
    * `paperless_url` survives only if it is exactly the link the server builds
      for that document now; nothing else is ever a link.

    `check_rights=False` (auth off, one trust domain) keeps the shape rules and
    skips the two access rules.
    """
    if not isinstance(source, dict):
        return None
    kind = source.get("kind")
    if kind not in (None, "paperless"):
        return None

    doc_id = None
    if "document_id" in source:
        doc_id = _document_id(source["document_id"])
        if doc_id is None or (check_rights and doc_id not in visible):
            return None

    if kind == "paperless":
        paperless_id = _document_id(source.get("paperless_document_id"))
        if paperless_id is None or (check_rights and not paperless_ok):
            return None
        out = {"kind": "paperless", "paperless_document_id": paperless_id}
        out.update(_typed_fields(source, _PAPERLESS_SOURCE_FIELDS))
    else:
        if doc_id is None:
            return None
        paperless_id = (visible.get(doc_id) or {}).get("paperless_document_id")
        out = {"document_id": doc_id}
        out.update(_typed_fields(source, _KNOWLEDGE_SOURCE_FIELDS))

    url = source.get("paperless_url")
    if isinstance(url, str) and url:
        # Auth off has no lookup, so a knowledge-base link cannot be re-derived
        # there; it is the one the server stored.
        if (kind is None and not check_rights) or url == _paperless_url(paperless_id):
            out["paperless_url"] = url
    return out


async def metadata_for_reader(metadatas: list, reader: Any) -> list:
    """The message metadata of a history page as THIS reader may see it.

    Two rules, in this order:

    1. Only `CLIENT_METADATA_KEYS` and `sources` leave the server, for every
       reader and with auth on or off. A metadata that is not a dict is `None`.
    2. `sources` are per-ASKER -- built from the asker's circle reach and
       Paperless permission -- while a conversation is a shared artifact
       (auth-on §8.1) that every member its tier reaches reads. Messages do not
       record who asked, so under auth each source is decided for EVERY reader,
       the asker included, by the reader's own rights (`_source_for_reader`).
       For the asker this changes nothing unless access was lost or the
       document is gone -- and then the chip led nowhere anyway.

    The visibility lookup runs on a session of its own (`_visible_meta`, as on
    the write side), so a failed statement cannot leave the caller's
    transaction aborted. A failed lookup withholds every source that names a
    knowledge-base document (fail-closed); it never fails the history.

    Returns a list parallel to `metadatas`, built from new dicts -- the stored
    ones are ORM column values and are never modified.
    """
    from utils.config import settings

    check_rights = bool(settings.auth_enabled)
    dicts = [m for m in metadatas if isinstance(m, dict)]

    visible: dict = {}
    paperless_ok = False
    if check_rights and any(_stored_sources(m) for m in dicts):
        wanted: set[int] = set()
        for meta in dicts:
            for source in _stored_sources(meta):
                doc_id = _document_id(source.get("document_id")) if isinstance(source, dict) else None
                if doc_id is not None:
                    wanted.add(doc_id)
        if wanted:
            try:
                visible = dict(await _visible_meta(wanted, getattr(reader, "id", None)))
            except Exception as e:
                logger.warning(
                    "chat sources: visibility lookup for a history reader failed, "
                    f"knowledge-base sources withheld: {error_text_without_content(e)}"
                )
        paperless_ok = _may_read_paperless(reader)

    out: list = []
    for meta in metadatas:
        if not isinstance(meta, dict):
            out.append(None)
            continue
        served = {key: meta[key] for key in CLIENT_METADATA_KEYS if key in meta}
        sources = [
            kept for source in _stored_sources(meta)
            if (kept := _source_for_reader(source, visible, paperless_ok, check_rights)) is not None
        ]
        if sources:
            served["sources"] = sources
        out.append(served)
    return out
