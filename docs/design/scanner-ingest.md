# Scanner ingest — one scanner, three instances, content-routed

**Status:** DESIGN (investigated + decided 2026-09-08). Not implemented.
**Flag:** `SCANNER_INGEST_ENABLED` (per instance, dark by default)
**Related:** `docs/FOLDER_INGEST.md`, `docs/EMAIL_INGEST.md`, `docs/design/pdf-split.md`

## Problem

Two problems arrived together.

1. **The scanner stopped working.** A Fujitsu ScanSnap S1500 on macOS 26 (Tahoe)
   is no longer usable through its vendor software.
2. **One scanner feeds N independent Renfield instances.** A deployment may run
   several separate instances — separate namespaces, separate databases,
   separate deployments — each receiving its own paper. Today the routing
   decision is a manual act: scan into a folder, then move the PDF into the
   right instance's injection folder by hand.

Automating (2) means automating a decision that currently crosses independent
trust-and-legal boundaries by human judgement. That is the whole design problem;
the scanning itself is trivial by comparison.

**N is configuration, not code.** `N = 1` is the degenerate case and must stay
trivial: with a single target the entire routing layer collapses to a
passthrough — no classifier, no separator sheets, no review queue. Everything
below is what switches on when a second target is configured.

## Root cause of the outage (investigated 2026-09-08)

The scanner and SANE were both fine. The vendor software was the fault.

| Check | Result |
|---|---|
| USB enumeration | OK — `Fujitsu ScanSnap S1500`, VID `0x04c5` / PID `0x11a2` |
| SANE backend built | OK — `libsane-fujitsu.so` present |
| Device in backend config | OK — `usb 0x04c5 0x11a2` already in `fujitsu.conf` |
| `scanimage -L` | FAILED — "No scanners were identified" |
| `SANE_DEBUG_SANEI_USB=5` | `USBDeviceOpen: another process has device opened for exclusive access` |

**ScanSnap Manager 7.2.50** (x86_64-only, running under Rosetta, a login item)
holds the USB device open **exclusively**. Ricoh EOL'd the S1500: on Tahoe the
app's device-watcher half still starts and claims the device, while its scanning
half no longer works. It bricks both itself and every alternative.

Quitting it is the entire fix:

