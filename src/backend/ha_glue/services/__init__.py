"""Service-layer ha-glue modules.

These services consume platform interfaces (LLM dispatch, hook system,
SQLAlchemy models) and provide HomeAssistant-specific behavior on top.
None of these modules should be imported by platform code.

Phase 1 Week 1.2: only `intent_fallback` is populated. Week 2 will move
the rest of the ha-glue services here (presence, room, audio output,
satellites, paperless audit, etc.) per
`docs/architecture/renfield-platform-boundary.md` in the parent Reva repo.
"""
