# Relaying a Spoken Message to a Person

"Sag ihm/ihr, das Essen ist fertig" / "Tell Eduard dinner is ready" → Renfield
finds where the person is and speaks the message on that room's speaker, with a
**privacy gate** so confidential content isn't overheard.

## Flow (LLM-orchestrated — nothing hardcoded)

The agent chains existing primitives; the sequence lives in the LLM, not in code:

```
"Sag ihm, das Essen ist fertig"
  1. resolve "ihm" → Eduard (from conversation history)
  2. internal.get_user_location("Eduard")        → room = "Arbeitszimmer"
  3. internal.announce_in_room(text=..., room_name="Arbeitszimmer",
                               for_users=["Eduard"], privacy=...)
        → Piper TTS → OutputRoutingService (room → device) → AudioOutputService
          (satellite tts_audio / DLNA / HA media_player)
```

`internal.announce_in_room(text, room_name, privacy, for_users, force)` is a
single TTS primitive (speak text in a room). It reuses the notification
subsystem's delivery path (`PiperService` → `OutputRoutingService` →
`AudioOutputService`). The person→room→device resolution and ordering are done
by the agent, so adding/altering the flow is prompt/config, not code.

## Routing

`config/agent_roles.yaml`: the **`presence`** role covers both "where is X" and
relaying a message, and carries `get_user_location` + `announce_in_room`. The
`conversation` role description explicitly **excludes** message relay (it has no
tools), so "Sag ihm …" no longer lands in the tool-less chat path. Routing is via
the LLM classifier reading these descriptions — there are no hardcoded trigger
phrases.

## Privacy gate (FAIL-CLOSED)

`privacy="public"` (default) is always announced. `privacy="personal"`
(confidential — the LLM sets this from the content, no keyword list) is spoken
aloud **only with positive proof the room is private**:

- recipients are known (`for_users`, resolved via presence),
- at least one person is *tracked* in the room, and
- **every** tracked occupant is a recipient.

| Situation | Result |
|---|---|
| Recipient alone | ✅ announced |
| Two recipients together in the room | ✅ announced |
| A non-recipient also present | 🔒 blocked |
| Nobody tracked in the room | 🔒 blocked |
| `personal` but no `for_users` given | 🔒 blocked |

On a block the tool returns `blocked="not_private"` (no TTS, no content). The
agent then announces a **neutral** note — *"<Name>, you have a message waiting —
shall I read it out?"* (`privacy="public"`, no content) — and if the recipient
**consents** in a follow-up turn, re-calls `announce_in_room` with `force=true`
to override. `force` is the consent-after-the-fact bypass.

## Camera occupancy check (catches untracked people)

BLE only sees people with a tracked device. To catch an **untracked** bystander
(a guest, a child without a phone), if the room has a satellite **with a camera**,
the personal-message gate — after the BLE check passes — takes an on-demand
snapshot and counts people via the vision model. If the camera sees **more
people than the tracked recipients present**, it's blocked (someone unknown is in
the room → the neutral "message waiting" + consent-to-`force` flow applies).

- **On-demand snapshot**: a new backend→satellite WS message `capture_snapshot` →
  the satellite captures a JPEG and replies `snapshot_result`. The image is
  **transient — never persisted**.
- **Vision**: `OllamaService.count_people_in_image` (a non-streaming call to the
  configured vision model) returns a headcount.
- **Config** (`ha_glue_settings`): `announce_camera_occupancy_check` (use it when
  a camera is in the room), `announce_snapshot_timeout`, and
  `announce_camera_check_fail_closed` — **default `false` (fail-open)**: if the
  snapshot or vision fails, the BLE decision stands; flip to `true` to block on an
  inconclusive camera check.
- The snapshot **only fires for `personal` messages whose BLE gate already
  passed** — so it doesn't activate the camera for ordinary public announcements.

### Inherent limitation

Even with the camera check, this is **best-effort**: a room without a camera
falls back to the BLE gate, the vision count can miss an occluded person, and
activating a camera to protect privacy is itself a tradeoff (mitigated by never
storing the image). It reduces — but does not eliminate — the chance a personal
message is overheard.

It can also **false-block in the safe direction**: if two intended recipients
are in the room but one carries no tracked device, the camera sees 2 while only 1
is tracked → blocked (→ the "message waiting" + `force` flow). Annoying but
private; `force` resolves it.

## TTS-Audio-Auslieferung an Renderer (DLNA / HA media_player)

