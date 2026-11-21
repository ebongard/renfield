"""No user content in a log line unless LOG_CONTENT says so (#1565).

Found 2026-11-19: about 35 INFO lines wrote chat messages, transcriptions,
titles, queries and entity names into the pod log. A log call may name a
content field only inside `len(...)` or `logged_content(...)`
(`utils/log_safety.py`), which prints `<87 chars>` unless the per-instance
debugging switch LOG_CONTENT is on.

The check is by NAME: a variable or attribute called like a content field
(`text`, `title`, `p.name_a`, ...) used as a value inside a logger call. It
cannot see content under any other name -- it is a ratchet against the known
shapes, with a negative control that it still fires.

`.name` is too common to list as a field, and an entity's or a speaker's
name is content (`subject.name`, `speaker.name`) while a room's or an MCP
server's is not. So a `.name` in a log call counts as content unless its
receiver is classified in `_NAME_IS_NOT_CONTENT`, per file -- the same
receiver (`path`) is a repo file in one module and a user's document in
another. An unclassified receiver fails, so a new one forces the decision
instead of slipping through. The same holds for a bare `name` or `*_name`
local (`speaker_name`, a KG `name`), classified by the identifier per file,
or everywhere for the configuration names in `_NAME_LIKE_NOT_CONTENT`
(2026-11-21, after the review of #1606 found speaker and entity names
logged under those).

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
_CONFIG = "configuration the admin or the repo defines, no household content"
#: `*_name` identifiers that name configuration wherever they appear.
_NAME_LIKE_NOT_CONTENT = frozenset({
    "tool_name", "intent_name", "role_name", "server_name", "action_name",
    "task_name", "index_name", "dim_name", "env_name", "namespaced_name",
    "renderer_name", "device_name", "voice_name", "profile_name",
    "room_name", "new_room_name", "old_room_name", "area_name",
})
#: (file, receiver) whose `.name` in a log line holds no user content.
#: Any other `X.name` is content (an entity, a speaker, a document's file).
_NAME_IS_NOT_CONTENT = {
    ("services/mcp_client.py", "state.config"): "an MCP server's configured name",
    ("services/mcp_client.py", "config"): "an MCP server's configured name",
    ("services/mcp_client.py", "file"): "an MCP overlay config file of the deployment",
    ("services/skill_seed_loader.py", "path"): "a seed skill file shipped in the image",
    ("services/prompt_manager.py", "path"): "a prompt file shipped in the image",
    ("ha_glue/services/room_service.py", "room"): "a room, " + _CONFIG,
    ("ha_glue/api/routes/rooms.py", "room"): "a room, " + _CONFIG,
    ("ha_glue/api/websocket/satellite_handler.py", "db_room"): "a room, " + _CONFIG,
    ("api/routes/roles.py", "role"): "a role, " + _CONFIG,
    ("api/websocket/chat_handler.py", "role"): "an agent role from agent_roles.yaml",
    ("services/auth_service.py", "role"): "a role, " + _CONFIG,
    ("services/auth_service.py", "existing"): "a system role",
    ("api/lifecycle.py", "r"): "a default role",
    ("api/routes/scheduled_tasks.py", "task"): "a scheduled task, " + _CONFIG,
    ("services/scheduled_tasks/engine.py", "task"): "a scheduled task, " + _CONFIG,
    ("services/agent_tools.py", "tool"): "a tool's registered name",
    ("utils/circuit_breaker.py", "self"): "the breaker's name, set in code",
    ("ha_glue/services/zeroconf_service.py", "self"): "the advertised service name",
    ("services/whisper_service.py", "tmp"): "a temporary file the service created",
    ("ha_glue/services/audio_output_service.py", "cache_file"): "a TTS cache file named by a hash",
    ("api/routes/settings.py", "model_file"): "a wakeword model file",
    ("services/auth_service.py", "role_config"): "a default role's definition",
    ("services/notification_poller.py", "s"): "an MCP server's configured name",
    ("services/notification_poller.py", "server"): "an MCP server's configured name",
    # A bare `name` / `*_name` local, classified by the identifier itself.
    ("api/lifecycle.py", "name"): "a boot task's name, set in code",
    ("api/routes/mcp.py", "name"): "an MCP server's configured name",
    ("api/routes/scheduled_tasks.py", "name"): "a scheduled task, " + _CONFIG,
    ("ha_glue/integrations/homeassistant.py", "name"): "a Home Assistant area, " + _CONFIG,
    ("ha_glue/services/output_providers.py", "name"): "an output provider from its config file",
    ("ha_glue/services/presence_analytics.py", "name"): "a configured time zone",
    ("ha_glue/services/room_service.py", "name"): "a room, " + _CONFIG,
    ("services/agent_router.py", "name"): "an agent role from agent_roles.yaml",
    ("services/agent_service.py", "name"): "a tool's registered name",
    ("services/daypart_service.py", "name"): "a configured time zone",
    ("services/household_backfill.py", "name"): "a backfill step's name, set in code",
    ("services/mcp_client.py", "name"): "an MCP server's configured name",
    ("services/mcp_health_monitor.py", "name"): "an MCP server's configured name",
    ("services/notification_poller.py", "name"): "an MCP server's configured name",
    ("services/paperless_metadata_extractor.py", "full_name"): "an MCP tool's namespaced name",
    ("services/polymorphic_atom_store.py", "name"): "a retrieval source's name, set in code",
    ("services/task_queue.py", "group_name"): "a Redis consumer group, set in code",
    ("services/watchdog.py", "name"): "a watchdog target from its config",
    ("utils/config.py", "name"): "a setting's name",
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


def _field_of(node: ast.AST, rel: str | None) -> str | None:
    if isinstance(node, ast.Name) and node.id in CONTENT_FIELDS:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in CONTENT_FIELDS:
        return node.attr
    # The same field read by key: `c["text"]`, `result.get("message")`.
    receiver, key = _key_read(node)
    if key in CONTENT_FIELDS:
        return f"{receiver}[{key!r}]"
    if isinstance(node, ast.Attribute):
        receiver, key = ast.unparse(node.value), node.attr
    elif isinstance(node, ast.Name):
        receiver, key = "", node.id
    if key is None or not _is_name_like(key) or key in _NAME_LIKE_NOT_CONTENT:
        return None
    # `x.name` is classified by its receiver, a bare `name`/`*_name` by itself.
    classified = (rel, receiver) if key == "name" and receiver else (rel, key)
    if classified in _NAME_IS_NOT_CONTENT:
        return None
    return f"{receiver}.{key}" if receiver else key


def _is_name_like(identifier: str) -> bool:
    return identifier == "name" or identifier.endswith("_name")


def _key_read(node: ast.AST) -> tuple[str, str | None]:
    """(receiver, key) of `x["key"]` or `x.get("key", ...)`, else (``, None)."""
    if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        return ast.unparse(node.value), node.slice.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return ast.unparse(node.func.value), node.args[0].value
    return "", None


def _is_method_call_on(attr: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> bool:
    call = parents.get(attr)
    return isinstance(call, ast.Call) and call.func is attr and attr.attr != "get"


def offenders_in(source: str, rel: str | None = None) -> list[tuple[int, str]]:
    """(line, field) for every content field used as a VALUE in a log call.

    `rel` is the file's path under the backend; it decides which `.name`
    receivers are classified as not content. Without it every `.name` counts.
    """
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for call in (n for n in ast.walk(tree) if _is_log_call(n)):
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(call):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(call):
            field = _field_of(node, rel)
            if field is None or node is call.func:
                continue
            parent = parents.get(node)
            # `body.client_id`: the field is only the way to another value --
            # but `text.strip()` is the content itself, so a method call counts
            # (`.get("key")` excepted: the key read is checked on its own).
            if (isinstance(parent, ast.Attribute) and parent.value is node
                    and not _is_method_call_on(parent, parents)):
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


def _offenders_in_file(rel: str) -> list[tuple[int, str]]:
    source = (_BACKEND / rel).read_text(encoding="utf-8")
    lines = source.splitlines()
    return [
        (line, field) for line, field in offenders_in(source, rel)
        if (rel, lines[line - 1].strip()) not in _NOT_CONTENT
    ]


def test_no_log_call_in_the_backend_formats_user_content():
    found = {}
    for path in _backend_files():
        rel = str(path.relative_to(_BACKEND))
        if hits := _offenders_in_file(rel):
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
    'logger.info("merged %s", logged_content(subject.name))',
    'logger.info("score=%s", summary["scores"]["faithfulness"])',
    'print(text)',
    'logger.info(f"has_response={bool(full_response)}")',
    'logger.warning(f"not a string: {type(prompt)}")',
    'logger.info("unparsable: {}", _describe_unparsable(response_text))',
])
def test_what_it_lets_through(line):
    assert offenders_in(line) == []


@pytest.mark.parametrize("line", [
    # Each of these was in the backend on 2026-11-21.
    'logger.info(f"merged {loser.name} into {winner.name}")',
    """logger.info(f"Linked speaker '{speaker.name}' to user")""",
    'logger.warning(f"pdftotext exit {rc} for {Path(file_path).name}")',
    'logger.info("starting for %s", [s["name"] for s in servers])',
    'logger.info(f"🗑️ Deleted speaker: {speaker_name}")',
    """logger.debug(f"KG: New entity '{name}' ({entity_type})")""",
    'logger.info(f"Handoff to {peer.remote_display_name}")',
    # A method call on content returns the content: not a route through.
    """logger.info(f"preview={c['text'].strip()[:80]!r}")""",
    'logger.info(f"{text.strip()}")',
])
def test_an_unclassified_name_counts_as_content(line):
    """Negative control: `.name` on a receiver nobody classified fires."""
    assert offenders_in(line, "services/agent_tools.py")


@pytest.mark.parametrize("line", [
    # Each of these was in the backend on 2026-11-21.
    """logger.info(f"Metadata extracted: {metadata.get('title', path.name)}")""",
    """logger.info(f"succeeded: {candidate_result.get('message', '')[:80]}")""",
    'logger.info(f"subject={parameters[\'subject\']}")',
    'logger.info("poll: %s", notification.get("title", "?"))',
])
def test_a_content_field_read_by_key_fires(line):
    assert offenders_in(line)


def test_a_classified_receiver_passes_only_in_its_own_file():
    line = 'logger.info("tool %s", tool.name)'
    assert offenders_in(line, "services/agent_tools.py") == []
    assert offenders_in(line, "services/rag_service.py") == [(1, "tool.name")]


@pytest.mark.parametrize("entry", sorted(_NAME_IS_NOT_CONTENT))
def test_every_name_classification_still_covers_a_log_line(entry, monkeypatch):
    """A stale entry would silently cover a new line in its file: without
    the entry, the file must report an offender -- the one it lets through."""
    monkeypatch.delitem(_NAME_IS_NOT_CONTENT, entry)
    assert _offenders_in_file(entry[0]), entry


def test_every_exemption_still_names_a_line_in_the_backend():
    """An exemption whose line is gone would silently cover a new one."""
    for (rel, line), _reason in _NOT_CONTENT.items():
        source = (_BACKEND / rel).read_text(encoding="utf-8")
        assert line in [x.strip() for x in source.splitlines()], (rel, line)
