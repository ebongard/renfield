---
paths:
  - "src/backend/ha_glue/services/audio_output_service.py"
  - "src/backend/ha_glue/services/tts_equalizer.py"
  - "src/backend/ha_glue/services/output_routing_service.py"
---
# Audio output routing & TTS sound profile

Loaded only when the output-routing / audio-output / TTS-equalizer services are read.
Long form: `docs/OUTPUT_ROUTING.md` (routing algorithm, "TTS-Klangprofil"), `docs/design/output-providers.md`.

## Per-device TTS sound profile
- Column `room_output_devices.tts_eq_profile`, migration `pc20260920_output_tts_eq`; profiles live in
  `ha_glue/services/tts_equalizer.py`. `NULL` = off (behaviour as before).
- Why: the household voice carries ~80 % of its energy in 80–300 Hz — fine on a satellite's small speaker, flat and
  muffled on a hi-fi system.
- `hifi_speech` = high-pass 120 Hz + high shelf +7 dB @ 2.5 kHz. **A profile is a NAME, not knobs** — do not expose
  frequencies/gains in the API or UI; a new profile is a code entry. An unknown name → 422.
- Applied to **DLNA / HA players only, never to Renfield devices** (satellites, browser get the unprocessed answer),
  and only in the TTS path — music on the same device is untouched.
- **Fail-safe:** any equalizer error plays the UNPROCESSED file — a sound profile must never cost the answer
  (`apply_profile` returns its input on error; `AudioOutputService._apply_sound_profile` runs it off the event loop
  via `asyncio.to_thread`).
- **PATCH semantics:** an omitted key = unchanged, `null` = profile OFF (the `_UNSET` sentinel in
  `output_routing_service`). Do not collapse the two into `None`.

## Generic output providers (`OUTPUT_PROVIDERS_ENABLED`, opt-in; off = byte-identical legacy routing)
- Pluggable room media/control targets via an `output_provider:` stanza in `mcp_servers.yaml`.
  **A new brand = config, not code.**
- The contract translation lives entirely in the Renfield backend (`ha_glue/services/output_providers.py`), never in
  the (third-party) MCP servers.

## Routing order
Devices are tried by (priority ASC, audio quality DESC, id ASC): the manual priority always wins, quality only breaks
ties (external renderer > HA speaker > Renfield tablet/satellite). Busy + `allow_interruption=False` → next device;
nothing available → fall back to the input device.