```
device `fujitsu:ScanSnap S1500:10551' is a FUJITSU ScanSnap S1500 scanner
$ scanimage --source 'ADF Duplex' --mode Color --resolution 300 ...
scanimage: sane_start: Document feeder out of documents
```

That error is the good one — the full open → configure → start path reached the
hardware.

### Three findings that prevent wasted work

- **The S1500 uses the `fujitsu` backend, not `epjitsu`.** It speaks SCSI over
  USB. There is **no `.nal` firmware to extract** — that applies only to the
  S300/S1100/S1300 family. Nothing needs to be installed beyond
  `brew install sane-backends`.
- **SANE's default scan area is US Letter (279.364 mm) — 17.7 mm shorter than
  A4.** Left at the default, the bottom of every A4 page is silently cut off,
  taking the footer with it: bank details, Steuernummer, totals, signature
  lines — precisely the Schicht-A fact material. Confirmed on the first real
  scan (both pages exactly 3299 px at 300 dpi = 279.3 mm; identical heights are
  the tell — that is the scan boundary, not the paper edge, whereas the widths
  differed because `--swcrop` was trimming). Fix: `--page-height 297`, which
  also raises the `-y` ceiling so `-y` needs no separate flag. `--ald` (scanner
  detects the lower edge) is the option for mixed paper sizes.
- **`--function` reads a constant `[1]`.** The S1500 has no multi-position
  profile dial (that is the fi-series with an LCD), so "route by scanner profile
  button" is **not available**. `--scan` and `--email` are the only two button
  sensors, which is not a usable three-way selector.

### Capabilities available via SANE (superset of the vendor app)

`ADF Front|ADF Back|ADF Duplex`, 50–600 dpi, `Lineart|Halftone|Gray|Color`,
`--swdeskew`, `--swcrop`, `--swdespeck`, `--swskip` (blank-page drop),
double-feed detection, and a live **Sensors** group: `--scan` (button),
`--page-loaded`, `--cover-open`, `--double-feed`, `--error-code`.

### Output quality — three defects the first attempt shipped

Measured against the vendor software on a real scan, the first `bin/scan.sh`
was visibly worse. Three causes, all self-inflicted, all relevant to the MCP
server since it performs the same PDF assembly:

- **`ocrmypdf` recompresses scans to lossy JPEG by default.** `--optimize 1`
  (the default) transcoded a 2421x3299 RGB page to `enc=jpeg` at 3.2 % of raw.
  **Use `--optimize 0`** — a document scan is an archival master, not a web
  image. This does not show up on small or mostly-white test pages, because the
  optimizer only transcodes when it judges the saving worthwhile; it must be
  verified on a *real* scan with `pdfimages -list`.
- **Deskewing twice.** SANE `--swdeskew` (scanner-side, on raw sensor data) plus
  `ocrmypdf --deskew` is a second resampling pass: softer text, no benefit. Do
  it once, on the scanner.
- **`--rotate-pages` is unsafe here.** Its orientation detection needs a page's
  worth of text and fails quietly otherwise, so it can rotate a page wrongly.

Two further defects, found only by looking at a real scan:

- **`--swdeskew` creates skew rather than removing it.** On a real duplex sheet
  the front came out straight and the **back was rotated ~34 degrees by the
  deskew itself**. It estimates the angle from content, and a sparse back side
  with fold lines and a barcode strip fools it. Diagnostic rule: a crooked ADF
  feed skews **both** sides of a sheet, so a **one-sided** tilt is always
  software, never paper. Default it OFF.
- **Raw S1500 colour is strongly blue.** Measured paper peak on white paper:
  **R226 G236 B253** (B-R = +26 across the page). Scaling the *white point* only
  gets to +16.5 — the cast is in the midtones. Anchoring on the **paper
  histogram peak** (paper is neutral by definition) and rescaling each channel
  to a common near-white gives **B-R = +2.6**, while leaving real colour intact.
  This is the correction ScanSnap Manager applied silently, and it is why the
  vendor output looked "way better" at first glance.

Two more, found only by iterating against real output:

- **Duplex show-through must be removed explicitly.** Duplex scanning lights the
  sheet from both sides, so the reverse side's ink is faintly visible. Measured
  on a real page it occupies the **200-224 band (7.08 % of pixels)**, cleanly
  separated from real ink (0-79). Mapping a level just below the paper peak to
  pure white erases it: at 0.90 of the peak, show-through falls **7.08 % ->
  1.93 %** while ink moves only 5.59 % -> 5.30 % (antialiasing crisping, not
  lost text); table rules, light logos and barcodes survive. Fold this into the
  SAME per-channel LUT as the white balance — one resampling pass, not two.
- **Re-saving pages in PIL silently destroys the DPI, and `img2pdf` sizes the
  PDF page from it.** Losing it turned an A4 page into a 1912 x 2631 pt (26
  inch) page at 96 dpi while the pixel data stayed correct — so it is invisible
  unless `pdfinfo` page size is checked. Always `save(..., dpi=(dpi, dpi))`, and
  assert the page comes out 612 x 841.92 pts for A4 at 300 dpi.

Two further contributors to "looks worse than the vendor app": `--swcrop=yes`
gives every page a *different* size (ragged PDF — prefer uniform A4), and
ScanSnap Manager silently applied brightness/contrast/sharpening that raw SANE
output does not. The scanner exposes `--brightness`, `--contrast` and
`--emphasis` for that; tune them against a real side-by-side, never by guessing.

## Requirements (user-fixed)

1. **Targets are 1..n and fully configurable.** Each is a separate instance with
   its own namespace, DB and deployment. Adding one is a config change, never a
   code change — the same idiom as the output-provider registry ("new brand =
   config, not code"). No target identity, count, or business meaning is
   hard-coded anywhere in this repository.
2. **All three routing layers are built**: declared intent, barcode separator
   sheets, and content classification with a human review floor.
3. The scanner and the router run on a single operator host on the LAN. Its
   address and the real target list live in `docs/private/` (gitignored), never
   here.
4. Derived, and load-bearing — see below: a scan enters exactly one instance,
   and only after its destination is settled.

## The invariant

> **A scan enters exactly one instance, and only after its destination is
> settled. An unrouted scan never transits any instance's database.**

Rationale: separate instances exist precisely because they are separate trust
*and legal* boundaries — private records, commercial records under their own
retention and bookkeeping obligations, and association records under their own
data-protection responsibility are not interchangeable. A misroute is not an
annoyance, it is a cross-boundary leak, and it is
not undoable: within seconds of ingest the document has become KB chunks,
embeddings, Schicht-A facts, KG entities and a Paperless copy. Deleting the row
afterwards retracts none of that.

**Consequence:** this rules out the design everyone reaches for first — *"one
hub instance receives everything, classifies, and forwards"*. The hub would hold
all three spheres' documents. Staging must live outside every instance.

## Architecture

### Component: `renfield-mcp-scanner`

Own repo, own package — the established shape for an MCP server that holds
credentials the backend must not (cf. `renfield-mcp-filesystem` holding SMB
creds, `renfield-mcp-email-ingest` holding IMAP creds).

**Packaged two ways from one codebase**: a macOS **LaunchAgent** (the decision
above) and a k8s node-pinned privileged pod with hostPath `/dev/bus/usb` (the
`k8s/satellite-esszimmer.yaml` pattern). Only the packaging differs, so moving
the scanner to a cluster node later is a deployment change, not a rewrite. This
costs nothing now and keeps the option open.

### Pipeline

```
ScanSnap (USB, SANE fujitsu backend)
   │  scanimage: ADF Duplex, 300dpi, swdeskew + swcrop + swskip
   ▼
