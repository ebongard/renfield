# Album Queue Playback — Analyse & Optionen

> **Status:** Entscheidung: DLNA MCP-Server (Option H)
> **Issue:** #197
> **Datum:** 2026-02-20 (aktualisiert: 2026-02-21)

## Problemstellung

Wenn ein Benutzer "Spiel das Album X im Arbeitszimmer" sagt, spielt Renfield nur den ersten Track. Der Agent-Loop erkennt das Album korrekt, ruft `get_album_tracks` auf, erhaelt alle Tracks — aber `internal.play_in_room` kann nur **eine einzelne URL** an Home Assistant senden.

> **Update (v2.12.2):** `internal.play_in_room` ist nicht mehr HA-only — es verzweigt jetzt bei `target_type == "dlna"` auf `_play_url_on_dlna()` (`mcp.dlna.play_tracks`, Ein-Track-Queue) statt auf `media_player.play_media`. Vorher führte ein DLNA-Raum dort zu `KeyError: 'entity_id'`. Mehrere Tracks (Album-Queue) laufen weiterhin über `internal.play_album_on_dlna` (Option H unten).

Ziel: Alle Tracks eines Albums sequentiell abspielen, ueber beliebige Wiedergabegeraete.

## Architektur-Kontext

```
User → Agent Loop → get_album_tracks(album_id) → [Track 1, Track 2, ..., Track N]
                  → internal.play_in_room(media_url=Track1, queue=[Track2..N])
                  → HA media_player.play_media(entity_id, url)
                  → Wiedergabegeraet (Apple TV, Sonos, Chromecast, ...)
```

Renfield nutzt Home Assistant als Abstraktionsschicht fuer Wiedergabegeraete. Die `media_player.play_media` Service-API unterstuetzt einen `enqueue`-Parameter, aber die Unterstuetzung haengt vom jeweiligen HA-Integrationstyp ab.

## Untersuchte Ansaetze

### 1. HA `media_player.play_media` mit `enqueue` (gescheitert)

