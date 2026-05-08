"""Per-request voice-origin signal for the chat pipeline.

When a chat message arrives via the voice path (frontend recorded audio →
voice-server STT → ``WSChatMessage.speaker_embedding`` populated), this
flag is set ``True`` for the duration of the request. Downstream code
(plugin hooks, tool-execution gates, audit logging) reads it without
threading the signal through every method signature.

Why a ContextVar and not a kwarg:

The chat-pipeline boundary is the WS handler; downstream paths fan out
through agent loop → MCP execute → orchestrator sub-agents. Plumbing a
``voice_originated`` kwarg through every layer (and every plugin's
``pre_mcp_call`` / ``execute_tool`` handler) would be invasive and
incomplete — plugin-defined tools aren't required to accept new kwargs.
``contextvars`` propagates naturally to ``asyncio.create_task`` / ``gather``
children (Python 3.7+) and stays request-scoped.

Example::

    from utils.voice_context import voice_originated

    # WS handler, after parsing the inbound message:
    voice_originated.set(bool(msg.speaker_embedding))

    # Anywhere downstream:
    if voice_originated.get():
        ...

Plugins (e.g. Reva's voice-2FA gate) may also set their own ContextVars
for related signals (``voice_confirmed`` for re-issued tool calls after
user confirmation). Those live in the plugin module; only the platform
signal lives here.

Audit note (2026-05-08, voice-2FA Phase 2):

The chat path uses ``asyncio.create_task`` and ``asyncio.gather`` in
several places (chat_handler, agent_service, orchestrator). Both
APIs propagate the calling task's context to children by default —
no explicit ``contextvars.copy_context()`` needed. The risk surface
is non-default executors (``loop.run_in_executor``) and synchronous
threadpool dispatch. Audit results are documented in the Phase 2 PR.
"""
from contextvars import ContextVar

# True iff the current request originated from the voice path.
# Default False: text channel, internal callers, background workers.
voice_originated: ContextVar[bool] = ContextVar(
    "voice_originated", default=False,
)