Wenn die Ansage auf einem DLNA-Renderer (statt einem Renfield-Satellite) landet,
synthetisiert das Backend das WAV, legt es im TTS-Cache ab und übergibt dem
Renderer eine **URL** (`/api/voice/tts-cache/{id}.wav`), die der Renderer dann
selbst **abruft**. Die URL baut `AudioOutputService._get_backend_url()` aus
`ADVERTISE_SCHEME` + `ADVERTISE_HOST` (+ `ADVERTISE_PORT`).

**Auslieferung (Prod): `http://renfield.local/api/voice/tts-cache/{id}.wav`.**
Plain http (kein https) — bedient von der `backend-tts-cache-http` IngressRoute
(eigener Route auf dem `web`-Entrypoint, ohne den `http→https`-Redirect). Bewusst
**kein https**, weil Samsung-TVs das self-signed Zertifikat nicht akzeptieren;
http funktioniert dagegen auf **allen** Renderern. Auflösung von `renfield.local`:
Linn/Samsung per Router-DNS, HiFiBerry per `/etc/hosts` (siehe
`provision-hifiberry.yml` — der CA-Teil ist für http nicht mehr nötig, nur der
`/etc/hosts`-Eintrag).

**Zwei stille Fehlerquellen, beide auf UNSERER Seite (gemessen):**
1. **Falsche URL/Scheme.** Früher `http://renfield.local:80` hinter dem
   Traefik-`http→https`-Redirect → der Renderer holte die URL nie (0 GETs); der
   event-silent HiFiBerry meldete trotzdem `state=playing`. Einzige Bodenwahrheit
   ist das Backend-Access-Log.
2. **DLNA-Resource war non-compliant** → Samsung lehnte mit UPnP **716
   „Resource not found"** ab (Linn/HiFiBerry sind lenient und schluckten es). Drei
   Bugs, alle gefixt:
   - **HEAD → 405.** DLNA-Renderer schicken ein HEAD vor dem GET; die
     `/api/voice/tts-cache`-Route war GET-only. Jetzt GET **+ HEAD**.
   - **Falscher MIME.** Das DIDL defaultete auf `audio/flac`; das `GetProtocolInfo`
     des TVs listet **`audio/x-wav`** (nicht `audio/wav`/`flac`). Route liefert jetzt
     `Content-Type: audio/x-wav` + `Content-Length`, und der DLNA-Play-Pfad gibt
     `mime_type=audio/x-wav` mit (→ passendes protocolInfo).
   - **Keine Extension.** URL endet jetzt auf `.wav` (Route akzeptiert + strippt sie).

**Pro-Renderer-Status (gemessen über `http://renfield.local`):**

| Renderer | löst `renfield.local` | spielt TTS (http) | Setup |
|---|---|---|---|
| Linn / openHome | ✅ Router-DNS | ✅ | keins |
| Samsung TV (Q60CA / 8 Series) | ✅ Router-DNS | ✅ (nach HEAD/x-wav/.wav-Fix) | keins |
| HiFiBerry (gstreamer) | per `/etc/hosts` | ✅ | `/etc/hosts`-Eintrag (`provision-hifiberry.yml`); **CA nicht mehr nötig** |
| 55" Signage Flip | ✅ | ⚠️ eigener Quirk (404 im dlna-mcp-Confirm) — separat |

Mit `ADVERTISE_SCHEME=http` (Default) byte-identisch zur Pre-https-Episode.
Hintergrund HiFiBerry-`.local`: nsswitch `hosts: files resolve [!UNAVAIL=return] dns`
→ systemd-`resolve` greift `.local` als mDNS (NOTFOUND vor `dns`); `curl`
(c-ares) umgeht das, gstreamer (`getaddrinfo`) nicht → daher der `/etc/hosts`-Eintrag.

## Where it lives

- Tool + gate: `ha_glue/services/internal_tools.py` (`_announce_in_room`, `internal.announce_in_room`)
- Delivery primitives (reused): `PiperService`, `OutputRoutingService.get_audio_output_for_room`, `AudioOutputService.play_audio`
- Presence: `PresenceService.get_room_occupants` / `find_user_by_name`
- Routing: `config/agent_roles.yaml` (`presence` / `conversation` roles)
- Agent guidance: `prompts/agent.yaml` (relay + confidentiality rules, DE + EN)
- Tests: `tests/backend/test_internal_tools.py::TestAnnounceInRoom`
