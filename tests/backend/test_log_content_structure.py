"""No user content in a log line unless LOG_CONTENT says so (#1565).

Found 2026-11-19: about 35 INFO lines wrote chat messages, transcriptions,
titles, queries and entity names into the pod log. A log call may name a
content field only inside `len(...)` or `logged_content(...)`
(`utils/log_safety.py`), which prints `<87 chars>` unless the per-instance
debugging switch LOG_CONTENT is on.

The check is by NAME: a variable or attribute called like a content field
(`text`, `title`, `p.name_a`, ...) used as a value inside a logger call. It
cannot see content under any other name -- it is a ratchet against the known
shapes, with a negative control that it still fires. Known blind spot: an
entity's `.name` (`subject.name`, `e.name`) -- `name` is far too common to
list; the KG lines that logged one were converted by hand.

2026-11-20 (smoke test after the rollout): the first list knew `text` but not
`message` or `response_text`, so ~35 lines kept logging chat messages, router
decisions quoting the message, raw LLM answers and document file names. The
list below is the measured set; a name that is content only somewhere is
handled by `_NOT_CONTENT` with its reason, not by leaving the name out.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

_BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"

#: Names that hold user content wherever they appear in this backend.
CONTENT_FIELDS = frozenset({
    "name_a", "name_b", "entity_name", "title", "query", "query_text", "content",
    "body", "text", "transcript", "transcription", "summary", "subject",
    "description", "message_text", "user_message",
    # 2026-11-20: the chat message, the model's answer, prompts, file names.
    "message", "response_text", "response", "full_response", "raw_response",
    "prompt", "filename", "user_input", "utterance", "answer", "question",
})
_LEVELS = frozenset({"trace", "debug", "info", "success", "warning", "error", "exception", "critical"})
_LOGGERS = frozenset({"logger", "log", "_log", "_logger"})
#: Wrappers under which a content field is fine: its length, or the gate.
_SAFE_WRAPPERS = frozenset({
    "len", "logged_content", "bool", "type",
    # agent_service: structure and a hash of a model response, never its text (#1381).
    "_response_fingerprint", "_response_keys", "_describe_unparsable",
})
#: (file, source line) where a content-named field holds no user content.
#: Keyed by the line's text, so an edit of the line re-opens the question.
_NOT_CONTENT = {
    ("ha_glue/api/websocket/satellite_handler.py",
     'logger.info(f"📥 Update progress from {satellite_id}: {stage} ({progress}%) - {message}")'):
        "the OTA progress text the satellite's updater reports",
    ("services/mcp_client.py", 'logger.warning(f"Invalid MCP tool schema: {e.message}")'):
        "a jsonschema SchemaError describes the tool's schema, not the call's arguments",
}
#: Directories that are not this backend's code.
_SKIP_PARTS = frozenset({"__pycache__", "alembic", "twin_adapter", "satellite", "wakeword-models"})


def _is_log_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LEVELS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _LOGGERS
    )


def _field_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id in CONTENT_FIELDS:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in CONTENT_FIELDS:
        return node.attr
    return None


def offenders_in(source: str) -> list[tuple[int, str]]:
    """(line, field) for every content field used as a VALUE in a log call."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for call in (n for n in ast.walk(tree) if _is_log_call(n)):
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(call):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(call):
            field = _field_of(node)
            if field is None or node is call.func:
                continue
            parent = parents.get(node)
            # `body.client_id`: the field is only the way to another value.
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue
            # `summary["scores"]`: a fixed key is the way to another value too;
            # a slice (`text[:100]`) is not.
            if (isinstance(parent, ast.Subscript) and parent.value is node
                    and isinstance(parent.slice, ast.Constant) and isinstance(parent.slice.value, str)):
                continue
            # `len(text)` / `logged_content(text)`, at any depth.
            safe, up = False, parent
            while up is not None and up is not call:
                if isinstance(up, ast.Call) and getattr(up.func, "id", getattr(up.func, "attr", None)) in _SAFE_WRAPPERS:
                    safe = True
                    break
                up = parents.get(up)
            # A keyword NAME (`title=...`) is not a value; ast has no node for it.
            if not safe:
                found.append((node.lineno, field))
    return sorted(set(found))


def _backend_files():
    for path in sorted(_BACKEND.rglob("*.py")):
        if _SKIP_PARTS & set(path.relative_to(_BACKEND).parts):
            continue
        yield path


def test_no_log_call_in_the_backend_formats_user_content():
    found = {}
    for path in _backend_files():
        rel = str(path.relative_to(_BACKEND))
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        hits = [
            (line, field) for line, field in offenders_in(source)
            if (rel, lines[line - 1].strip()) not in _NOT_CONTENT
        ]
        if hits:
            found[rel] = hits
    assert not found, (
        "user content in a log line -- wrap it in `logged_content(...)` "
        "(utils.log_safety; prints its length unless LOG_CONTENT is on) or log its "
        f"`len(...)`: {found}"
    )


@pytest.mark.parametrize("line", [
    'logger.info(f"Transcription: {text}")',
    'logger.info(f"query={query[:50]}")',
    'logger.warning("pair %r ~ %r", p.name_a, p.name_b)',
    'logger.info("created: %s", notification.title)',
    'logger.debug("KG: {!r}", description)',
    # 2026-11-20
    """logger.info(f"Router classified '{message[:60]}...' as '{role_name}'")""",
    'logger.info(f"Router LLM raw response: {response_text[:300]}")',
    'logger.info("pre-selection for: {}", message[:60])',
    'logger.warning(f"folder-ingest: rejected extension for {meta.filename!r}")',
    'logger.debug(f"Raw LLM response: {response[:300] if response else 0}")',
])
def test_the_check_fires_on_the_shapes_it_was_built_from(line):
    """Negative control: each of these was in the backend on 2026-11-19."""
    assert offenders_in(line)


@pytest.mark.parametrize("line", [
    'logger.info(f"Transcription: {logged_content(text)}")',
    'logger.info(f"{len(text)} chars")',
    'logger.info(f"credential minted: {body.client_id}")',
    'logger.info("tool %s", tool.name)',
    'logger.info("score=%s", summary["scores"]["faithfulness"])',
    'print(text)',
    'logger.info(f"has_response={bool(full_response)}")',
    'logger.warning(f"not a string: {type(prompt)}")',
    'logger.info("unparsable: {}", _describe_unparsable(response_text))',
])
def test_what_it_lets_through(line):
    assert offenders_in(line) == []


def test_every_exemption_still_names_a_line_in_the_backend():
    """An exemption whose line is gone would silently cover a new one."""
    for (rel, line), _reason in _NOT_CONTENT.items():
        source = (_BACKEND / rel).read_text(encoding="utf-8")
        assert line in [x.strip() for x in source.splitlines()], (rel, line)