PDF assembly (img2pdf) + OCR text layer (ocrmypdf, deu+eng)
   │
   ▼
ROUTING DECISION  ── L1 declared intent ──┐
                  ── L2 separator sheet ──┤ confident
                  ── L3 classification  ──┘    │        uncertain
                                               ▼            ▼
                        POST /api/folder-ingest/document   staging queue
                        → exactly ONE instance             (router disk only)
```

### Routing granularity: the batch, not the page

Route whole batches. This matches the existing manual workflow (scan a stack,
move the whole PDF), and the destination instance's **PDF-Split** already splits
multi-document batches at ingest as a document-worker pre-stage.

This is the decision that avoids needing to split *before* routing — which is
what would otherwise force the rejected hub design, since splitting requires
ingesting somewhere first. L2 separator sheets cover mixed stacks when needed.

### L1 — declared intent (primary, zero risk)

The operator names the destination in the request — *"scan this to <target>"* →
the agent calls `scan_document(target=<configured id>)`, validated against the
configured target list. The operator is standing at the scanner anyway.
No classification, no misroute possible. This should be the everyday path.

### L2 — barcode separator sheets (deterministic, unattended)

Printed cover sheets carrying a QR/Code128 payload — **one per configured
target, generated from the target list** so sheets and config cannot drift
apart. Decoded from the scanned pages with `zbar`, then matched against the
configured ids; an unrecognised payload routes to the review floor rather than
to a default.

Uniquely, a separator marks the document boundary **and** the destination in a
single mark, so this is the one layer that handles a **mixed stack** correctly
in one pass: split at each separator, route each resulting segment by its own
sheet. Cost: a `zbar` dependency and the discipline of keeping printed sheets by
the scanner.

### L3 — content classification (assist, gated)

OCR text of the first 1–2 pages → **one strict-JSON call** to the local model →
`{target, confidence, evidence}`. Same shape as `services/schicht_a_extractor.py`
and the PDF-split boundary detector: `get_default_client`, strict-JSON prompt in
`prompts/`, `_normalize_*` caps, safe empty result on failure.

Strongest real signals are the **addressee** and identifiers: company vs. home
addressee identity, registration/tax identifiers, and letterhead issuer. The
prompt is **built from the configured targets' own labels and descriptions** —
the classifier never carries a hard-coded notion of what the targets are.

### The review floor

Below `scanner_route_auto_threshold` the batch parks in the router's staging
queue with the classifier's evidence and a first-page preview. Surfaced as MCP
tools so **any** instance's agent can resolve it by voice — *"wohin gehört der
Scan von eben?"* — **without the content entering any instance's database**.

This is the shipped `pdf_split_proposals` review pattern (`/brain/review`,
durable resolution, reject is permanent), lifted one level up. Same reasoning:
an uncertain automated decision with irreversible consequences goes to a human,
it is never guessed.

### Two levels of routing, only one of which can be server-authoritative

An honest limitation that must not be papered over.

- **Level 1 — which instance.** The router picks the base URL + Bearer token.
  This is **inherently router-owned**: three separate deployments with three
  separate databases, and the receiving backend has no way to know whether it
  was the right recipient. Server authority is structurally impossible here.
  The mitigations are the layered routing (L1/L2 deterministic first), the
  review floor, and an append-only routing audit log on the router.
- **Level 2 — which sphere within that instance.** This **is** made
  server-authoritative, mirroring `services/email_ingest.py::resolve_mailbox_target`
  exactly: the router sends an opaque `scan_profile_id`; the backend resolves it
  to `(owner, tier, kb)` itself; an unknown id → `failed`, never a guess. So a
  compromised router token cannot escalate tier or file into an arbitrary sphere
  within an instance.

### Federation is not the carrier

Renfield's federation subsystem is a **query** transport (asker/responder
retrieval, Ed25519 pair anchors, TLS cert pinning). It moves questions, not
documents. Per-instance folder-ingest Bearer tokens are the correct credential
for ingest — that is what the route was built for.

### Registration in each instance (the two-step)

Per `CLAUDE.md`, registering the tools is **two** steps, not one:

1. A `scanner` stanza in `config/mcp_servers.yaml`
   (`transport: streamable_http`, `url: ${SCANNER_MCP_URL:-http://<scanner-host>:9093/mcp}`).
2. The tool names in the relevant role's `internal_tools`/`prompt_tools` in
   **`config/agent_roles.yaml`** — this is **ConfigMap-served**, not baked into
   the image, so each cluster's live ConfigMap must be patched too.

Skip step 2 and the agent reports "no tool available" even though the tools are
registered.

### Tools

| Tool | Purpose |
|---|---|
| `list_scanners()` | Devices + capabilities |
| `scanner_status()` | Real hardware state from the SANE sensors — ready / power-save / cover-open / page-loaded / error code. Not a guess. |
| `scan_document(target?, source, mode, resolution, deskew, skip_blank, title?)` | Scan, assemble, route, dispatch. `target` omitted → L2/L3 decide. |
| `list_pending_scans()` | The review floor — unrouted batches with evidence |
| `route_scan(scan_id, target)` | Resolve one, dispatch it, purge from staging |

## macOS host specifics

The host decision is a Mac Studio (`Mac16,11`) — a stationary desktop with
FileVault on, not a laptop. That materially de-risks the choice.

- **LaunchAgent, not LaunchDaemon.** USB access via libusb wants the user
  session; a root daemon with no session risks TCC/USB restrictions. Trade-off
  to accept: the agent runs only while the user is logged in.
- **ScanSnap Manager must stay disabled.** Remove it from Login Items
  (System Settings → General → Login Items). If it ever relaunches it
  re-claims the device exclusively and scanning dies again. Nothing needs to be
  uninstalled or deleted.
- **Sleep.** The staging queue is **durable on disk**, so a sleep or a restart
  never loses a scan; pending dispatches drain on wake with backoff. Hold
  `caffeinate` only while a scan job is actually in flight.
- **Reachability.** Every configured target's cluster must reach the router on
  `<scanner-host>:9093`. Needs a DHCP reservation (or static lease). Bind to the LAN interface only; the MCP
  endpoint itself is Bearer-gated; do not expose beyond the LAN.
- **Credentials.** Three folder-ingest tokens live in the **macOS Keychain**,
  not in a plist or an env file. Staging directory `0700`. FileVault (already
  on) covers at-rest.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `SCANNER_MCP_URL` | `http://<scanner-host>:9093/mcp` | Per-instance client URL |
| `SCANNER_TARGETS` | *(required, 1..n)* | The target registry — see below |
| `SCANNER_INGEST_ENABLED` | `false` | Per-instance flag (dark) |
| `scanner_route_auto_threshold` | `0.85` | Below → review floor |
| `scanner_default_target` | *(unset)* | Deliberately unset — no silent default |
| `scanner_staging_retention_days` | `30` | Purge unrouted scans (cf. `meeting_retention`) |
| `scanner_resolution` / `scanner_mode` | `300` / `Color` | Scan profile |

`scanner_default_target` being unset is deliberate: a scan that cannot be routed
must reach a human, never a default instance.

### The target registry (`SCANNER_TARGETS`)

The one structure the whole design turns on. Router-side only — it holds
credentials, so it never lives in this repository:

```yaml
targets:
  - id:                 # opaque, operator-chosen; used by scan_document(target=)
    label:              # human name, shown in review UI + voice confirmations
    description:        # one line; the ONLY thing the classifier is told about it
    base_url:           # that instance's folder-ingest endpoint
    token_ref:          # Keychain reference — never an inline secret
    scan_profile_id:    # opaque; THAT backend resolves it to owner/tier/kb
    separator_payload:  # barcode value on this target's printed cover sheet
```

Consequences worth stating, because they are what "1..n" has to mean in
practice:

- **`n = 1` short-circuits everything.** One target → dispatch directly; no
  classifier call, no separator decoding, no review queue, no confidence gate.
  The routing machinery must not impose cost on the single-target case.
- **The classifier is built from `description` fields at call time.** It has no
  hard-coded notion of what any target is, so adding a target changes a config
  file and nothing else.
- **Separator sheets are generated from this list**, so printed sheets and
  configuration cannot silently drift apart.
- **`id` is opaque.** Nothing downstream may infer meaning from it, which is what
  lets the real names stay out of this repository entirely.

## Phasing

- **Phase 0 — restore scanning (no architecture commitment).** Disable the
  ScanSnap Manager login item; a `bin/scan.sh` producing an OCR'd PDF into the
  current staging folder. The existing manual move workflow continues unchanged.
  Value today, independent of everything below.
- **Phase 1 — MCP server + L1.** `renfield-mcp-scanner`, LaunchAgent packaging,
  declared-intent routing, push to one instance, `scanner_status`.
  Register in `renfield` first.
- **Phase 2 — L2 separator sheets.** `zbar`, sheet templates, mixed-stack
  segment-and-route. Roll out to the second configured target.
- **Phase 3 — L3 classification + review floor.** Classifier, confidence gate,
  staging queue, `list_pending_scans` / `route_scan`. Roll out to any remaining
  configured targets.
- **Phase 4 (optional) — promote to k8s** if the scanner ever moves to a node.
  Packaging change only.

## Risks / accepted residuals

- **Level-1 instance choice cannot be server-authoritative** (see above). The
  layered routing + review floor + audit log are the mitigation, not a fix.
- **Classification misroute risk is non-zero.** This is exactly why L1 and L2
  are primary and L3 is an assist with a human floor beneath it.
- **Concentration on one host.** The Mac holds three instances' ingest tokens
  and stages documents from three spheres. Mitigated by Keychain storage, `0700`
  staging, FileVault, and retention purge — reduced, not eliminated.
- **Availability is tied to a logged-in user session.** Accepted per the host
  decision; the durable queue means scans are delayed, never lost.
- **SANE exposes no event API for the Scan button.** If a
  press-the-button-to-scan trigger is added, it must poll `--page-loaded` /
  `--scan` (~1s). This is a **documented exception** to the repo's
  "no polling — event-driven watcher" rule: the driver offers nothing to
  subscribe to.
- **Blank-page dropping via `--swskip` is not reliably tunable.** Measured on
  the first real duplex scan: a content page read 7.62 % dark pixels and its
  reverse 3.97 % — too narrow a margin to threshold without also dropping
  sparse real pages (a short letter, a receipt). Do not crank `--swskip`.
  Prefer `ADF Front` for single-sided stacks, or Gray/Lineart for text
  documents where the metric is better behaved. A surviving near-blank page
  costs one page and harms nothing downstream.
- **`ocrmypdf` at ingest is partly redundant** with the backend's own
  Docling/OCR + VLM coverage fallback. Keeping a text layer still helps dedup
  and classification; revisit if it costs wall-clock.