**Erster Implementierungsversuch (PR #215, gemergt und deployed).**

HA's `media_player.play_media` akzeptiert:
```json
{"extra": {"enqueue": "play"}}   // Queue ersetzen, abspielen
{"extra": {"enqueue": "add"}}    // An Queue anhaengen
```

**Ergebnis:** Alle 11 Enqueue-Aufrufe scheiterten mit HTTP 500.

**Root Cause:** Apple TV (pyatv) unterstuetzt `MEDIA_ENQUEUE` **nicht**. Das Feature-Flag fehlt komplett in der HA Apple TV Integration. pyatv kann nur eine einzelne URL streamen (`stream_url()`). Der `enqueue`-Parameter wird als `**kwargs` an pyatv's `stream_url()` durchgereicht, was den HTTP 500 verursacht.

**Geraete-Support fuer `enqueue`:**

| HA-Integration | MEDIA_ENQUEUE | Anmerkungen |
|---|---|---|
| Apple TV (pyatv) | **Nein** | Nur single-URL via AirPlay |
| HomePod (pyatv) | **Nein** | Gleiches Problem |
| Sonos | **Ja** | Native Queue-Unterstuetzung |
| Google Cast / Chromecast | **Ja** | Native Queue via Cast-Protokoll |
| DLNA DMR (`dlna_dmr`) | **Nein** | Library kann es, Integration nutzt es nicht (s.u.) |
| Music Assistant | **Ja** | Aber kein Apple TV / AirPlay Support |

### 2. Jellyfin Playlist API

**Untersucht:** Kann Jellyfin eine Playlist als einzelnen Audio-Stream ausliefern?

**Ergebnis: Sackgasse.**

Jellyfin hat eine vollstaendige Playlist-API:
- `POST /Playlists` — Playlist erstellen (Name, Item-IDs, User-ID)
- `POST /Playlists/{id}/Items` — Items hinzufuegen
- `GET /Playlists/{id}/Items` — Items abrufen

Aber: **Kein Endpoint liefert eine Playlist als kontinuierlichen Audio-Stream.** Jeder Streaming-Endpoint arbeitet auf einer einzelnen Track-ID:
- `/Audio/{itemId}/stream` — einzelner Track
- `/Audio/{itemId}/universal` — einzelner Track, Auto-Codec
- `/Audio/{itemId}/master.m3u8` — einzelner Track als HLS-Segmente

Ein Community-Post der nach Playlist-Streaming als HLS fragte wurde beantwortet mit: *"Not with plain Jellyfin."*

**InstantMix-API:** Existiert (`GET /Albums/{id}/InstantMix`), liefert aber nur eine JSON-Liste von Track-Metadaten zurueck — keinen Stream.

### 3. DLNA / UPnP

**Untersucht:** DLNA als universelles Protokoll fuer sequentielle Wiedergabe.

#### DLNA Queue-Mechanismus (`SetNextAVTransportURI`)

UPnP AVTransport definiert zwei Aktionen:
- `SetAVTransportURI` — aktuellen Track setzen + abspielen
- `SetNextAVTransportURI` — naechsten Track vorpuffern (gapless Uebergang)

```
Control Point                          Renderer
     |-- SetAVTransportURI(Track 1) ------>|
     |-- Play() --------------------------->|  [Track 1 startet]
     |-- SetNextAVTransportURI(Track 2) -->|  [Renderer puffert Track 2]
     |  [Track 1 endet]                    |  [Auto-Transition → Track 2]
     |-- SetNextAVTransportURI(Track 3) -->|  [naechster Track nachladen]
```

Nur **ein** naechster Track kann vorgeladen werden (single look-ahead slot). Der Control Point ist verantwortlich fuer das sequentielle Nachladen.

#### Jellyfin DLNA Server

Jellyfin hat einen DLNA-Server (seit v10.9 als Plugin). Er fungiert als Media Server UND Control Point ("Play To").

**Status: Kaputt.** Beim Push einer Playlist an einen DLNA-Renderer spielt nur der erste Track, dann stoppt die Wiedergabe. Bekannter Bug seit 2019 (Issues #888, #2028, #2226). Der Code ruft `SetNextAVTransportURI` auf, aber die Implementation hat persistente Fehler. Alle drei Queue-Issues wurden als "stale" geschlossen ohne Fix.

#### HA `dlna_dmr` Integration

HA hat eine DLNA Media Renderer Integration (`dlna_dmr`). **Sie unterstuetzt `MEDIA_ENQUEUE` nicht.**

Jedoch: Die zugrundeliegende Python-Library `async-upnp-client` HAT volle `SetNextAVTransportURI`-Unterstuetzung:
```python
# In async_upnp_client/profiles/dlna.py:
async def async_set_next_transport_uri(self, media_url, media_title, meta_data=None):
    # Calls SetNextAVTransportURI
```

Die Plumbing existiert, aber HA's Integration nutzt es nicht. Ein PR an HA Core waere noetig.

#### Apple TV als DLNA Renderer

**Apple TV unterstuetzt kein DLNA.** Nur AirPlay. Drittanbieter-Apps (VLC, Infuse) koennen als DLNA-Clients browsen, aber Apple TV erscheint nicht als DLNA-Renderer im Netzwerk.

#### Renderer-Kompatibilitaet (theoretisch)

`SetNextAVTransportURI` ist laut Berichten **inkonsistent implementiert**:
- Viele Renderer advertisen es aber scheitern beim Track-Wechsel
- Bose-Geraete bleiben haengen
- Manche ignorieren den Aufruf still
- Unbekannte Track-Dauer deaktiviert den Mechanismus
- Sonos unterstuetzt es, aber nur am Group Coordinator mit aktivierter Queue (sonst Error 712)

#### Netzwerk-Scan (2026-02-20)

SSDP-Discovery im lokalen Netzwerk ergab 6 DLNA-MediaRenderer. Fuer jeden wurde die AVTransport-SCPD auf `SetNextAVTransportURI`-Support geprueft:

| Geraet | IP | Modell | SetNextAVTransportURI |
|---|---|---|---|
| **HiFiBerry** | 192.168.1.191 | HiFiBerryOS | **Ja** |
| **Samsung TV 65"** | 192.168.1.254 | UE65MU8009 | **Ja** |
| **Samsung Flip 55"** | 192.168.1.45 | WM55B | **Ja** |
| **Denon AVR** | 192.168.1.185 | AVC-X4700H (HEOS) | **Ja** |
| **Linn Majik DSM** | 192.168.1.121 | Wohnzimmer | **Nein** (OpenHome) |
| **Linn Sneaky DSM** | 192.168.1.195 | Ben's Zimmer | **Nein** (OpenHome) |

**Ergebnis:** 4 von 6 Renderern unterstuetzen `SetNextAVTransportURI`. Die Linn-Geraete nutzen stattdessen das OpenHome-Playlist-Protokoll.

#### HiFiBerry als DLNA-Renderer + MPD (Live-Test)

Der HiFiBerry laeuft unter HiFiBerryOS und bietet **parallel** mehrere Player:
- **DLNA Renderer** (UPnP) — per SSDP im Netzwerk sichtbar
- **MPD** (Port 6600) — volle Queue-Unterstuetzung mit HTTP-URLs
- **AirPlay** (shairport-sync) — AirPlay 1+2 Receiver
- **Spotify Connect** — Spotify-App sieht ihn als Speaker

**Live-Test (2026-02-20):** 3 Jellyfin-Track-URLs (`/Audio/{id}/stream?static=true`) direkt per MPD-Protokoll in die Queue geladen und abgespielt. **Funktioniert einwandfrei:**
- Sofortige Wiedergabe (<1s Latenz)
- 44100Hz/24bit/Stereo, 192kbps
- Track-Metadata (Artist, Title, Album, Genre) korrekt gelesen
- Skip/Next funktioniert
- audiocontrol2 REST-API zeigt Metadata inkl. MusicBrainz-IDs

#### HA-Integrationen fuer HiFiBerry/DLNA (kein Queue-Support)

Geprueft wurden drei HA-Integrationen — **keine** unterstuetzt `MEDIA_ENQUEUE`:

| HA-Integration | Problem |
|---|---|
| `dlna_dmr` | `play_media` nutzt nur `SetAVTransportURI`. `SetNextAVTransportURI` wird nicht aufgerufen. `MEDIA_ENQUEUE` fehlt im Feature-Bitmask. |
| `mpd` | `play_media` ruft `clear()` vor jedem Play auf — Queue wird geloescht. `MEDIA_ENQUEUE` fehlt. |
| HiFiBerry HACS (Beta) | Nur audiocontrol2 REST (Play/Pause/Next/Volume). Kein Queue-Management. |

**Fazit:** Das Queue-Problem liegt nicht an den Geraeten, sondern an HA's Integrationsschicht. Die Geraete koennen Queue — HA exponiert es nicht.

### 4. MRP (Media Remote Protocol) / Companion Link

**Untersucht via `homey-apple` Repository** ([github.com/basmilius/homey-apple](https://github.com/basmilius/homey-apple)).

Das Projekt nutzt 8 private `@basmilius/apple-*` Packages:

| Package | Funktion |
|---|---|
| `apple-airplay` | AirPlay-Protokoll |
| `apple-companion-link` | Companion Link (Apple TV Navigation) |
| `apple-raop` | RAOP (Remote Audio Output Protocol) |
| `apple-audio-source` | Audio Source — agiert als Audio-Quelle |
| `apple-devices` | Device Discovery |
| `apple-encoding` / `encryption` / `common` | Infrastruktur |

**Befund:** Die App bietet Remote Control (Play, Pause, `NextInContext`, `PreviousInContext`, Volume) und Now-Playing Metadata. **Kein `play_url`, kein Queue-Management, kein Media-Push.**

`NextInContext` / `PreviousInContext` sagen dem Apple TV "naechster/vorheriger Track im aktuellen Kontext" — funktioniert aber nur wenn die Apple TV App (z.B. Apple Music) bereits eine Queue hat. Von aussen kann keine Queue aufgebaut werden.

**Relevant:** Die Existenz von `apple-raop` und `apple-audio-source` zeigt, dass RAOP Direct Streaming als eigenstaendige Library implementierbar ist (s. Option G).

### 5. `node-appletv-x` / MRP Queue

**Untersucht:** [github.com/stickpin/node-appletv-x](https://github.com/stickpin/node-appletv-x) — Node.js Implementation des Media Remote Protocol.

Hat einen `queue`-Befehl, dieser ist aber **read-only**: Liest die aktuelle Playback Queue aus, kann sie nicht setzen oder manipulieren. MRP sendet kontinuierlich `PlaybackQueueRequestMessages` (1-2x/Sekunde) fuer den State, aber es gibt keinen dokumentierten `SetQueue`- oder `InsertQueueItem`-Befehl.

### 6. MCP Apple Music Server (`mcp-applemusic`)

**Untersucht:** [github.com/samwang0723/mcp-applemusic](https://github.com/samwang0723/mcp-applemusic) — MCP-Server fuer Apple Music Steuerung.

Das Projekt exponiert 9 MCP-Tools fuer Apple Music:

| Tool | Funktion |
|---|---|
| `apple-music-play` / `pause` / `next-track` | Basis-Steuerung |
| `apple-music-search-song` / `search-album` / `search-artist` | Bibliothek durchsuchen |
| `apple-music-search-and-play` | Suche + spiele ersten Treffer |
| `apple-music-set-volume` | Lautstaerke (macOS System-Volume!) |
| `apple-music-get-current-track` | Aktueller Track |

**Ergebnis: Fuer Renfield nicht nutzbar.**

1. **macOS-only:** Kommuniziert ausschliesslich via `osascript` (AppleScript) mit der lokalen `Music.app`. Nicht in Docker lauffaehig.
2. **Kein Queue-Management:** Kein Add-to-Queue, kein Play-Playlist, kein Shuffle/Repeat. `search-and-play` spielt immer nur den ersten Treffer.
3. **Kein AirPlay Target:** Steuert nur die lokale Music.app — keine Moeglichkeit, ein AirPlay-Zielgeraet (Apple TV, HomePod) auszuwaehlen.
4. **Nur lokale Bibliothek:** Sucht in `playlist "Library"`, nicht im Apple Music Streaming-Katalog.
5. **Unreif:** v0.1.0, ein einziger Commit, 2 Stars.

Selbst wenn macOS verfuegbar waere: Ohne Queue-Management und ohne AirPlay-Target-Auswahl loest es das Album-Playback-Problem nicht.

### 7. pyatv Capabilities

pyatv ([pyatv.dev](https://pyatv.dev/)) ist die Library hinter HA's Apple TV Integration:
- `stream.play_url(url)` — einzelne URL via AirPlay abspielen
- `stream.stream_file(path)` — einzelne Datei via RAOP streamen
- Kein Queue, keine Playlist, kein Multi-Track
- RAOP-Internals existieren (Audio-Encoding, RTSP-Session), sind aber nicht als Public API fuer kontinuierliches Streaming ausgelegt

## Optionen

### Option A: HA-Native Enqueue (fuer unterstuetzte Geraete)

`media_player.play_media` mit `enqueue` korrekt nutzen — `enqueue` als Top-Level Key in `service_data`, nicht innerhalb von `extra`.

```python
# Erster Track:
service_data = {
    "media_content_id": track1_url,
    "media_content_type": "music",
    "enqueue": "play",
}
# Weitere Tracks:
service_data = {
    "media_content_id": trackN_url,
    "media_content_type": "music",
    "enqueue": "add",
}
```

| | |
|---|---|
| **Geraete** | Sonos, Chromecast/Google Cast |
| **Nicht unterstuetzt** | Apple TV, HomePod, DLNA (`dlna_dmr`) |
| **Vorteile** | Nativ, gapless, Track-Info/Artwork korrekt, Skip/Next funktioniert, kein eigener State |
| **Nachteile** | Apple TV faellt komplett raus |
| **Aufwand** | Minimal — bestehender Code, nur Position von `enqueue` korrigieren + Device-Capability-Check |

**Implementation:** Vor dem Enqueue `supported_features` des Media Players via HA State API pruefen. `MEDIA_ENQUEUE` = Bitmask `0x200000` (2097152). Nur wenn gesetzt, Enqueue nutzen.

### Option B: ffmpeg Concat Proxy (Server-Side Stream-Zusammenfuehrung)

Neuer Backend-Endpoint, der alle Track-URLs per ffmpeg zu einem einzigen Audio-Stream zusammenfuehrt.

```
Agent → POST /api/media/album-stream
        Body: {track_urls: [...], format: "mp3"}
     ← {stream_url: "http://renfield:8000/api/media/stream/abc123"}

HA ← media_player.play_media(stream_url)
Apple TV ← spielt einen langen "Track" (= das ganze Album)
```

ffmpeg concat demuxer:
```bash
ffmpeg -f concat -safe 0 -protocol_whitelist file,http,https,tcp,tls \
  -i tracklist.txt -c:a libmp3lame -b:a 320k -f mp3 pipe:1
```

| | |
|---|---|
| **Geraete** | Alle (universelle URL) |
| **Vorteile** | Funktioniert mit jedem Player inkl. Apple TV, eine URL = ein Album |
| **Nachteile** | Kein Skip/Next zwischen Tracks, keine Track-Info im Player (ein "Track"), CPU-Last durch Transcoding, Startup-Latenz (Puffer noetig), Cleanup-Logik fuer temporaere Streams |
| **Aufwand** | Mittel — neuer FastAPI-Endpoint, ffmpeg-Subprocess, Stream-Lifecycle. ffmpeg ist im Docker-Container bereits installiert. |

**Variante mit Kapitelmarkern:** ffmpeg kann Chapter-Metadata einbetten (`-map_chapters`), die manche Player anzeigen. Apple TV ignoriert diese allerdings bei AirPlay.

### Option C: Custom DLNA Control Point

Eigener Service nutzt `async-upnp-client` direkt (ohne HA als Mittelsmann) und steuert DLNA-Renderer via `SetNextAVTransportURI`.

```python
from async_upnp_client.profiles.dlna import DmrDevice

# Track 1 abspielen
await dmr.async_set_transport_uri(track1_url, "Track 1")
await dmr.async_media_play()

# Track 2 vorpuffern (gapless)
await dmr.async_set_next_transport_uri(track2_url, "Track 2")

# Bei Track-Wechsel (LAST_CHANGE Event): Track 3 vorpuffern
await dmr.async_set_next_transport_uri(track3_url, "Track 3")
```

| | |
|---|---|
| **Geraete** | Smart TVs (Samsung, LG, Sony), Denon/Marantz AVRs, WiiM, Kodi, Xbox, PlayStation |
| **Nicht unterstuetzt** | Apple TV (kein DLNA), Chromecast (kein DLNA), Sonos (proprietaere UPnP-Erweiterungen) |
| **Vorteile** | Gapless, Track-Info korrekt, volle Kontrolle, Event-basiert (`LAST_CHANGE` via UPnP SUBSCRIBE — kein Polling) |
| **Nachteile** | Apple TV faellt raus. Renderer-Support inkonsistent. Eigenentwicklung noetig. |
| **Aufwand** | Hoch — UPnP Control Point, Device Discovery, Event Subscription, Fehlerbehandlung fuer kaputte Renderer |

### Option D: Jellyfin Playlist API

Playlist per API erstellen, als Stream abrufen.

| | |
|---|---|
| **Ergebnis** | **Sackgasse.** Jellyfin kann Playlists erstellen/verwalten, aber kein Endpoint liefert eine Playlist als Audio-Stream. Alle Streaming-Endpoints arbeiten auf einzelnen Track-IDs. |

### Option E: Icecast/Liquidsoap (Internet-Radio)

Liquidsoap generiert on-demand einen Icecast-Stream aus einer Track-Liste.

| | |
|---|---|
| **Geraete** | Alle (universelle URL) |
| **Vorteile** | Standard-Streaming, ICY-Metadata moeglich (Track-Titel im Player) |
| **Nachteile** | Massiv overengineered. Zwei zusaetzliche Services (Icecast + Liquidsoap). Kein Skip/Next. Startup-Latenz. |
| **Aufwand** | Sehr hoch |

### Option F: Anderes Wiedergabegeraet

Apple TV durch ein Geraet ersetzen, das Queue nativ unterstuetzt.

| Geraet | Enqueue via HA | DLNA | Preis |
|---|---|---|---|
| Sonos | Ja (nativ) | Nein (proprietaer) | ~200-500 EUR |
| Chromecast Audio (gebraucht) | Ja (Google Cast) | Nein | ~30-50 EUR |
| WiiM Mini/Pro | Nein (kein HA Enqueue) | Ja (`SetNextAVTransportURI`) | ~80-150 EUR |
| Denon/Marantz AVR | Nein (kein HA Enqueue) | Ja | bereits vorhanden? |

### Option G: RAOP Direct Audio Streaming

Renfield agiert selbst als AirPlay-Audio-Quelle und streamt dekodierte Audiodaten direkt an Apple TV/HomePod ueber RAOP (AirTunes).

```
Jellyfin API → Audio-Download → Renfield (RAOP Client) → Apple TV/HomePod
                                     ↑
                              Dekodiert Audio,
                              kontrolliert Track-Uebergaenge,
                              sendet Metadata
```

Das ist das gleiche Prinzip wie wenn ein iPhone per AirPlay ein Album auf Apple TV abspielt — die Quelle kontrolliert den Audio-Stream komplett.

| | |
|---|---|
| **Geraete** | Apple TV, HomePod, alle AirPlay-Geraete |
| **Vorteile** | Gapless Transitions, volle Queue-Kontrolle, Track-Info/Artwork, Skip/Next, kein Polling |
| **Nachteile** | Erheblicher Aufwand. `@basmilius/apple-raop` ist privat. pyatv hat RAOP-Internals aber keine Public API fuer kontinuierliches Multi-Track-Streaming. Audio-Dekodierung, Resampling, ALAC-Encoding, Encryption, RTSP-Session-Management noetig. |
| **Aufwand** | Sehr hoch — im Prinzip einen AirPlay-Player bauen |

### Option H: DLNA MCP Control Point (gewaehlt)

Eigenstaendiger MCP-Server der als UPnP/DLNA **Control Point** agiert. Nutzt `async-upnp-client` um DLNA-Renderer direkt zu steuern — unabhaengig von HA. Jellyfin's eingebauter DLNA-Server liefert die Medien.

```
                    ┌─────────────────────────┐
                    │  Jellyfin DLNA Server    │
                    │  (Medien bereitstellen)  │
                    └──────────┬──────────────┘
                               │ Media-URLs
┌──────────┐    MCP Tools    ┌─┴──────────────────────┐    UPnP AVTransport
│  Agent   │ ──────────────→ │  DLNA MCP Server       │ ──────────────────→ DLNA Renderer
│  Loop    │  play_album()   │  (Control Point)       │  SetAVTransportURI   (HiFiBerry,
│          │  list_renderers │  - Device Discovery    │  SetNextAVTransportURI Samsung,
│          │  stop/next/prev │  - Queue State Machine │  Play/Stop/Pause      Denon, ...)
└──────────┘                 │  - Event Subscription  │
                             └────────────────────────┘
```

**MCP Tools:**

| Tool | Funktion |
|---|---|
| `list_renderers` | SSDP-Discovery: verfuegbare DLNA-Renderer im Netzwerk |
| `play_tracks` | Album/Playlist abspielen: Track 1 via `SetAVTransportURI`, Track 2+ via `SetNextAVTransportURI`, Event-basiertes Nachladen |
| `stop` | Wiedergabe stoppen |
| `pause` / `resume` | Pause/Fortsetzen |
| `next` / `previous` | Naechster/vorheriger Track in der Queue |
| `get_status` | Aktueller Track, Position, Queue-Inhalt, Renderer-State |
| `set_volume` | Lautstaerke setzen (0-100). Geht **direkt** ueber RenderingControl `SetVolume` mit rohen 0-100-Werten (NICHT `DmrDevice.async_set_volume_level`, das bei Renderern mit absurder Range — Linn meldet 2^31-1 — auf volle Lautstaerke springt). Sane advertised Range wird respektiert, bogus Range als 0-100 behandelt. |
| `get_volume` | Aktuelle Lautstaerke lesen (0-100, `None` falls Renderer sie nicht meldet) — direkt via RenderingControl `GetVolume`; cached aus Events/`set_volume`. Backt die relative Lautstaerke (`internal.media_control` mit `volume_step`). |
| `set_mute` | Stummschalten (`mute=true`) / aufheben (`mute=false`) via nativem RenderingControl `SetMute` — Renderer stellt die vorige Lautstaerke beim Unmute selbst wieder her (kein gespeicherter Wert). Capability via Action-Presence (nicht `has_volume_mute`). |

> **`internal.media_control` (Renfield-Backend, raumbasiert)** kapselt diese Tools: `action` = `stop`/`pause`/`resume`/`next`/`previous`/`volume` (absolut `volume=0-100` oder relativ `volume_step=±N` Prozentpunkte)/`mute`/`unmute`/`status`. Der Agent nutzt IMMER `internal.media_control` mit `room_name` (Raumaufloesung + HA-vs-DLNA-Branch); `mcp.dlna.set_volume` ist nicht mehr LLM-facing. `status` liefert den aktuellen Track/State (DLNA: `get_status`; HA: `media_player`-State).

**Queue State Machine (pro Renderer):**

```
                    ┌─────────┐
          play_tracks()       │
                    ▼         │
              ┌──────────┐   │ LAST_CHANGE
              │ Playing   │───┘ (Track endet)
              │ Track N   │
              └────┬─────┘
                   │ SetNextAVTransportURI(Track N+1)
                   ▼
              ┌──────────┐
              │ Preloaded │  (Renderer puffert naechsten Track)
              │ Track N+1 │
              └────┬─────┘
                   │ Auto-Transition
                   ▼
              ┌──────────┐
              │ Playing   │  → SetNextAVTransportURI(Track N+2) → ...
              │ Track N+1 │
              └──────────┘
```

Event-basiert via UPnP SUBSCRIBE (`LAST_CHANGE`) — kein Polling im Normalbetrieb.

**`LAST_CHANGE`-Parsing-Fix + GetTransportInfo-Poll-Backstop (v2.12.6).**
`async-upnp-client` liefert den `LAST_CHANGE`-Callback eine **Liste** von
`UpnpStateVariable`-Objekten (je `.name`/`.value`), keinen Dict. Der Handler rief
`.get()` darauf auf und warf bei **jedem** Event `AttributeError` — der gecachte
`_transport_state` wurde nie gesetzt. Folgen (vor v2.12.6 still kaputt):

- `get_status` meldete dauerhaft `unknown` (z. B. am Arbeitszimmer-HiFiBerry — der
  Renderer ist **nicht** event-still, der Handler ist nur immer abgestürzt).
- Die Gapless-/Auto-Advance-Erkennung im Handler lief nie → Alben über mehrere
  Tracks luden den übernächsten Track nicht vor bzw. sprangen auf Nicht-SetNext-
  Renderern nicht weiter.

Fix: Liste in eine `name→value`-Map falten. Zusätzlich abgesichert, weil der Fix
die zuvor toten `STOPPED`-Zweige scharf schaltet — Track-Ende/Queue-Ende werden
nur gewertet, wenn der Renderer **vorher tatsächlich `PLAYING`** erreicht hat
(ein transientes `STOPPED`/`NO_MEDIA_PRESENT` in der Anlaufphase darf Track 1
nicht überspringen oder eine Single-Track-Session vorzeitig abräumen); doppelte
`STOPPED`-Events werden per `_advancing`-Flag entprellt.

**GetTransportInfo-Poll als Backstop:** `get_status` ruft vor dem Status-Mapping
einen per `asyncio.wait_for` auf 3 s budgetierten `GetTransportInfo`-Poll auf und
liest danach den von der Library gepflegten `transport_state` (auch wenn der
aktive Refresh scheitert — das Event-Abo hält den Wert frisch). So liefert
`get_status` auch dann einen echten State, falls ein Renderer wider Erwarten keine
Events schickt. Das Budget verhindert, dass ein hängender Renderer das Tool
zehnersekundenlang blockiert.

| | |
|---|---|
| **Geraete** | HiFiBerry, Samsung TVs, Denon/Marantz AVRs, alle DLNA-Renderer mit `SetNextAVTransportURI` |
| **Nicht unterstuetzt** | Apple TV (kein DLNA), Linn (kein `SetNextAVTransportURI`, nutzt OpenHome) |
| **Vorteile** | Gapless, Track-Info (DIDL-Lite Metadata), Skip/Next, kein Polling, MCP-Paradigma erhalten, Jellyfin DLNA-Server liefert Medien, deckt 4/6 vorhandene Renderer ab |
| **Nachteile** | Apple TV/Linn fallen raus. UPnP Event-Subscription braucht langlebigen Prozess. |
| **Aufwand** | Mittel — `async-upnp-client` macht die schwere Arbeit, Queue State Machine + MCP-Wrapper |

## Vergleichsmatrix

| Option | Apple TV | Sonos/Cast | DLNA | Skip/Next | Track-Info | Gapless | Aufwand |
|---|---|---|---|---|---|---|---|
| **A: HA Enqueue** | Nein | Ja | Nein | Ja | Ja | Ja | Minimal |
| **B: ffmpeg Concat** | Ja | Ja | Ja | Nein | Nein | Ja* | Mittel |
| **C: Custom DLNA** | Nein | Nein | Ja | Ja | Ja | Ja | Hoch |
| **D: Jellyfin Playlist** | — | — | — | — | — | — | Sackgasse |
| **E: Icecast** | Ja | Ja | Ja | Nein | Teilw. | Ja | Sehr hoch |
| **F: Anderes Geraet** | Entfaellt | Ja | Ja | Ja | Ja | Ja | Hardware |
| **G: RAOP Streaming** | Ja | Nein** | Nein | Ja | Ja | Ja | Sehr hoch |
| **H: DLNA MCP Server** | Nein | Nein*** | **Ja** | **Ja** | **Ja** | **Ja** | **Mittel** |

*\* ffmpeg Concat: gapless innerhalb des Streams, aber als ein einzelner "Track" ohne Kapitel*
*\*\* RAOP: nur AirPlay-Geraete, nicht Sonos/Cast/DLNA*
*\*\*\* Sonos hat proprietaere UPnP-Erweiterungen, Standard-DLNA funktioniert eingeschraenkt*

## Empfehlung

**Option H (DLNA MCP Control Point)** als Primaer-Loesung:

- Deckt 4 von 6 vorhandenen Renderern ab (HiFiBerry, 2x Samsung, Denon)
- Passt ins MCP-Paradigma (eigenstaendiger Server, keine Aenderung an internal_tools.py)
- Jellyfin DLNA-Server liefert die Medien — keine eigene Media-Server-Logik noetig
- Event-basiert, kein Polling, kein Background-Player im Renfield-Backend
- Mittlerer Aufwand dank `async-upnp-client` als Basis

**Spaeter optional:**
- **Option A** (HA Enqueue) fuer Sonos/Chromecast als Ergaenzung
- **Option B** (ffmpeg Concat) als universeller Fallback fuer Apple TV

## Plan: DLNA MCP Control Point

### Projekt-Setup

Neues Repository `renfield-mcp-dlna` — gleiche Struktur wie `renfield-mcp-jellyfin`:

```
renfield-mcp-dlna/
├── pyproject.toml
├── src/renfield_mcp_dlna/
│   ├── __init__.py
│   ├── __main__.py          # python -m renfield_mcp_dlna
│   ├── server.py            # FastMCP server + Tool-Definitionen
│   ├── discovery.py         # SSDP Discovery + Renderer-Cache
│   ├── queue_manager.py     # Queue State Machine pro Renderer
│   └── upnp_client.py       # async-upnp-client Wrapper
└── tests/
    ├── test_discovery.py
    ├── test_queue_manager.py
    └── test_tools.py
```

**Dependencies:**
- `mcp` (FastMCP)
- `async-upnp-client` (UPnP/DLNA Steuerung + Event Subscription)
- `httpx` (optional, fuer Jellyfin DLNA-Server Browsing)

### Transport

**stdio** — wie der Jellyfin MCP-Server. Wird von Renfield-Backend als Subprocess gestartet.

```yaml
# config/mcp_servers.yaml
- name: dlna
  transport: stdio
  command: python
  args: ["-m", "renfield_mcp_dlna"]
  enabled: "${DLNA_MCP_ENABLED:-false}"
  prompt_tools:
    - list_renderers
    - play_tracks
  examples:
    de: ["Spiel das Album im Arbeitszimmer"]
    en: ["Play the album in the study"]
```

### Kern-Komponenten

#### 1. Discovery (`discovery.py`)

- SSDP M-SEARCH fuer `urn:schemas-upnp-org:device:MediaRenderer:1`
- Cached Renderer-Liste (TTL: 5 Minuten)
- Pro Renderer: Friendly Name, UDN, `SetNextAVTransportURI`-Support (aus SCPD), Control URL
- `list_renderers` Tool gibt kompakte Liste zurueck:
  ```json
  [
    {"name": "HiFiBerry Garten", "udn": "uuid:...", "supports_queue": true},
    {"name": "Samsung 8 Series (65)", "udn": "uuid:...", "supports_queue": true},
    {"name": "Linn Wohnzimmer", "udn": "uuid:...", "supports_queue": false}
  ]
  ```

#### 2. Queue Manager (`queue_manager.py`)

- Ein `QueueSession` Objekt pro aktiver Wiedergabe (Renderer UDN → Session)
- Haelt die Track-Liste + aktuellen Index
- UPnP Event Subscription auf `AVTransport` (`LAST_CHANGE`)
- Bei `TransportState=STOPPED` oder `CurrentTrackURI` wechselt → naechsten Track via `SetNextAVTransportURI` nachladen
- `next()` / `previous()` manipulieren den Index und rufen `SetAVTransportURI` fuer sofortigen Wechsel

**Lebenszyklus:**
- `play_tracks()` erstellt eine `QueueSession`, startet Track 1, preloaded Track 2
- Event-Loop empfaengt `LAST_CHANGE`, laedt naechsten Track nach
- `stop()` beendet Session, kuendigt Event-Subscription
- Automatisches Cleanup wenn letzter Track fertig

#### 3. UPnP Client Wrapper (`upnp_client.py`)

- Duenner Wrapper um `async-upnp-client`
- `DmrDevice` Instanziierung aus Discovery-Daten
- Methoden: `play_uri()`, `set_next_uri()`, `stop()`, `pause()`, `get_transport_info()`
- DIDL-Lite Metadata-Generierung fuer Track-Info (Titel, Artist, Album, Art-URL)

#### 4. MCP Server (`server.py`)

FastMCP mit stdio Transport. Tools:

```python
@mcp.tool()
async def list_renderers() -> list[dict]:
    """Discover available DLNA media renderers on the network."""

@mcp.tool()
async def play_tracks(
    renderer_name: str,
    tracks: list[dict],   # [{url, title, artist, album, art_url}, ...]
) -> dict:
    """Play a list of tracks on a DLNA renderer with gapless queue."""

@mcp.tool()
async def stop(renderer_name: str) -> dict:
    """Stop playback on a DLNA renderer."""

@mcp.tool()
async def next_track(renderer_name: str) -> dict:
    """Skip to the next track in the queue."""

@mcp.tool()
async def previous_track(renderer_name: str) -> dict:
    """Go to the previous track in the queue."""

@mcp.tool()
async def get_status(renderer_name: str) -> dict:
    """Get current playback status, track info, and queue position."""
```

### Agent-Prompt Integration

Update `src/backend/prompts/agent.yaml`:

```
Album abspielen:
(1) search_media(type="MusicAlbum") → Album finden
(2) get_album_tracks(album_id) → alle Tracks mit api_stream URLs
(3) list_renderers → verfuegbare DLNA-Renderer
(4) play_tracks(renderer_name="...", tracks=[{url, title, artist, album}, ...])
```

Der Agent waehlt den Renderer anhand des Raum-Namens aus der `list_renderers`-Antwort.

### Event-Subscription Herausforderung (stdio-Transport)

**Problem:** UPnP Event-Subscriptions erfordern einen HTTP-Callback-Endpunkt. Der Renderer sendet `NOTIFY`-Requests an eine URL die der Control Point bereitstellt. Bei einem stdio-MCP-Server gibt es keinen langlebigen HTTP-Server.

**Loesung:** `async-upnp-client` enthaelt einen eingebauten Event-Handler (`AiohttpNotifyServer`) der einen temporaeren HTTP-Server auf einem freien Port startet. Der MCP-Server haelt diesen im Hintergrund am Leben solange eine Queue-Session aktiv ist.

**Alternative (einfacher, robuster):** Polling-freier Ansatz mit `GetTransportInfo()` nur bei Bedarf (bei `next`/`stop` Aufrufen). Fuer den gapless Preload: `SetNextAVTransportURI` direkt nach `SetAVTransportURI` senden (optimistisch). Wenn der Renderer den naechsten Track automatisch startet, ist das ausreichend. Kein Event-Server noetig.

### Renderer-Name ↔ Raum Mapping

Der Agent muss wissen welcher Renderer in welchem Raum steht. Optionen:

1. **Friendly Name enthält Raum:** "HiFiBerry Garten", "Samsung Wohnzimmer" — Agent matched per Name
2. **Config-Mapping:** Env-Var oder Config-Datei `DLNA_ROOM_MAP={"Arbeitszimmer": "HiFiBerry Garten", ...}`
3. **HA Entity Mapping:** DLNA-Entity in HA hat eine Raum-Zuordnung — MCP-Server fragt HA

Option 1 ist am einfachsten und reicht fuer den Start.

### Testplan

1. **Unit-Tests:** Discovery Mock, Queue State Machine, DIDL-Lite Generation
2. **Integration-Test:** Echte Jellyfin-URLs auf HiFiBerry-DLNA-Renderer abspielen
3. **E2E-Test:** Agent-Loop "Spiel das Album X im Arbeitszimmer" → Jellyfin → DLNA MCP → HiFiBerry

## Quellen

- [Jellyfin Playlists API](https://github.com/productinfo/jellyfin_api/blob/main/doc/PlaylistsApi.md)
- [Jellyfin UniversalAudioController Source](https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/UniversalAudioController.cs)
- [Jellyfin Forum: Playlist als HLS streamen](https://forum.jellyfin.org/t-i-want-to-livestream-a-playlist-created-inside-jellyfin-to-a-single-hls-viewer) — "Not with plain Jellyfin"
- [Jellyfin DLNA Queue Bug #888](https://github.com/jellyfin/jellyfin/issues/888) — seit 2019 ungeloest
- [HA dlna_dmr Source](https://github.com/home-assistant/core/blob/dev/homeassistant/components/dlna_dmr/media_player.py) — kein MEDIA_ENQUEUE
- [async-upnp-client](https://github.com/StevenLooman/async_upnp_client) — hat `async_set_next_transport_uri`
- [HA Architecture Issue #765](https://github.com/home-assistant/architecture/issues/765) — Enqueue API Definition
- [pyatv Dokumentation](https://pyatv.dev/) — Stream-Interface, Supported Features
- [pyatv Stream API](https://pyatv.dev/development/stream/) — "One stream can be played at the time"
- [LMS-uPnP Gapless Playback](https://deepwiki.com/philippe44/LMS-uPnP/9.2-gapless-playback) — SetNextAVTransportURI Probleme
- [homey-apple Repository](https://github.com/basmilius/homey-apple) — AirPlay + Companion Link + RAOP Packages
- [node-appletv-x](https://github.com/stickpin/node-appletv-x) — MRP Queue ist read-only
- [MRP Protocol Spec](https://github.com/jeanregisser/mediaremotetv-protocol) — Unofficial Protocol Documentation
- [HA Community: Queue multiple play_media calls](https://community.home-assistant.io/t/possible-to-queue-multiple-calls-to-media-player-play-media-for-sequential-playback/205931)
- [mcp-applemusic](https://github.com/samwang0723/mcp-applemusic) — AppleScript-basierter MCP-Server, macOS-only, kein Queue/AirPlay
- [HA MPD Integration Source](https://github.com/home-assistant/core/blob/dev/homeassistant/components/mpd/media_player.py) — `play_media` ruft `clear()` auf, kein MEDIA_ENQUEUE
- [HA DLNA DMR Integration Source](https://github.com/home-assistant/core/blob/dev/homeassistant/components/dlna_dmr/media_player.py) — nur `SetAVTransportURI`, kein `SetNextAVTransportURI`
- [HiFiBerry OS](https://github.com/hifiberry/hifiberry-os) — Minimale Audio-Linux-Distribution fuer Raspberry Pi
- [audiocontrol2](https://github.com/hifiberry/audiocontrol2) — REST + Socket.IO API fuer HiFiBerryOS
- [pyhifiberry](https://github.com/schnabel/pyhifiberry) — Python-Library fuer audiocontrol2
- [HA HiFiBerry HACS Component](https://github.com/willholdoway/hifiberry) — Custom Component, Beta
- [HA MPD Integration Docs](https://www.home-assistant.io/integrations/mpd/) — UI-konfigurierbar, kein Queue-Support
