"""External integration clients used by the ha_glue layer.

- `ha_glue.integrations.homeassistant` — REST API client for Home
  Assistant instances. Entity map, keywords, state push/pull,
  service calls.
- `ha_glue.integrations.frigate` — REST API client for Frigate NVR
  (camera events, snapshots).

These were originally top-level `integrations/homeassistant.py` and
`integrations/frigate.py` in platform code. Moved to ha_glue in
Phase 1 W2 because both clients are 100% home-automation consumer
code — platform agent/chat loops don't reach for them directly.
Every reference now goes through the hook system established in
Phase 1 W1.2 (intent_fallback_resolve, build_entity_context,
validate_classified_intent, shutdown_finalize).
"""
