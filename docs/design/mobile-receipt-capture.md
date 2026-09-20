# Mobile receipt capture — native iOS app, routed to the right instance

**Status:** DESIGN, revision 4 (2026-09-14; R3 adds projects + travel expense reports; R4 records all user decisions, incl. per diems + mileage, owner settlement, ZIP export). All product decisions are made; what remains is the tax-advisor checklist and the Phase 0 checks. **R5 (same day):** travel expense reports become a **plugin** (`expense_reports`) that is not loaded, mounted, migrated or shown on instances that don't activate it. **R6 (same day):** desk/webcam capture on the Mac and iPad. **R7 (same day, after the /autoplan gate):** scan-first capture with an on-device type suggestion, the "Trotzdem aufnehmen" quality escape, a per-user mobile-capture KB, and two factual corrections (split-child source; Paperless custom fields are new work). Nothing implemented. Phase 0
(measurement spike) gates everything below.
**Flag:** `MOBILE_CAPTURE_ENABLED` (per instance, dark by default)
**Related:** `docs/design/scanner-ingest.md` (the precedent this design reuses),
`docs/design/ingest-credentials.md`, `docs/FOLDER_INGEST.md`,
`docs/EMAIL_INGEST.md`, `docs/design/pdf-split.md`,
`docs/design/user-events-ws.md`, `docs/design/sso-token-handoff-hardening.md`

## Revision log

Revision 1 recommended an iOS Shortcut + the system document scanner. The user
decided on a **native iOS app**, distributed via TestFlight, with the business
instance first. Sections marked **[R2]** were rewritten for revision 2; sections
marked **[R2 edited]** keep their revision-1 structure with targeted changes;
unmarked sections are unchanged. Revision 3 (same day) adds project assignment and
travel expense reports; its sections are marked **[R3]**. Revision 4 (same day)
records the user's decisions on every remaining open question and adds the
per-diem / mileage calculation, the owner-settled status model and the ZIP
export; its sections are marked **[R4]**.

| Section | Change |
|---|---|
| Problem, Requirements | [R2 edited] native app, rollout order, push |
| The invariant | unchanged |
| 1. Capture client | [R2] decision recorded, native app architecture |
| 2. Reachability | [R2 edited] background `URLSession`, home detection, TLS pinning |
| 3. Where routing runs | [R2 edited] v1 manual target, app posts direct |
| Distribution and source code | [R2] new |
| App architecture (built to grow) | [R2] new |
| Architecture / Backend route | [R2 edited] PDF-only, details, device registration |
| Auth, pairing and revocation | [R2] Keychain, self-service pairing on an auth-on instance |
| Business-meal details | [R2] new (replaces the free-text note) |
| Location | [R2] new — decision final (optional, per receipt, place name only) |
| Receipts downstream / GoBD | [R2] paper is kept |
| User feedback and push | [R2] APNs design |
| Rollout: business instance first | [R2] new |
| Security / threat model | [R2 edited] |
| Configuration | [R2 edited] |
| Phases | [R2] |
| Risks | [R2 edited] |
| Open questions | [R2] most revision-1 questions decided; remaining ones listed |
| Projects and travel expense reports | [R3] new |
| Requirements, backend metadata, Configuration, Phases, Risks, Open questions | [R3 edited] project/trip additions |
| Open questions → **Decided (2026-09-14)** | [R4] every question answered; only the tax-advisor checklist and Phase 0 checks remain |
| Distribution (account holder, testers, minimum iOS), relay placement, auth-off pairing, history thumbnails | [R4 edited] decisions recorded |
| Credential scope + capture write path (`PATCH …/captures/{id}`) | [R4 edited] edit details/assignments after processing; own open trips from the app |
| Travel expense report: schema, status model, active trip | [R4 edited] owner settles, audit events, times required with per diems |
| Statutory structure → **Per diems and mileage allowance** | [R4] rate sets as configuration, calculation, traceability, go-live gate, tax-advisor checklist |
| Export / handover | [R4 edited] PDF + CSV + streamed ZIP of receipts |
| Configuration, Phases (Phase 1.5 effort), Risks | [R4 edited] |
| **Plugin architecture: `expense_reports`** | [R5] new — core/plugin split, versioned host contract + hooks, separate plugin migration environment, frontend plugin registry, app section gating |
| **Desk and webcam capture (Mac, iPad)** | [R6] new — device options, shared desk-scan pipeline, macOS distribution, pairing on the Mac, web capture assessment, privacy |
| Phases (Phase 0 desk measurements, Phase 1.6), Risks, Decided | [R6 edited] |
| Upload/PATCH metadata (`trip_ref` → opaque `extensions`), trips routes, "Instances that do not use this", Configuration, Phases, Risks, Decided | [R5 edited] — where older text says `EXPENSE_REPORTS_ENABLED`, core-owned expense tables or top-level `trip_ref`, the R5 section supersedes it |
| **On-device document type suggestion (scan first)** | [R7] new — capture order is scan first; an on-device model proposes the type and suggests fields; optional capability with manual fallback; privacy, eval, placement |
| Business-meal details (capture type), desk-scan quality gate, Pairing (KB), Backend route (PDF-Split children), How details reach Paperless, What exists today, hook table, Configuration, Phases, Risks, Decided | [R7 edited] after the /autoplan review: scan-first order, "Trotzdem aufnehmen" escape, **per-user mobile-capture KB** (fixes a cross-user dedup link and KB-owner visibility), split children keep `source=mobile_capture`, Paperless custom fields are **new** work |

## Problem [R2 edited]

A receipt arrives away from home — a business lunch, a fuel receipt, a parking
ticket. Today it goes into a wallet, fades (thermal paper), and is scanned weeks
later or never. The request:

> Capture receipts and documents with the iPhone while on the road; they must be
> transferred into the **correct** Renfield instance.

Three problems hide in that sentence, and only the first is about the camera:

1. **Capture quality.** A phone capture of a crumpled thermal receipt under bad
   light is a much worse input than a 300 dpi ADF scan.
2. **Reachability.** Every instance is LAN-only. "On the road" means the phone
   cannot reach any of them — and this design does not change that.
3. **Routing across trust and legal boundaries.** A deployment runs several
   independent instances — a household, a business instance (xidra) with
   bookkeeping obligations, an association instance with its own
   data-protection responsibility. The wrong one is a cross-boundary leak. This
   is the problem `docs/design/scanner-ingest.md` solved for the scanner; this
   design reuses its answer.

**N is configuration, not code.** Adding an instance to the app is pairing one
more account, never a code change.

### Capturing a receipt without the app — the two paths that exist today

- **Photo → watched folder** (`docs/FOLDER_INGEST.md`): save the photo into a
  share the `renfield-mcp-filesystem` server watches (local/SMB/NFS); `png`/`jpg`/`jpeg`
  are in the default `ALLOWED_EXTENSIONS`, and the file is ingested into the KB and
  filed to Paperless. Routing is per share, so the folder decides the instance.
- **Photo → mail to the watched mailbox** (`docs/EMAIL_INGEST.md`): send the picture
  as an *attachment* (inline images are skipped by design) to an IMAP mailbox the
  email-ingest server watches; routing is per mailbox.

Neither path knows the device, the project or the on-road context — that gap is
what this design is for.

## Requirements [R2 edited]

1. A **native iOS app**, receipts first, **built to grow** into further sections
   (deadlines, documents, chat) without a rewrite.
2. v1: capture (VisionKit), manual target choice, offline outbox, per-receipt
   status history, multiple instances/accounts, structured business-meal
   details, push feedback.
3. The document lands in **exactly one** instance, the one the user chose.
4. Instances stay LAN-only. No public endpoint, ever.
5. One ingest credential per **(person, device, instance)**, in the Keychain,
   device-bound, not backed up; revocable per lost phone.
6. Reuse the shared ingest seam — dedup, owner/tier, PDF-Split, Paperless,
   Schicht-A, user events all apply unchanged.
7. Rollout: **xidra first**, then household, then the association instance.
8. Feedback via the in-app history plus content-free APNs push, event-driven.
9. **[R3]** Optionally assign a receipt to a **project** and to a **travel expense
   report**, primarily on xidra; an "active trip" assigns automatically; instances
   without projects or reports must not break.

## The invariant (inherited, unchanged)

> **A capture enters exactly one instance, and only after its destination is
> settled. An unrouted capture never transits any instance's database.**

Within seconds of ingest a document has become chunks, embeddings, Schicht-A
facts, KG entities and a Paperless copy; deleting the row retracts none of that.
Consequences:

- **No "hub instance"** receives every capture and forwards it.
- **Unrouted captures wait outside every instance** — in the app's outbox, or (a
  later phase) in the capture router's staging directory, never in an instance's
  Postgres.
- **The app never uploads a capture for instance B through instance A.** Each
  account talks only to its own instance.

A second, capture-specific invariant:

> **The app deletes a capture's bytes only after its instance answered
> `ingested` or `duplicate` for it.** `retry`, a transport error, a timeout, or a
> cancelled background task keeps it in the outbox.

## Options

### 1. Capture client [R2]

**Decision (user, 2026-09-14): native iOS app (Swift/SwiftUI).** The Shortcut and
the PWA are rejected.

Revision 1's comparison, kept for the record of *why*:

| Option | Why it lost |
|---|---|
| PWA | WebKit has no Background Sync and no Web Share Target; an outbox only drains while the page is open; one origin = one instance |
| Shortcut + system scanner | token outside the Keychain; no real background upload; no push; brittle UX for an everyday tool |
| **Native app** | costs a second codebase + distribution overhead, but gives VisionKit in-app, Keychain, background `URLSession`, APNs, and a shell that can grow — **chosen** |

Capture in the app:

- **VisionKit document camera** (`VNDocumentCameraViewController`): edge
  detection, perspective correction, multi-page.
- **Output: PDF only.** The app renders the scanned page images into a PDF
  itself, with JPEG-encoded page images at a measured quality and resolution.
  HEIC never leaves the app (`heic` is not in `ALLOWED_EXTENSIONS`), and the app
  controls the page geometry instead of inheriting defaults.
- **No EXIF.** Pixel buffers rendered into a PDF carry no GPS metadata; the
  backend still strips EXIF from any image it receives (defence in depth).

**Measure, do not assume.** The scanner doc recorded six quality traps that were
invisible until real output was checked. The app repeats the "assemble a PDF
from page images" step, so the same discipline applies (Phase 0):

- **Filter vs thermal print.** Compare color / grayscale / high-contrast renders
  on real faded receipts through the real OCR pipeline (Docling + the VLM
  coverage fallback), not by eye.
- **Compression and effective resolution** — `pdfimages -list` on the produced
  PDF.
- **Page geometry.** The PDF page size must come from pixel dimensions and an
  explicit DPI (the scanner's PIL-DPI trap); long till rolls must not be squeezed
  onto a portrait A4 page. Check with `pdfinfo`.
- **Double processing.** VisionKit already corrects perspective; the app must not
  add a second resampling pass.

### 2. Reachability on the road [R2 edited]

| Option | New exposure | Latency | Verdict |
|---|---|---|---|
| **Upload only on home Wi-Fi** (background `URLSession`) | none | until home | **MVP** |
| **Self-hosted WireGuard, on-demand** | one UDP port that answers nothing unauthenticated | immediate | **Phase 3** |
| Hosted mesh VPN (third-party coordination) | third party sees the device graph | immediate | not recommended |
| **Public HTTPS endpoint** | an internet-reachable PDF parser | immediate | **never** (user decision) |

Why no public endpoint: what sits behind it is a document parser inside the
deployment that holds the knowledge base. The project already pulled n8n back
off the internet.

#### Background uploads

- Uploads run as **background `URLSession` upload tasks from a file** (the
  multipart body is pre-built into the outbox). The system daemon performs the
  transfer, so it survives the app being suspended or **terminated by the
  system**, and relaunches the app in the background to report completion.
- **Honest limit:** if the **user force-quits** the app from the app switcher,
  iOS cancels its pending background transfers. The outbox still holds every
  capture (invariant 2); the tasks are re-enqueued on the next launch. Phase 0
  confirms this on the target iOS version.
- Session configuration: `allowsCellularAccess = false`,
  `allowsExpensiveNetworkAccess = false`, `allowsConstrainedNetworkAccess =
  false`, `sessionSendsLaunchEvents = true`, `isDiscretionary = false`.

#### "Home Wi-Fi only" without asking for the SSID

iOS does not let an app condition a transfer on a specific SSID. Reading the SSID
needs location permission or special entitlements, and location is optional
here (see Location). The design does not use the SSID at all:

- **"Home" = the instance proves its identity.** Each account holds the
  instance's base URL and a **TLS public-key pin** received at pairing. A
  network where that name does not resolve, or answers with a different key, is
  "not home".
- **The pin check happens before any request header is sent**, so a hostile
  Wi-Fi that spoofs the name never sees the token or a receipt.
- **Event-driven triggers, no polling loop:**
  - app launch or foreground → path check (`NWPathMonitor`) → one pinned health
    probe per account → enqueue the outbox;
  - a network-path change while the app runs → the same;
  - a background task that failed on a non-home network → the completion event
    wakes the app → re-enqueue with an `earliestBeginDate` backoff.
  - The backoff is bounded (Phase 0 measures what iOS does with a task that
    waits for Wi-Fi and then meets a foreign network), and it is the one place
    where a timed retry exists. It disappears in Phase 3, when the tunnel makes
    every network "home".
- **Arriving home with the app closed** is not an event a third-party app can
  receive without "Always" location (region monitoring), which the location
  decision rules out. So pending uploads start at the next app open at home, or
  when an already-queued background task gets a Wi-Fi path. This is accepted
  for the MVP; the tunnel removes it.

#### TLS trust for LAN certificates

The instances use LAN certificates that iOS does not trust by default. The two
options are pinning inside the session delegate, or installing the private CA as
a trusted profile on the phone. **Phase 0 must verify that background-session
authentication challenges reach the delegate when the app is relaunched.** If
they do not, the private-CA profile becomes mandatory and the pin check moves to
the foreground health probe.

#### Tunnel phase

The phone addresses each instance by a name the tunnel's DNS resolves — the same
name on the LAN and through the tunnel. `.local` names are mDNS and do not cross
a tunnel. Tunnel details live in private runbooks.

### 3. Where routing runs [R2 edited]

- **L1 — declared intent (v1).** The user picks the account (= instance) before
  or after scanning; the app posts **directly** to that instance. No router in
  the path, so v1 has no dependency on the scanner host.
- **L2 — separator sheets.** Not applicable to a phone.
- **L3 — content classification + review floor (later phase).** "Automatisch"
  sends the capture to the **existing capture router** (the
  `renfield-mcp-scanner` host). It reuses the router's target registry,
  classifier, confidence gate, staging queue, `list_pending_scans` /
  `route_scan` review tools and audit log. The invariant holds because staging
  is the router's own directory, not an instance database. On-device
  classification is rejected: there is no local model, and a cloud model would
  send the receipt to a third party.

## Distribution and source code [R2]

### TestFlight with a paid Apple Developer account

Consequences to plan for:

- **90-day build expiry.** A TestFlight build stops launching 90 days after
  upload. An expired app cannot drain its outbox; the captures stay on disk but
  are unreachable until a new build is installed. Mitigations:
  - a scheduled rebuild at ≤ 75 days, even without code changes;
  - the backend records `client_build` and build date from every request header,
    and a Scheduled-Task check raises an `ops_alert` to the admin when any active
    device runs a build older than 75 days;
  - the app shows its own expiry banner from the embedded build date.
- **Testers.** Internal testers (App Store Connect users on the team, up to 100)
  get builds without review. External testers (invite by e-mail, e.g. family or
  xidra colleagues without App Store Connect accounts) need Beta App Review for
  the first build of each version. **Decided (R4):** internal
  testers while the group is small.
- **Account holder.** An organization account requires a D-U-N-S number;
  an individual account shows a personal name as the developer. **Decided (R4):
  an organization account held by xidra.**
  - The D-U-N-S number is a **prerequisite with its own lead time** (issuance plus
    Apple's organization verification, typically days to weeks, not under our
    control). Request it **before Phase 0 starts**. No TestFlight build (M2)
    can ship without the enrolled organization account.
- **Minimum iOS (R4):** current major version minus one — provisionally
  **iOS 18**, fixed after the Phase 0 checks.
- **APNs environment.** TestFlight builds use the production APNs environment
  (Phase 0 confirms). The relay must target production for TestFlight and
  sandbox for Xcode debug builds — the device registration carries the
  environment.

### Build and signing pipeline

- **Recommendation: a local `fastlane` pipeline on the stationary Mac** (the
  scanner host already runs there): `match`-less, with automatic signing
  against the team; the App Store Connect API key in the macOS Keychain; lanes
  `test`, `beta` (build, upload to TestFlight, bump build number) and
  `expiry_check`.
- Alternative: **Xcode Cloud** (Apple-hosted; included compute hours with the
  program). It needs no local toolchain maintenance, but the source and signing
  are on Apple's CI. It is acceptable as a fallback, not the default, in line
  with building on owned infrastructure.
- Tests: XCTest for the core package (API client, outbox state machine, Keychain
  store, PDF assembly with fixture images); UI tests for capture → outbox →
  history with a stubbed server.

### Where the source lives

**Recommendation: a separate private repository (working name `renfield-ios`)**,
the same shape as the MCP repositories. Reasons:

- a different toolchain (Xcode, Swift), release cadence (TestFlight) and CI;
- team ID, bundle ID, push topic and signing configuration do not belong in the
  public Renfield repository.

The **server contract stays in this repository**: the `mobile-capture` routes,
their JSON schemas and a `MOBILE_CAPTURE_CONTRACT_VERSION`. The app sends its
contract version as a header; skew is logged leniently, as for folder ingest. A
contract test fixture (request/response examples) is published from this repo
and consumed by the app's tests, so the two cannot drift silently.

The push relay (see below) is server code and gets its own small private
repository, likewise.

## App architecture — built to grow [R2]

v1 ships one section (Belege). The shell is designed so that further sections
are added, not retrofitted.

```
RenfieldApp (SwiftUI, one target)
 ├─ Shell
 │   ├─ AccountSwitcher      accounts = paired instances, each with its own label/colour
 │   └─ SectionRegistry      tabs per account; v1: [Belege]; later: Fristen, Dokumente, Chat
 ├─ Packages (Swift Package Manager, local)
 │   ├─ RenfieldCore
 │   │   ├─ Account          id, label, baseURL, tlsPin, instance contract version
 │   │   ├─ CredentialStore  Keychain; credentials typed by KIND per account
 │   │   ├─ APIClient        per-account URLSession, pin check, contract header, typed errors
 │   │   └─ Push             APNs registration, per-account device registration
 │   ├─ CaptureFeature       VisionKit, PDF assembly, business-meal details, location suggestion
 │   ├─ OutboxEngine         file outbox, background URLSession, state machine, re-enqueue
 │   └─ HistoryFeature       per-receipt status list, pull-to-refresh, push deep-link
```

Growth decisions made **now** so later sections do not force a rewrite:

- **Credentials are typed by kind.** v1 holds one kind, `ingest` (push-only plus
  reading its own captures' status). Deadlines, documents and chat need a
  **user session**, which is a different kind with a different lifecycle (a
  device session minted through a one-time-code + PKCE exchange, the pattern of
  `sso_handoff_store`). The ingest credential must **never** grow read scope to
  make a later section easier. Adding the `session` kind is additive: same
  account, second Keychain item.
- **Sections declare their required credential kind.** The shell shows a
  section only for accounts that hold that kind, so a household account with
  ingest-only pairing simply has no chat tab.
- **The API client is per account.** No global base URL, no shared cookie jar —
  which also keeps the invariant structural.
- **Contract negotiation per account.** The health response lists supported
  contract versions and features; sections that the instance does not offer
  stay hidden (the `/api/config/features` idea, per instance).

Later sections (named, not designed): **Phase 6 Fristen** (deadline agenda +
confirm), **Phase 7 Dokumente/Wissen** (search + detail), **Phase 8 Chat**. All
three start with the `session` credential kind.

## Desk and webcam capture — Mac and iPad [R6]

User requirement (R6): besides the iPhone app, scan documents lying on the desk
with the **camera of a Mac or an iPad**. Every earlier decision still applies:
- the native app and TestFlight under the xidra organization account;
- pairing + Keychain, one credential per (person, device, instance);
- xidra first, and the routing invariant;
- `source="mobile_capture"`;
- business-meal details;
- the project picker and the `expense_reports` plugin;
- never coordinates.

### What the hardware and platforms actually offer (researched)

| Fact | Status |
|---|---|
| **Desk View** (a top-down, perspective-corrected view of the desk) needs macOS 13+. It is available on a Mac through **Continuity Camera with iPhone 11 or later (not iPhone 16e/SE)**, or with the **built-in camera of MacBook Pro 2024+, MacBook Air 2025+, iMac 2024+**, or **Studio Display (2026) / Studio Display XDR**. **No iPad model is listed.** [Apple Support: Use Desk View](https://support.apple.com/en-us/121541) | confirmed (Apple Support) |
| Desk View on the iPhone uses the **ultra-wide** camera and de-warps a crop of it. The effective resolution of the document area is therefore well below a close still photo. | ultra-wide confirmed; **effective resolution: verify in Phase 0** |
| A Mac **without** a built-in camera (e.g. Mac Studio / Mac mini with a first-generation Studio Display) has **no built-in Desk View**. It needs an iPhone via Continuity Camera, a 2026 Studio Display, or an external UVC document camera. | confirmed via the device list |
| AVFoundation exposes Desk View as its own capture device type (`AVCaptureDevice.DeviceType.deskViewCamera`). Apple's documentation page did not render for automated fetching. | **verify in Phase 0**: type name, macOS version, whether it offers still-photo output or video frames only |
| **VisionKit's document camera (`VNDocumentCameraViewController`) is iOS/iPadOS only**, not macOS. Reports say it answers "Document camera is not available" even under Mac Catalyst on a Mac. [Apple Developer Forums](https://developer.apple.com/forums/thread/773015) | confirmed as unavailable on macOS; the Catalyst behaviour is reported, not tested by us |
| Vision `VNDetectDocumentSegmentationRequest` (document corners, perspective-aware, the building block of Apple's own scanner) is available since **macOS 12** and on iOS. [Apple Developer](https://developer.apple.com/documentation/vision/vndetectdocumentsegmentationrequest) | confirmed |
| **Center Stage** on iPad (front ultra-wide, keeps a *person* in frame) exists on recent iPads. It is a person-tracking video feature, not a document mode. [Apple Support: Center Stage](https://support.apple.com/en-us/111102) | confirmed; unsuitable for documents |
| **Browsers:** `ImageCapture.takePhoto()` is Chromium-only; **Safari captures a frame of the video stream**, so the image is limited to the negotiated video resolution and the video pipeline's processing. [Dynamsoft](https://www.dynamsoft.com/codepool/take-high-resolution-photo-in-the-browser.html), [Chrome Developers](https://developer.chrome.com/blog/imagecapture) | confirmed |
| Our **enforcing CSP** (`src/frontend/nginx.conf`) is `script-src 'self' 'wasm-unsafe-eval' blob: …`, `connect-src 'self'`, `worker-src 'self' blob:`. No CDN scripts, but **self-hosted** JS/WASM (e.g. a bundled document-detection WASM) is allowed. No `Permissions-Policy` header restricts the camera today. | confirmed in the repo |

### Options per device

| Option | Mac | iPad | Verdict |
|---|---|---|---|
| **(a) Native macOS app** — a macOS destination of the same multiplatform SwiftUI project: shared `RenfieldCore` (API client, Keychain, outbox, pairing), shared capture UI (business meal, project, trip), plus a new "Schreibtisch-Scan" mode | Desk View / UVC / Continuity camera, own detection pipeline (no VisionKit document camera on macOS) | — | **recommended for the Mac** |
| **(b) The iOS app, universal on iPad** | — | handheld: **VisionKit document camera on the rear camera**, same as the iPhone. **Stand mode:** iPad in an overhead stand with the rear camera looking down, using the same desk-scan pipeline as the Mac | **recommended for the iPad** |
| iPad front camera / Center Stage, document placed in front of the iPad | — | wrong angle (a steep oblique view of the desk), person-tracking crop, mirrored, lower detail on paper | **rejected** |
| **(c) Web capture in the PWA** — `getUserMedia` + in-browser document detection (self-hosted WASM, CSP-compatible) + upload through the user's web session | zero install | works in Safari too | **deferred — decided R6-1: not now** |

Ergonomics, stated realistically:
- **Mac with Desk View** is the natural "document lying in front of the screen"
  setup: no stand, and the view is already top-down.
- **iPad** is only good for desk scanning in an **overhead stand**. An iPad
  propped in a normal stand sees the desk at a grazing angle through the *front*
  camera, which no pipeline fixes into OCR-grade text. Handheld with the rear
  camera (VisionKit) remains the best iPad path for single documents.
- **External UVC document cameras** (visualizers on an arm) give a true top-down
  view on any Mac. They are supported by the same pipeline as a normal
  AVFoundation device.

### The desk-scan pipeline (shared Swift package `DeskScanKit`, macOS + iPad stand mode)

The iPhone/iPad handheld path keeps VisionKit. The desk pipeline exists because
VisionKit's document camera is not available on macOS and is not built for a
fixed overhead camera.

1. **Camera selection.**
   - Prefer the Desk View device when present, then external UVC cameras, then
     other cameras; remember the user's choice per device.
   - Choose the highest-resolution format.
   - **Use still-photo capture** where the device supports it; fall back to the
     best video frame only if Desk View exposes frames only (verify in Phase 0).
2. **Live edge detection** on downscaled preview frames: `VNDetectDocumentSegmentationRequest`
   (fallback `VNDetectRectanglesRequest`). The detected corners are drawn as an
   overlay.
3. **Auto-capture on stable edges (no trigger needed).** The capture fires when
   all of these hold:
   - the corners stay within a small pixel tolerance for a hold time;
   - the preview is sharp enough;
   - no motion is detected (a hand leaving the frame).

   Tolerance and hold time are **set from Phase 0 recordings**, not guessed. A
   manual shutter stays as a fallback. On capture, detection runs again on the
   **full-resolution** still — preview corners are only a hint.
4. **Perspective correction exactly once.**
   - Rectify the full-resolution still with a perspective transform
     (`CIPerspectiveCorrection`) from the refined corners.
   - **No second deskew or rotation pass** (the scanner-ingest trap: every
     resampling softens text).
   - **No destructive "document" filter** (the thermal-print trap from the
     iPhone path; colour/grayscale choice measured).
5. **Quality gate before a page is accepted** (thresholds measured in Phase 0):
   - **sharpness** — variance of the Laplacian on the rectified page;
   - **resolution** — pixels on the page's short edge. There is no physical
     scale reference, so this is a *pixel density proxy*, not a true DPI. It is
     calibrated against A4 letters and receipts in Phase 0;
   - **glare** — the share of clipped highlights inside the page area;
   - **coverage** — the page area as a share of the frame.

   A failing page is not accepted automatically. The user sees a concrete hint ("näher
   heran", "Blendung — Lampe verschieben", "unscharf") and the camera stays live.
   **[R7]** After **3 consecutive rejections** of the same page, a button
   **"Trotzdem aufnehmen"** appears. A page taken this way is kept and marked
   `quality: low` in the capture metadata. Processing and filing continue
   normally. The server sets a **sticky ledger reason** `low_capture_quality`,
   which shows as the "Du bist dran" bucket and outranks `processed` in status
   aggregation, including over PDF-Split children. It is resolved by an explicit
   **"Geprüft"** acknowledge action (app history or web). `needs_review` does not
   exist in the backend today, so this state and its resolution are new work.
   `quality` is the worst page of the capture. The flag is a client hint,
   rate-limited per client. The server-side OCR coverage signal stays the real
   quality gate.
   The same escape applies to the iPhone path when a capture is retaken for quality.
6. **Multi-page without touching the device.** After a capture the pipeline
   waits for a **page change**: the corners disappear or the rectified content
   changes beyond a threshold, then stabilises again → next auto-capture. A
   perceptual-hash guard skips a re-capture of the same page. "Fertig" ends the
   document; one document = one PDF (PDF-Split stays the safety net).
7. **PDF assembly** — identical rules to the iPhone path:
   - page images JPEG-encoded at a **measured** quality, never HEIC;
   - PDF page size computed from pixel dimensions and an **explicit DPI value**
     (the scanner doc's PIL-DPI trap);
   - no EXIF or other metadata;
   - checked with `pdfimages -list` / `pdfinfo` in Phase 0;
   - written into the **same outbox**, uploaded through the same
     `POST /api/mobile-capture/document`.
8. **Tests:** recorded Phase 0 frame sequences (synthetic documents, no private
   papers) as fixtures for detection, stability, page-change and quality-gate
   logic, plus PDF geometry assertions.

### Distribution of the macOS app

| Option | Verdict |
|---|---|
| **TestFlight for macOS**, same organization account, internal testers | **decided (R6-2)**: one account, one fastlane pipeline (a macOS lane beside the iOS lane), the same build-age alert covers the 90-day expiry, the same tester group |
| Developer ID signed + notarized direct distribution (DMG) | fallback: no 90-day expiry, but needs its own update mechanism and signing lane, and updates are no longer pushed through TestFlight |

Verify in Phase 0: TestFlight for Mac with our minimum macOS version and the
internal-tester flow on the organization account.

### Auth and pairing on Mac and iPad

- **Mac and iPad are their own devices:** one ingest credential per (person,
  device, instance), stored in the Keychain as device-only and not synced to
  iCloud Keychain. Revocable in "Mobile Geräte" like the iPhone.
- **Pairing without pointing a camera at the screen.** The pairing page is
  usually open *on the same Mac*, so it offers, next to the QR:
  - **"In der Renfield-App öffnen"** — a custom-URL-scheme link carrying only
    the one-time pairing code, instance URL, label and TLS pin;
  - a **manual code field** as a fallback.

  Same single-use, short-TTL code as the QR; the long-lived token is still never
  shown. The iPad can use the QR with its camera like the iPhone.
- **Background uploads on macOS:** the same outbox and background `URLSession`
  design. Because the Mac usually sits on the home network, uploads normally go
  out immediately. The home-detection (TLS pin) rule is unchanged.
- **Push:** APNs registration works the same on macOS (a separate device-token
  registration per credential).

### Web capture (option c) — assessed; deferred by decision R6-1 (kept for the record)

| Aspect | Assessment |
|---|---|
| Image quality | Safari takes a video frame, not a still. Quality is capped at the negotiated stream resolution after video denoising/compression, and focus/exposure control is limited. Likely below the native pipeline for receipts; **measure in Phase 0 if R6-1 chooses it**. |
| Detection | would need a self-hosted document-detection library (JS/WASM). The CSP allows self-hosted WASM (`'wasm-unsafe-eval'`, `'self'`), but it adds bundle weight and maintenance. |
| Auth | **the logged-in web session** (cookie + CSRF on xidra) instead of an ingest credential. A session-authenticated sibling route (`POST /api/mobile-capture/web-document`) uses the same bridge, validation, ledger (`client_id = web:<user_id>`), owner = the session user, tier = the user's mobile-capture default. Differences: no pairing, no device token/push (the user-events WebSocket is the feedback channel instead), CSRF applies. |
| Offline | not needed — the Mac is online at home. A failed upload keeps the PDF in memory with a retry; no durable outbox. |
| Value | zero install for someone without the app (e.g. a colleague on a shared Mac) |

### Privacy

- **The camera runs only while the scan sheet is open.** The capture session
  stops when the sheet closes, the app goes to the background, the screen locks
  or the Mac sleeps. Tests assert the session is stopped in each case.
- **No video recording.** Preview frames are processed in memory and dropped;
  **no frame is ever written to disk**. Only the final PDF enters the outbox,
  plus the 30-day local thumbnail (R2-6).
- **Indicators:** the system camera indicator (green light on the Mac, the iOS
  indicator on iPad) plus an explicit in-app "Kamera aktiv" state. Camera
  permission is requested on the first desk scan, never at app start.
- **No coordinates:** unchanged — the optional place-name suggestion follows the
  location decision on every platform.

## On-device document type suggestion — scan first [R7]

**Decision (user, 2026-09-14, at the /autoplan gate):**
- The capture order is **scan first**.
- After the scan, an **on-device model on the phone proposes the document type**
  (Beleg / Bewirtung / Rechnung / Sonstiges).
- Where sensible, it suggests fields (merchant, date, total).
- The user confirms or changes the suggestion with one tap.

This supersedes the "type chosen before scanning" order of R2.

### Platform facts (researched 2026-09-14; items marked *verify in Phase 0* are not confirmed by us)

| Fact | Status |
|---|---|
| Apple's **Foundation Models framework** gives apps access to the on-device ~3B-parameter language model. Available from **iOS/iPadOS/macOS 26** on Apple-Intelligence-capable devices with Apple Intelligence **enabled**: iPhone 15 Pro and later, M-series iPads, Apple Silicon Macs. Older devices, or devices with Apple Intelligence off, report "unavailable". [Apple Newsroom](https://www.apple.com/newsroom/2025/09/apples-foundation-models-framework-unlocks-new-intelligent-app-experiences/), [WWDC25](https://developer.apple.com/videos/play/wwdc2025/286/) | confirmed (vendor + secondary) |
| **Image input** (attachments on a prompt, receipt parsing on-device) arrived with the **2026 framework update (iOS 27 cycle)**. The same update made the session API span on-device, Private Cloud Compute and third-party models. [WWDC26](https://developer.apple.com/videos/play/wwdc2026/241/), [secondary](https://dev.to/hariharanjagan/whats-new-in-apples-foundation-models-framework-at-wwdc-2026-5227), [secondary](https://swiftwithmajid.com/2026/09/01/building-ai-features-using-foundation-models-multimodal-input/) | reported; **verify in Phase 0**: exact OS version, whether image input runs on the on-device model, API names |
| How to **pin a session to the on-device system model** so a request can never route to Private Cloud Compute or a third party | **verify in Phase 0** (hard requirement, see Privacy) |
| **German** prompt/output quality of the on-device model, and availability of Apple Intelligence / the framework for devices set to an **EU region** | **verify in Phase 0** (the first instance is operated in Germany) |
| Vision `RecognizeDocumentsRequest`: on-device document OCR with structure (paragraphs, tables, lists, detected data such as phone numbers). iOS 26 added table structure. [WWDC25 "Read documents using the Vision framework"](https://developer.apple.com/videos/play/wwdc2025/272/) | confirmed (vendor); minimum OS for the full structure API **verify in Phase 0** |
| `VNRecognizeTextRequest` (OCR) is available on every OS this app supports (iOS 18+) | confirmed |

### Tiers — an optional capability, never a raised minimum

**The minimum OS stays iOS 18 (R2-4).** The suggestion is a capability the app detects at runtime per device, and each tier degrades to the next:

| Tier | Device / OS | Pipeline | What the user gets |
|---|---|---|---|
| **A** | Foundation Models with on-device **image** input available and enabled | page image(s) + on-device OCR text → on-device model, guided generation into a typed struct `{type, evidence[], merchant?, date?, total?, currency?}` | type pre-selection + field suggestions |
| **B** | Foundation Models available, **text-only** | on-device OCR (`RecognizeDocumentsRequest`, else `VNRecognizeTextRequest`) → on-device model on the text → same struct | same as A, from text only |
| **C** | no Foundation Models (iOS 18–25, older devices, Apple Intelligence off, region-restricted) | **manual type choice** (the R2 behaviour); no suggestion | chips, nothing pre-selected |

- **Tier C has no own Core ML classifier in v1.** A bundled classifier would need
  training data from real receipts, a model update path and an eval of its own.
  That is revisited only if the Phase 0 device inventory shows most users on
  Tier C.
- The app shows **why** there is no suggestion only in Settings ("Typvorschlag:
  auf diesem Gerät nicht verfügbar"). The capture sheet itself stays calm.

### Privacy — nothing leaves the device for classification

- The page image, the OCR text and the model output **never leave the device**
  for classification.
- **No Private Cloud Compute, no third-party model and no server-side vision
  model call from the app.**
- The session is created against the on-device system model only.
  - If the platform cannot guarantee on-device execution, the app treats the
    device as **Tier C**: fail closed.
  - The code allows only the on-device model type, and a build-time check fails
    on any cloud or third-party model type. A runtime re-check runs before each
    classification. The Phase 0 pinning check (airplane mode plus a router-level
    packet capture) is a go/no-go item.
  - An in-app network trap cannot prove this: framework requests leave through
    system processes, not the app's own network stack.
- **Session hygiene:**
  - one fresh session per capture, so no context (or injected receipt text)
    carries over;
  - requests are serialised, and a rapid second scan waits;
  - cancelled when the sheet is dismissed or the app goes to the background;
  - availability is re-checked on every capture (the model may still be
    downloading);
  - input is bounded to the context window: OCR text of the first + last page is
    truncated;
  - runs only after VisionKit is dismissed.
- **Suggestions are defaults, not data.**
  - The **server-side extraction (Schicht-A) stays the source of truth** for
    merchant, date and total.
  - **[R7-D-W4c] Suggested fields are sent as hints — never as facts.** See
    "Client hints" below. They may also pre-fill **user-editable** fields that
    already exist (e.g. the Bewirtung `place_name` from the merchant); once
    confirmed, those are ordinary user input.
  - The chosen `capture_type` travels as today.
  - Optionally the metadata includes `type_suggestion: {suggested, accepted: bool,
    tier}` for eval telemetry, with no document content. Enum values only; the
    server validates it and never interprets it as instructions.
    - **[review]** `accepted: bool` is replaced by
      `state ∈ {preselected, ordered, late, timeout, unavailable, disabled}` +
      `rulesVersion`, so the eval is not skewed.
    - It is stored with the capture ledger row and deleted with it.
    - `tier` reveals whether Apple Intelligence is on for the device; this is
      noted as a minor disclosure.
    - `rechnung`, `sonstiges` and `quality` are sent only when the instance's
      health lists them (contract bump); the request model forbids unknown
      fields.
- **Prompt-injection surface on the phone:** text printed on a receipt ("ignore
  previous instructions …") can steer the on-device model's output. Defences:
  - guided generation into a closed enum plus bounded fields;
  - a suggestion never triggers an action; it only pre-selects a chip;
  - the server re-validates everything.

### UX

- **Fast path at the till.**
  - The Erfassen tab opens the camera immediately. Setting "Kamera sofort
    öffnen" defaults to on.
  - After the scan, the **send sheet appears at once** with the four type chips
    and the target account ("an <Instanz>").
  - The suggestion runs asynchronously with a budget (Phase 0 measures latency;
    target ≤ 1.5 s on the slowest supported Tier A/B device). If it arrives
    later, the chip pre-selects only if the user has not tapped yet.
  - The user's tap always wins. "Senden" is never blocked on the model.
- **Confidence.**
  - A language model's self-reported confidence is not calibrated. Confidence
    is therefore derived from **agreement**: the model's type **and** at least
    one deterministic evidence rule match. Examples:
    - a Bewirtung keyword or tip line;
    - "Rechnung" + invoice number + payment term;
    - a payment-terminal slip;
    - VAT id + "Summe" on a till receipt.
  - **High** (agreement, and the type's precision in the Phase 0 eval ≥ the
    threshold): the chip is pre-selected, with a small "Vorschlag" label and the
    evidence as a tooltip/accessibility hint ("erkannt: Trinkgeld, Bewirtung").
  - **Low:** nothing pre-selected; the model's guess is ordered first among
    the chips, without a label.
  - **No result / timeout / unavailable:** plain manual choice.
- **Field suggestions:** shown greyed under the chips ("Händler · Datum · Betrag –
  Vorschlag"). They travel as `client_hints` (below), not as facts. Merchant →
  `place_name` is offered only for Bewirtung, as an editable pre-fill.

### Client hints — suggestions sent to the backend [R7-D-W4c]

**Decision (user):** the on-device suggestion is sent to the instance as a hint.
**Contract.** An optional metadata block, versioned and sent **only when the
instance's health advertises `client_hints: {version: 1}`**:
```
client_hints: {
  v: 1,
  source: "on_device_model",           // enum
  tier: "A" | "B",                     // C sends no block
  rules_version: "<semver>",
  type:     {value: <one of health.client_hints.types>, state: preselected|ordered|late|timeout|unavailable|disabled},
  // every field below is {value, confidence: high|low}; this block replaces the separate type_suggestion telemetry
  merchant: "<string>"?,               // ≤ 120 chars
  date:     "YYYY-MM-DD"?,             // calendar-valid, within [today − 10 years, today + 1 day]
  total:    "<decimal>"?,              // ^-?\d{1,9}([.,]\d{1,2})?$ , |value| ≤ 1 000 000
  currency: "<ISO 4217>"?              // 3 uppercase letters from the ISO list
}
```
**Server rules:**
- **Validation.**
  - **[review fix]** The outer upload metadata accepts `client_hints` as **raw
    JSON**, capped at 4 KB. The block is then validated separately with its own
    `extra=forbid` model: NFC, control characters stripped, no HTML/Markdown
    interpretation. A forbid error in the block can therefore never 422 the
    receipt.
  - If the block is invalid, **only the block is dropped**. The upload is
    accepted, the ledger records `client_hints_invalid`, and the log carries the
    **error codes only, never the values**. A hint is never a reason to reject a
    receipt.
  - The allowed `type` values come from the instance's advertisement.
  - Amounts: reuse the Schicht-A amount parser's ISO-4217 list. An ambiguous
    `1.234` / `1,234` (3 digits after the only separator) is rejected.
  - The regex allows `|value| ≤ 1 000 000` with ≤ 2 decimals, and nothing longer.
- **Old instance.** No advertisement means the app sends no block, and a stray block
  is rejected as an unknown field by the forbid rule. The app hides nothing
  user-visible.
- **Storage.** A separate table `capture_client_hints`:
  - `document_id` FK with CASCADE, `client_id`, `capture_id`, the validated fields,
    `received_at`;
  - never written to `document_facts` and never into chunks;
  - it **inherits the document's tier by join, not by copy.** There is no tier
    column; every read joins `documents` through the document-facts circle filter
    pattern, so a later tier change applies automatically.
  - A dedup hit on another owner's document stores **no** hints.
  - Visibility: same owner, same visibility, read only
    through the document's circle filter);
  - it is **deleted with the document**.
- **Use — a comparison signal only:**
  - **[review fix] Trigger.** Schicht-A runs fire-and-forget. It is skipped when a
    document has only table chunks or the flag is off, and it can return without
    facts or skip on its per-document lock. So the comparator:
    - is called **at the end of the Schicht-A hook, after its commit**, on every
      run including re-extraction;
    - is backed by a small scheduled sweep for captures whose document completed
      without a comparison.
    - Its states are `pending` (not yet compared), `n/a` (with a reason:
      `extraction_off`, `no_candidate`, `split`, `table_only`, `no_facts`), and
      `match | partial | mismatch`.
    - The worker updates the ledger and emits the content-free user event.
  - **[review fix] Inputs.**
    - **Date:** `documents.document_date`, the derived document date.
    - **Merchant:** the `issuer` fact.
    - **Total:** a defined selection rule over the extracted amount facts. Prefer
      the gross/"Summe/Gesamt/Total" amount. For Bewirtung, compare both
      with and without a tip line; a difference within the tip → `partial`.
    - No unique candidate → `n/a/no_candidate`.
    - A currency difference → `partial`.
  - **Split:** hints on a capture that PDF-Split turned into several children →
    `n/a/split`, and the review shows "mehrere Belege erkannt".
  - Normalisation: decimal comma, thousands separators, date formats, merchant
    token overlap.
  - The result is stored as `hint_agreement` per field.
  - **Resolution:**
    - A money mismatch uses the same sticky-reason mechanism as
      `low_capture_quality`. It is resolved by **"Geprüft"**.
    - It is **cleared automatically** when a recomputation after re-extraction
      yields `match`.
    - Mismatch flags are rate-limited per client.
  - A **mismatch** shows in the review surfaces and the app history, e.g. "App
    erkannte 23,40 €, Server 32,40 €". It raises the capture into the
    "Du bist dran" bucket only for the **total** (money). Date/merchant
    mismatches are informational.
  - **No automatic trust.** A hint never overwrites, confirms or creates a fact,
    never sets `amount_confirmed` in the expense plugin, and never changes the
    tier.
  - **Not an LLM input.** Hints are not placed into any extraction or agent
    prompt. If a later Schicht-A cross-check uses them, it does so only as typed,
    validated values in code **outside** the instruction context (the
    comparator), never as prompt text.
- **Rendering.** Merchant text is rendered escaped (React/SwiftUI boundaries) in
  review and history. It is **never placed into push, notification or TTS text**;
  push texts stay loc-keys only.
- **Prompt-injection surface.** Merchant text comes from OCR of a printed receipt
  and may carry instructions. Two defences:
  - length caps + character rules;
  - storage outside chunks/prompts. The agent read path does **not** surface
    hints.
- **Tests:**
  - injection strings in merchant (stored inert, never in chunks/prompt
    fixtures);
  - invalid dates (Feb 30, far future), amounts (letters, 3 decimals, overflow)
    and currencies;
  - a missing block;
  - a block sent to an instance without the advertisement → unknown field dropped;
  - tier inheritance (lower-reach user cannot read hints);
  - cascade delete;
  - the comparator's normalisation cases;
  - a mismatch that appears in review and in the app.
- **Multi-page:** classify on the first page plus the last page (totals). Pages in
  between are not sent to the model, which bounds latency.
- **Desk scan (Mac / iPad stand):**
  - The same tiers apply. Apple Silicon Macs are Apple-Intelligence-capable, so
    Tier A/B is likely. **Verify in Phase 0** that Foundation Models image input
    and on-device pinning also hold on macOS.
  - The suggestion runs after "Fertig", on the assembled document.
- **Learning from corrections:** the app does **not** learn on-device in v1.
  Accept/override counts (enum only) feed the eval.
- **Accessibility:** VoiceOver announces "Vorschlag: Bewirtung, doppeltippen zum
  Ändern". Colour is never the only signal.

### Eval (Phase 0 gate for showing pre-selections) — superseded in part by the eval-set process below

- **Eval set.**
  - Target: at least 30 captures per type (Beleg, Bewirtung, Rechnung,
    Sonstiges), incl. faded thermal receipts and multi-page invoices.
  - Receipts are private business documents. The set and the raw results stay
    on the operator's device / private storage. The public repository holds only
    the harness, synthetic examples and aggregate numbers.
- **Measured per tier (A, B) and per device class:**
  - precision and recall per type;
  - the agreement rule's precision;
  - latency p50/p95;
  - thermal/battery impact over 20 consecutive captures.
- **Threshold.** Pre-selection is shown for a type only if its **agreement
  precision ≥ 95%** in the eval. Otherwise that type is only "ordered first"
  (low confidence). Field suggestions are shown only where exact-match precision
  ≥ 90% for that field.
- **Re-run** on every major OS update: the model changes with the OS. An app
  debug screen runs the harness locally.
- A per-instance kill switch comes from the mobile health feature list
  (`type_suggestion`). An operator can turn pre-selection off without an app
  build.
  - When it is off, the model does not run at all.
  - The scan happens before the target account is confirmed, so the **most
    restrictive** setting among the paired accounts applies. It is re-evaluated
    when the account changes.
  - Unknown or stale health (offline at the till) → no pre-selection.
- **[review] Bewirtung safety and statistics:**
  - **Beleg is never pre-selected** when any Bewirtung signal is present (tip
    line, number of guests, restaurant receipt pattern). A receipt with VAT id +
    "Summe" is not Beleg evidence on its own.
  - The eval tracks the **Bewirtung → Beleg confusion rate** separately.
  - The threshold uses the **lower 95% confidence bound** of agreement precision,
    not the point estimate. With about 30 captures per type the bound is too low
    to pre-select anything (29/30 correct ≈ 83% lower bound); see the eval-size
    decision.
  - If the first and last page disagree on the type (a batch PDF-Split may later
    split), no suggestion is shown.
  - Suggested field strings are truncated on the client.
  - **Honest limit:** text printed on a receipt can satisfy both the model and the
    evidence rules. Agreement is a quality signal, not a defence against a
    crafted receipt; the impact is limited to which chip is pre-selected.

### Eval set — 100+ real receipts per type before M2 [R7-D-W4a]

**Decision (user):** collect **at least 100 real captures per type** (Beleg,
Bewirtung, Rechnung, Sonstiges) in Phase 0, before M2. Pre-selection is **active from
the first build** for every type that clears the precision bound on this set. Types
that miss the bound get "rank first, no pre-selection".

- **Who, where.**
  - The paper originals are kept (GoBD decision), so the set is built mainly from
    the **existing paper archive**: past years' receipts and invoices, including
    naturally faded thermal paper, topped up with new receipts.
  - Collection happens per instance by the person who owns the documents: the
    business instance for business receipts and meals, the household for private
    Beleg/Rechnung/Sonstiges.
  - Capture uses the Phase 0 prototype app on the operator's own devices, in the
    same capture conditions as production.
- **Private data handling.**
  - The eval set is private business and personal data. It is stored **only**:
    - on the operator's device (app container, excluded from backup);
    - or in one access-restricted encrypted folder on the operator's own storage.
  - Access is limited to the operator plus the named labeller(s) inside the
    owning organisation.
  - **Never** in git, any repository, a CI system, a ticket, a cloud AI service or
    a chat.
  - The public repository holds only the eval runner, the label schema, synthetic
    examples and **aggregate numbers**.
  - Retention: the images and OCR text are deleted within 30 days after the
    Phase 0 eval report is accepted. The label file keeps only per-item ids +
    labels + stratum, with no image and no text. A deletion record (date, count)
    goes into the private runbook.
  - A re-run on a later OS uses a newly collected set, or a retained set only if
    the data-protection owner approves a longer retention in writing.
- **Third parties.**
  - Business-meal receipts can show participants (handwritten on the back or in a
    note) and staff names.
  - The eval uses **only the printed front** of the receipt. Handwritten
    participant notes are covered before capture or not captured.
  - The label never records a person's name.
  - The legal basis and any need for an internal notice are checked with the
    instance's data-protection responsibility **before** collection starts. This
    is a Phase 0 go/no-go item, not assumed.
- **Labels.**
  - The owner labels each item in the prototype's labelling screen: type
    (4 values), plus merchant/date/total/currency as printed (for hint-field
    precision), plus stratum tags.
  - **A second person independently re-labels a random 20% sample.** Cohen's κ is
    reported, and disagreements are resolved by the owner with a note.
  - If κ < 0.8 on type, the label guide is revised and the set is re-labelled.
- **Stratification (tags, with a minimum per type where applicable):**
  - thermal/faded: ≥ 20;
  - handwritten or partly handwritten: ≥ 10;
  - foreign-language: ≥ 10;
  - poor light / crumpled: ≥ 20;
  - multi-page (Rechnung): ≥ 20.

  Results are reported per stratum. A stratum below its minimum is reported as
  "insufficient", never folded silently into the total.
- **Reproducible runner.**
  - `EvalRunner` in the app's debug build runs on device.
  - The same code runs in the simulator on exported page images **only for Tier B
    text logic**. The model tiers A/B need a real device.
  - Inputs are the private set + a pinned `rules_version`.
  - The output is a JSON report: per `(type, tier, device class, OS build,
    rules_version)` precision, recall, the Wilson 95% interval, the confusion
    matrix incl. **Bewirtung → Beleg**, hint-field exact-match precision,
    latency p50/p95, peak memory.
  - The report contains no images, no text and no names.
  - The harness itself is unit-tested on synthetic fixtures in CI.
- **Threshold rule.**
  - A type gets pre-selection iff **the Wilson lower bound of agreement precision
    ≥ 0.95** on its eval items for that tier, and for Beleg additionally the
    Bewirtung → Beleg rate ≤ 1%.
  - A hint field is sent with `confidence: high` only if its exact-match
    precision lower bound ≥ 0.90; otherwise it is still sent as a hint, with
    `confidence: low`.
  - The thresholds are recorded with the report and baked into the build as
    `SuggestionPolicy` (per type, per tier).
  - **[review fix] Honest maths (Wilson, z = 1.96).**
    - The denominator is the number of **predictions where the agreement rule
      fired** for that type, not the number of items.
    - A lower bound ≥ 0.95 needs:
      - **≥ 73 agreeing predictions with zero errors** (73/73 → 0.950);
      - 99/100 → 0.946 fails; 100/100 → 0.963 passes.
    - For hint fields, a lower bound ≥ 0.90 needs e.g. 96/100 (0.902).
    - The Beleg guard uses the **upper** Wilson bound of the Bewirtung → Beleg rate
      ≤ 0.05, not a point estimate.
    - Consequence: only types with near-perfect agreement pre-select. The others
      rank first, which is the user's stated fallback. The report states these
      numbers verbatim.
    - The runner has unit vectors for these cases.
  - **[review fix] No label leakage.**
    - A separate **dev set** of ~25 captures per type (collected the same way,
      same privacy rules) is used to tune the evidence rules.
    - `rules_version` is **frozen before** the ≥ 100-per-type **holdout** is scored.
    - The holdout is labelled by someone other than the rules author.
    - Total collection is therefore ~125 per type.
  - **Strata** are reported for bias visibility only. With 10–20 items their
    bounds are not used for gating.
  - Cells per tier × device × OS are not required to reach 100. Gating is per
    type and tier on the reference device class; other device classes are
    reported.
  - **Intent vs. print:** Bewirtung vs. Beleg is partly the user's intent, not
    visible on paper. The label guide asks "what would you file this as"; κ is
    reported per pair, and a low κ on this pair is expected and documented.
  - **[review fix] OS drift.**
    - `SuggestionPolicy` records the evaluated OS major version. On any other OS
      major version the app **falls back to "rank first"** at runtime until a new
      eval exists.
    - A new eval needs either a new collection or a regression subset retained
      with written approval of the data-protection owner.
    - Export of images to the simulator goes only to the encrypted folder, never
      elsewhere.
- **Calendar vs. effort.**
  - Collecting from the paper archive is mostly calendar time for the owner:
    ~1–2 working days per 100 items (capture + label), plus the second labeller's
    ~0.5 day.
  - If the archive lacks 100 Bewirtung receipts, topping up with new ones depends
    on how often business meals happen. That can take **weeks to months**.
  - **Contingency (documented, not a new decision):** a type that cannot reach
    100 items by the Phase 0 end date ships as "rank first, no pre-selection" and
    is re-evaluated when the set is complete.

### Effort and placement [R7-D-W4d]

- **Decision (user):** the suggestion is part of **M2** (no M2b).
- **M2 now depends on these Phase 0 gates:**
  - (1) the Foundation Models on-device guarantee;
  - (2) image input on the target OS;
  - (3) German output quality;
  - (4) the eval set + report.
- **Contingency** (documented fallback, not a new decision):
  - If gate 1, 2 or 3 fails, **or the data-protection go/no-go fails, or κ
    re-labelling does not converge by the Phase 0 end date**, M2 ships scan-first
    with **manual chips**, and the suggestion is carried as a follow-up without
    changing M2's other scope.
  - The M1 `client_hints` backend is built **dark** (not advertised in health)
    and switched on only after the Phase 0 result, so a failed gate leaves no
    active surface.
  - If only gate 2 fails, Tier B (text-only) still ships when gates 1 and 3 pass.
  - If gate 4 is incomplete for some types, those types start as "rank first".
- **Effort added to M2:** human ~2.5–3.5 wk / CC ~5–7 d.
  - Covers tier detection, OCR + guided generation, agreement rules,
    `SuggestionPolicy`, `client_hints`, sheet states, kill switch, eval runner and
    tests.
  - Backend `client_hints` + comparator + review display: human ~1–1.5 wk /
    CC ~2–3 d, in M1.
- **Phase 0 added:**
  - gate checks: human ~3–4 d / CC ~1 d;
  - labelling tool + runner: human ~1 wk / CC ~2 d;
  - collection: owner calendar time, see above.
- **Re-estimated M2 start:** Phase 0 end + 0.
  - Phase 0 takes **~3–5 calendar weeks** if the archive covers all four types,
    **longer** where new business-meal receipts must be collected. In that case
    the contingency above keeps M2 from waiting on one type.
  - The D-U-N-S / organisation enrolment lead time still gates the first
    TestFlight build independently.

### Risks

- **The API or on-device pinning may not behave as reported.** Mitigation: the
  Phase 0 go/no-go; fall back to Tier C everywhere, which is still the R2 manual
  flow.
- **Automation bias:** users accept a wrong pre-selection (e.g. a Bewirtung
  captured as Beleg, so no occasion is recorded). Mitigations:
  - pre-selection only at ≥ 95% agreement precision;
  - the Bewirtung evidence is shown;
  - the server review flow and Schicht-A stay unchanged;
  - accept/override telemetry.
- **Model drift** with OS updates changes hit rates silently. Mitigations: re-run
  the eval per major OS; kill switch.
- **Uneven experience across devices** (Tier C users get no suggestion).
  Accepted; it is a capability, not a requirement.
- **Region / language availability** (EU, German) is unverified. Mitigation:
  Phase 0; Tier C fallback.

## Architecture [R2 edited]

```
iPhone app (per account = per instance)
  VisionKit → PDF assembly → business-meal details (+ optional place-name suggestion)
        │
        ▼
  OUTBOX (app container, excluded from backup)  ── state: queued
        │  home network: pinned health probe OK      (Phase 3: tunnel, any network)
        ▼
  background URLSession upload task (from file)
        │  POST https://<instance>/api/mobile-capture/document
        │  Bearer rfi.<client_id>.<secret>   ·  X-Mobile-Capture-Contract  ·  X-Client-Build
        ▼
  backend  api/routes/mobile_capture.py
    credential → (owner, tier, kb) SERVER-SIDE · PDF magic-byte check · details validated
    services/folder_ingest.ingest_document(source="mobile_capture")
        │  4-state: ingested | duplicate | retry | failed   → outbox: accepted / keep
        ▼
  document worker: PDF-Split pre-stage → OCR/VLM → chunks → Schicht-A → KG
  paperless_reconciler (async) → details mirrored to Paperless custom fields
  xidra: review flow (opted in for mobile_capture)
        │  after-commit state transitions (processed / needs_review / failed)
        ▼
  Redis stream renfield:tasks:mobilepush  →  consumer group in API pods
        │  dedupe in mobile_capture_log  →  push relay  →  APNs  →  phone
        ▼
  app wakes → GET /api/mobile-capture/captures?ids=…  (authenticated re-fetch)
```

### Backend: a thin dedicated route over the shared bridge [R2 edited]

`POST /api/mobile-capture/document` — a thin route plus
`services/mobile_capture.py` over `services/folder_ingest.ingest_document`, the
same shape as email ingest. Why not reuse `/api/folder-ingest/document`:

- **Route-scoped credentials.** A new `ROUTE_MOBILE = "mobile_capture"` in
  `ingest_credentials`, whose route check already exists. A token from a phone
  cannot push into folder ingest, and vice versa.
- **Honest provenance:** `documents.source = "mobile_capture"`. Downstream
  consumers key on `source`, so each opts in deliberately (see Rollout).
- **Its own flag** and health contract.
- **Capture-specific payload:** `capture_id`, business-meal details, and device
  metadata.

Request (multipart):

| Part | Content |
|---|---|
| `file` | **PDF only** in v1 (magic-byte sniff; anything else → `failed/unsupported_format`). |
| `metadata` | JSON: `capture_id` (UUID, required), `filename`, `captured_at` (ISO-8601, device clock), `sha256`, `details` (see Business-meal details), `client_build`, **[R3]** optional `project_id`, **[R5]** optional `extensions` — an opaque, size-capped JSON object keyed by plugin namespace (e.g. `{"expense_reports": {"trip_ref": "<report id or offline client UUID>"}}`); the core never interprets it and hands each namespace to the owning plugin via a hook (see Plugin architecture) |
| headers | `X-Mobile-Capture-Contract`, `X-Client-Build` |

What the request **cannot** carry: owner, tier, knowledge base or target
instance. Unknown keys are ignored.

Route-side order:
1. flag gate;
2. Bearer via `resolve_ingest_client(route=ROUTE_MOBILE)`;
3. worker-alive gate;
4. metadata + details validation;
5. streamed, size-capped read;
6. magic-byte check;
7. sphere from the credential row;
8. `ingest_document(..., source=MOBILE_CAPTURE_SOURCE, file_to_paperless=<setting>)`;
9. upsert `mobile_capture_log (client_id, capture_id) → document_id, state`, plus the details row — in the same transaction as the create where the bridge allows, otherwise immediately after with an idempotent upsert.

Same `capture_id` with different bytes is rejected as `failed/capture_id_conflict`
rather than creating a second document.

Further routes (all authenticated by the same ingest credential, scoped to its
own `client_id`):

| Route | Purpose |
|---|---|
| `GET /api/mobile-capture/health` | enabled, instance label, contract versions, feature list, max size, cert pin confirmation |
| `GET /api/mobile-capture/captures?ids=…` / `?since=…` | status of this device's captures: `accepted`, `processing`, `processed`, `needs_review`, `failed`, plus a Paperless filing flag. **No title, amount or issuer** in v1 — the app shows what it captured locally. |
| `PATCH /api/mobile-capture/captures/{capture_id}` **[R4]** | edit **this capture only**: `details`, `project_id` (or null), `extensions` (R5: opaque, plugin-validated — e.g. the trip assignment). Only for captures created by the calling `client_id` (404 otherwise); the same server-side validation as at upload; `If-Match` on the capture's version → 412 if the web edited it meanwhile; moving the capture into or out of a `submitted`/`settled` report → 409 `report_locked`. Re-mirrors details/tags to Paperless. |
| `POST/PATCH /api/plugins/expense-reports/app/trips…` **[R4, moved to the plugin in R5]** | mounted **only when the plugin is loaded**; authenticated with the same ingest credential through the host contract's auth dependency; the owner's own open trips only: create (idempotent `client_ref`), end, times, days (country, provided meals), mileage legs; a trip no longer `open` → 409 |
| `PUT /api/mobile-capture/device` | register or refresh the APNs device token + environment + build |
| `DELETE /api/mobile-capture/device` | unregister (sign-out of an account in the app) |

Everything after `ingest_document` is unchanged and free: hash dedup, PDF-Split,
the Paperless reconciler, Schicht-A facts, generated titles, `document_date`,
KB near-duplicate detection (#1170), and `documents_changed` user events.

**PDF-Split children.** A capture containing several receipts becomes N child
documents. **[R7 corrected]** Split children inherit the parent's `source`,
so they stay `source="mobile_capture"`, and they carry `split_from_document_id` → the
capture's document. The exception is a child that dedups onto an existing document,
which is deliberately **not** lineage-stamped. Aggregation therefore uses the child
ids stored from the split plan on the capture ledger, not the lineage column alone.
`split_archived` (the parent) and `split_review` are mapped explicitly. Capture status is aggregated over the children: `processed` when all
children are completed; `needs_review` if the split itself or any child needs
review.

### Server-authoritative sphere, two levels

- **Level 1 — which instance.** Chosen in the app (v1) or by the router (later).
  Structurally not server-authoritative; mitigated by explicit per-account
  choice, a visible account colour/label on the capture screen, and later the
  router's audit log.
- **Level 2 — which sphere inside the instance.** Server-authoritative: the
  credential row carries owner/tier/KB (ingest-credentials Phase 4, shipped).

## Auth, pairing and revocation [R2]

### Credential

Reuse `ingest_credentials` with the third route value. Nothing new about
hashing, timing equalisation, revocation or `last_authenticated_at`.

- One credential per **(person, device, instance)**. `client_id` is operator- or
  system-generated (`mobile-<user>-<device-short>`), opaque downstream.
- **Scope [R4 edited]** — enumerated, and nothing else:
  - push captures;
  - read the status of **its own** captures;
  - **edit its own captures** after processing — business-meal details, project
    assignment and trip assignment, through `PATCH /api/mobile-capture/captures/{capture_id}`;
  - read ids/names of the owner's **own active projects and open trips**;
  - create and maintain the owner's **own open trips** (start, end, times,
    per-day country and provided meals, mileage legs) — **R5:** only through the
    `expense_reports` plugin's routes, and only where the plugin is loaded;
  - register its device token.
- **Never** through this credential: tier, owner, KB, submitting or settling a
  report, confirming amounts, exports, or any other user's data. Final and
  financial actions (submit, settle, confirm amounts, export) require the web
  session. The later `session` credential kind is the only path to widen this.

### Pairing on an auth-on instance (xidra first)

1. The user logs into the instance's web UI with their own account (cookie
   session + CSRF, as today) and opens **Einstellungen → Mobile Geräte → Gerät
   koppeln**.
   - A normal user can pair only for themselves: owner = the logged-in user.
   - The tier defaults to that user's configured default for mobile captures;
     the user may pick any tier for their own documents.
   - **[R7 corrected] The KB is the paired user's own mobile-capture KB**,
     written as the credential row's `kb_name`. It is created at pairing if absent:
     - **name:** `"<MOBILE_CAPTURE_KB_NAME> · u<user id>"`. KB names are globally
       unique, and the stable user id survives a rename;
     - **owner:** `owner_id` = that user;
     - **default tier:** the user's mobile-capture tier;
     - **creation:** idempotent under concurrent pairings (insert-or-reselect).
       The existing folder-ingest get-or-create is not reused, because it
       creates ownerless KBs and does not handle a concurrent create.
     - **lookup by owner, not by name:** the KB is found by
       `(owner_id, purpose = mobile_capture)`, and its **id** is stored on the
       credential row. Any user allowed to create KBs can create one with any
       free name, so a name-based lookup could be squatted: a pre-created
       "<prefix> · u<id>" would capture another user's receipts. Pairing refuses
       if a KB with that name exists under a different owner.
     - **user deletion:** deleting a user revokes their mobile credentials and
       archives their mobile-capture KB (never re-adopted by name).
     Reason: ingest dedup keys on `(file_hash, knowledge_base_id)` and returns
     the existing document. The document visibility filter's owner branch is
     the KB owner. With one shared KB, a second user's identical bytes would
     link to the first user's document, and the KB owner would see every user's
     tier-0 captures.
   - **A duplicate never links across owners.** If the matched document's owner
     is not the credential owner, the capture is recorded as `duplicate`
     without a document reference. The same owner check applies to **every**
     bridge result that references an existing row: duplicate, retry
     (in progress), the concurrent-create winner, and re-ingest of a failed row.
     An admin changing a credential's KB must pick a KB owned by the credential
     owner. Capture details and project/trip links are
     keyed on `(client_id, capture_id)`.
   - An admin can pair on behalf of another user.
2. The backend mints the credential in a **pending** state and a **single-use
   pairing code** in Redis (atomic `GETDEL`, short TTL, the
   `sso_handoff_store` pattern). The page shows a QR containing only
   `{instance_url, instance_label, pairing_code, tls_spki_pin}`.
3. The app scans the QR, validates the pin against the live TLS connection, and
   `POST`s `{pairing_code, device_name}` to `/api/mobile-capture/pair`.
   - That route is unauthenticated apart from the code; it is structurally
     CSRF-exempt because it carries no cookie, and it is rate-limited.
   - It returns the long-lived token **once** and activates the credential.
4. The app writes the token to the Keychain and creates the account. The web
   page turns green, via the existing user-events channel, so a hijacked code is
   noticed immediately.

An unused code expires; a replayed code fails; a pending credential that was
never activated is removed with the code's expiry.

**Auth-off household (Phase 2)**: the pairing page is reachable by anyone on the
LAN when auth is off. It is accepted there, because everyone on that LAN already
has the same full access, and recorded as a residual. **Decided (R4): any device
on the household LAN may pair, no PIN.**

### Keychain storage

- `kSecClassGenericPassword`, one item per account.
- Accessibility `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`. The
  `ThisDeviceOnly` suffix means the item is bound to the device and excluded
  from backups and iCloud Keychain migration. "After first unlock" is required
  because background relaunches must read it; `WhenUnlocked` would break
  background completion handling. Phase 0 verifies this.
- **Honest residual:** a queued background upload task stores its request,
  including the `Authorization` header, in the system's transfer daemon until it
  runs. That is OS-protected storage, not our Keychain.

### Lost phone

On each paired instance, **Mobile Geräte → revoke** (the user for their own
devices, or an admin). Revocation disables the credential and deletes its APNs
device token. Captures still in the outbox of a lost phone are protected by the
device passcode and the file's Data Protection class.

## Business-meal details [R2]

Business entertainment receipts need occasion and participants recorded. The
place where that information exists is the moment of capture.

### Fields

| Field | Type | Limits |
|---|---|---|
| `occasion` | string | 1–200 chars |
| `participants` | list of strings | 0–20 entries, each 1–120 chars |
| `place_name` | string | 0–200 chars (restaurant or place; see Location) |
| `note` | string | 0–1000 chars |

**[R7]** The form follows the capture type. The user chooses the type **after
scanning**, on the send sheet. Where supported, the choice is pre-selected by the
on-device suggestion (see "On-device document type suggestion").
- **Beleg:** no details.
- **Bewirtung:** details; `occasion` required.
- **Rechnung** and **Sonstiges:** no details. They are hints only; the server
  extraction decides what the document is.

**Validation** (server-side, authoritative; the app mirrors it for UX):
- Unicode NFC normalisation;
- reject NUL and other control characters (newline allowed in `note` only);
- trim, collapse whitespace, and reject empty entries;
- enforce the limits, rejecting instead of truncating (`failed/invalid_details`,
  which the app shows before upload);
- no HTML or Markdown interpretation anywhere.

### Where they are stored

| Option | Verdict |
|---|---|
| Columns on `documents` | pollutes the shared document model with one producer's schema |
| Schicht-A `document_facts` | **rejected**: Schicht-A re-extraction purges and rewrites a document's fact set, and these are not extracted facts; routing them there would also blur "deterministic from the document" vs "typed by a person" |
| Paperless custom fields only | Paperless is an optional downstream; the KB and the app would lose the data when filing is off or fails |
| **Dedicated table `capture_details` (1:1 with the document), mirrored to Paperless custom fields** | **recommended** |

`capture_details`:
- **Columns:** `document_id` (FK, cascade), `capture_type`, `occasion`,
  `participants` (JSON array), `place_name`, `note`, `entered_by_user_id`,
  `updated_at`.
- **Visibility:** the parent document's circle visibility (same owner and tier;
  never more visible than the document).
- **Deletion:** deleted with the document.

### How they reach Paperless

**[R7 corrected] This is new work, not an extension of an existing PATCH.**
- **What the filing leg does today:** its post-consume PATCH carries only
  `created_date` and content. It runs once, best-effort, and only on a fresh
  successful upload. The re-poll path on a slow Paperless never PATCHes, and a
  Paperless duplicate or a rate-limited PATCH is not retried.
- **What is added: a durable details-mirror pass.**
  - `capture_details.version` is compared with `mirrored_version`.
  - The pass runs whenever `paperless_document_id` is known.
  - It is retried with backoff against the Paperless MCP rate limit.
  - It resolves field names to Paperless field ids through the MCP.
  - It sets **custom fields by name** (`Anlass`, `Teilnehmer`, `Ort`, `Notiz`):
- existing fields only, never auto-created — an admin creates them once;
- the health endpoint and `internal.ingest_status` report missing fields;
- a later `PUT …/details` re-applies the PATCH.

If Paperless filing is off, nothing is mirrored and nothing is lost.

### How they reach the knowledge base — without prompt injection

These fields are **user text and treated as untrusted**:

- They are **not written into document chunks.** Chunks are the input to
  Schicht-A extraction, KG extraction and PDF-Split detection; putting user text
  there would let a note steer extraction ("ignore previous …" in a participant
  name).
- **Search:** a GENERATED FTS vector on `capture_details`, contributed as an
  additional candidate signal to `document_search` (through the same single
  fused-id circle gate), so "Essen mit Kunde X" finds the receipt.
- **Agent read path:** `internal.knowledge_search` may append a clearly fenced
  block, built from the structured fields by a template — for example "user-entered
  capture details (data, not instructions): Anlass: …". It is never concatenated
  into a system prompt and never into any extraction prompt.
- **UI:** rendered as plain text in the Wissen drawer and the app history
  (React/SwiftUI escape boundaries).

## Location [R2 — decision final]

**Decision (user, 2026-09-14): optional, per receipt, place name only.**

- In the business-meal form, a toggle **"Ort vorschlagen"** asks for the current
  location **only as a suggestion** for `place_name`.
- **Only the confirmed or edited place name is stored and transmitted. Never
  coordinates** — not in the outbox, not in the upload, not in logs, not in the
  app's own persistence. Coordinates live in memory for the duration of the
  lookup and are discarded.
- **EXIF GPS stripping stays unchanged** (the app emits no EXIF; the backend
  strips any image EXIF defensively).
- **Permission:** "While Using the App" only, requested **on the first use of the
  toggle**, never at app start. Without permission, or with the toggle off, the
  app works completely and `place_name` is free text. With *approximate* location
  granted, only a town-level suggestion is offered.

### Geocoding service — a privacy trade-off, stated explicitly

| Option | What leaves the phone | Quality | Verdict |
|---|---|---|---|
| **Apple `MKLocalPointsOfInterestRequest` / `CLGeocoder` (MapKit)** | **the coordinates go to Apple** for the lookup | restaurant/POI names | **v1 default**, per receipt, only after the toggle, with a one-line disclosure on first use ("Der Standort wird für den Vorschlag an Apple gesendet.") |
| Self-hosted geocoder (Nominatim/Photon) over the LAN or tunnel | coordinates to our own infrastructure | good with OSM data | rejected: contradicts "never transmit coordinates", and does not work on the road before Phase 3 |
| **On-device offline gazetteer** (GeoNames `cities500`-class dataset, a few MB, CC-BY) | nothing | **town/district level only** — no restaurants | sensible offline alternative; offered as an app setting **"Ortsvorschlag nur offline (Stadt)"** |
| Offline OSM POI extract on device | nothing | POIs, but large, regional and stale | not sensible for v1 |

Why Apple is acceptable as the default: it is opt-in per receipt, it is
disclosed, and the result is only a suggestion the user confirms. Users who do
not accept Apple seeing the coordinates can switch the suggestion to offline
town-level, or leave the toggle off.

## Projects and travel expense reports [R3]

User requirement: on a business trip, receipts can be assigned on xidra to a
**project** and to a **travel expense report**.

### What exists today (researched, not assumed)

- **Projects exist** (`models/database.py::Project`, `api/routes/projects.py`,
  `services/project_service.py`; gated `projects_enabled`, off on the household).
  - **Fields:** `name`, `description`, `owner_id`, `status`, `circle_tier`
    (default 2).
  - **One dedicated knowledge base per project** (1:1, `knowledge_base_id`).
  - **Listing is owner-only when auth is on:** `GET /api/projects` filters
    `owner_id == user.id`, and single-project routes are owner-gated 404. The
    `circle_tier` column exists but does **not** currently grant visibility to
    anyone else.
- **Links to projects today:** conversations, meetings and notes have a nullable
  `project_id` (`SET NULL` on delete). **Documents have no `project_id`** — a
  document belongs to a project only by being ingested into the project's KB.
  The project timeline (`services/project_timeline.py`) merges project-KB
  documents, meetings, minutes decisions, chats and notes.
- **The web UI** has a shared `components/meetings/ProjectSelect.tsx` dropdown
  ("no project" clears the link; renders nothing when there are no projects).
- **No travel, expense or trip concept exists** anywhere in code or schema.
  The only hits are test-data file names.
- **Paperless** options already used by the filing leg: tags (resolve-or-create,
  config-gated), document types, correspondents, storage paths (in the metadata
  extractor's taxonomy). **[R7 corrected]** Custom fields are **not** set by the
  filing leg today. Its post-consume PATCH carries only `created_date` and
  content, so custom fields need the new details-mirror pass (see Business-meal
  details).

### Project assignment

**Link, do not move.** A receipt assigned to a project stays in the KB its
credential fixes. It gets a link row instead of being ingested into the project's
KB:

| Option | Verdict |
|---|---|
| Ingest into the project's KB | **rejected**: the client would choose the KB, breaking server-authoritative sphere routing; dedup is per `(file_hash, kb)`, so reassigning later would duplicate; the project KB's default tier would silently re-tier the receipt |
| **`document_project_links` (document_id, project_id, assigned_by, source)** | **recommended**: sphere unchanged, reassignable, usable later for any document (not only captures), and a new project-timeline source |

- **Picker in the app:** `GET /api/mobile-capture/lookups` (ingest credential)
  returns `{projects: [{id, name}], trips: [...]}`.
  - Projects are only the **active** ones the credential's owner may see —
    exactly the `/api/projects` visibility rule (owner-only today). If project
    visibility is widened later, it is widened in one place and both surfaces
    follow.
  - This is a deliberate, enumerated widening of the ingest credential's read
    scope, limited to ids and names of the owner's own active projects and open
    trips (**decided, A5**).
- **Server-side validation at upload:** the project exists, is active, and is
  visible to the credential owner. Otherwise the receipt is ingested **without a
  project**, the capture is marked `needs_review` with reason
  `assignment_invalid` (push "Prüfung nötig"), and the owner can fix the link in
  the history or on the web. **Never rejected, never lost.**
- **PDF-Split children inherit** the link.
- **Instance without projects** (`projects_enabled=false`): the health feature
  list omits `projects`, the app hides the picker, and a stray `project_id` is
  ignored with a log line, never an error.

### Travel expense report — its own entity

**Source of truth — decided (A1): Renfield tables, mirrored to Paperless.**

| Option | Verdict |
|---|---|
| Paperless only (tag per trip + custom fields) | rejected: no status workflow, no per-item confirmation, no report header; Paperless filing is optional and can fail; business logic encoded in tag names is brittle |
| **Renfield tables = truth, Paperless = mirror** | **recommended** |
| Instance-specific accounting system as truth | out of scope for the public design; possible as an export adapter |

Schema **[R4 edited; R5: these tables live in the `expense_reports` plugin's own migration environment, prefixed `plugin_expense_reports_*` — names below are shortened]**:

```
expense_reports
  id, owner_user_id (FK users, SET NULL), client_ref (UUID, unique per owner — offline creation idempotency)
  purpose              text 1–300      -- Anlass / Reisezweck
  destination          text 1–200
  start_at, end_at     timestamptz     -- end_at NULL while the trip is active
  times_recorded       bool            -- false = dates only (times optional, A10)
  timezone             text            -- IANA; reference zone for calendar-day boundaries
  per_diem_requested   bool default false   -- R4: trip wants per-diem calculation → times REQUIRED
  project_id           FK projects SET NULL, nullable
  status               open | submitted | settled
  circle_tier          int             -- see Permissions
  submitted_at, settled_at, settled_by_user_id, created_at, updated_at, version

expense_report_items
  id, report_id (FK, CASCADE), document_id (FK documents, CASCADE)
  assigned_by        active_trip | manual
  category           lodging | transport | meal_entertainment | incidental | other
  amount_confirmed   numeric(12,2) NULL, currency char(3) NULL   -- NULL = not yet confirmed
  note               text 0–500 (untrusted user text)
  UNIQUE (report_id, document_id)

expense_report_days            -- R4, only meaningful with per_diem_requested
  report_id (FK, CASCADE), day date, country_code char(2) (ISO 3166-1),
  breakfast_provided, lunch_provided, dinner_provided bool, overnight bool
  UNIQUE (report_id, day)

expense_mileage_legs           -- R4
  id, report_id (FK, CASCADE), leg_date date,
  from_text text 1–200, to_text text 1–200,   -- free text, NEVER coordinates, no GPS track, no route lookup
  km numeric(7,1) > 0, vehicle_type text (must exist in the rate set), note text 0–300

expense_computed_lines         -- R4, live preview while open; frozen snapshot at submit
  id, report_id, kind per_diem | meal_reduction | mileage, day | leg_id,
  inputs jsonb (absence hours, country, provided meals, km, vehicle type),
  rate_key, rate_value, currency,
  rate_set_id, rate_set_version, rate_valid_from, rate_valid_to, rate_set_checksum,
  formula text (fixed template, rendered), amount numeric(12,2), computed_at

expense_report_events          -- R4, append-only audit (there is no four-eyes step)
  id, report_id, action (created|ended|submitted|withdrawn|settled|reopened|exported|computed),
  from_status, to_status, actor_user_id, via (web|app), at, reason text 0–500
```

(The `mileage` item category from R3 is dropped: mileage is no longer a receipt,
it is a computed leg.)

- **Amounts come from the document, confirmed by a person.** The Schicht-A
  `amount` fact (total) is shown as a **suggestion**; only `amount_confirmed`
  enters totals and exports. An unconfirmed item is exported as "unconfirmed",
  never silently as the extracted value.
- **Paperless mirror** (best-effort, via the existing filing leg):
  - a tag per report (`Reisekosten <yyyy-mm> #<id>` — no destination or purpose in
    the tag name);
  - a custom field `Reisekostenabrechnung` with the report id;
  - the category as a custom field.
  - Existing fields only, as for the business-meal fields. The tag is a mirror;
    deleting it in Paperless changes nothing in Renfield.

### Active trip (app)

- **"Reise starten"**: purpose, destination and optional project. It is created
  on the server when reachable. **Offline it is created locally** with a
  `client_ref` UUID and synced idempotently on the next home connection
  (`POST /api/expense-reports` with `client_ref` → the same row on retry).
- **While a trip is active**, every new capture on that account defaults to
  `trip_ref` = that trip (and to the trip's project). The assignment is shown on
  the capture screen and **overridable per receipt** (other trip, none, other
  project).
- **"Reise beenden"** sets `end_at`; it does not submit. Submitting is a separate,
  deliberate step, taken on the web **[R4]**.
- **[R4] Times and per diems.** At "Reise starten" a toggle **"Verpflegungspauschale
  berechnen"** sets `per_diem_requested`.
  - **Trips without it:** times stay optional (A10).
  - **Trips with it:** departure and return **times are required**. "Reise beenden"
    does not complete until both are present and `end > start`, and the web
    refuses submit for the same reason (`422 times_required`).
  - **What the app shows:** a clear inline message on the trip card, a badge in
    the history, and time pickers pre-filled from the trip's start and "Reise
    beenden" moments, which the user confirms or corrects.
  - **Per-day entry (same screen):** country (ISO picker, defaulted from the
    previous day — never from geolocation), provided breakfast/lunch/dinner, and
    overnight.
  - **Mileage legs** (date, from, to, km, vehicle type) can be added during or
    after the trip.
  - Missing data never blocks capturing receipts.
- **Server validation at upload:** the trip belongs to the credential owner and is
  `open`. Otherwise the receipt lands **without a trip**, `needs_review` with
  reason `assignment_invalid`, and is fixable later. A trip `submitted` in the
  meantime is not reopened by an upload.
- **Cache:** projects and trips are cached per account in the app. They are
  refreshed at app foreground on the home network and after every successful
  upload. A stale cache is harmless because the server validates.

### Relation to the business-meal details

- A business meal on a trip is **one capture with `capture_type=Bewirtung`
  (occasion, participants, place) that is also an expense item** with category
  `meal_entertainment`. The details stay in `capture_details`; the item only
  references the document. No duplication.
- **Entertaining others is not the traveller's own meals.** The traveller's own
  meal costs on a business trip are typically handled through per-diem
  allowances rather than by the receipt amount. The structure keeps the two
  apart (a Bewirtung item vs a computed per-diem line, see below), and the tax
  treatment is a point for the tax advisor, not something this design decides.

### Statutory structure — what a German travel expense report typically contains [R4 edited]

**This is a structure checklist, not tax advice.** Rates, thresholds and
treatment change and are deliberately **not** hard-coded here. Every point marked
*[StB]* is for the tax advisor; the concrete checklist is below.

| Element | Typically needed | Reports v1 (decided A2: largest scope) |
|---|---|---|
| Traveller, purpose/occasion, destination(s) | yes | **in** |
| Start and end — date **and time** of departure/return | yes, times matter for per-diem thresholds | **in**; optional without per diems, **required** with per diems |
| Means of transport | yes | item categories + mileage vehicle type **in** |
| Receipts per cost item | yes | **in** (items = documents) |
| Overnight costs; breakfast separated from the room rate on hotel bills | yes | `lodging` items **in**; breakfast split **out** (item note only) *[StB 8]* |
| Travel costs actual (ticket, taxi, parking, tolls) | yes | **in** as items |
| **Mileage allowance** for a private vehicle (km × rate) | yes if used | **in** — per leg, computed *[StB 7]* |
| **Per diems for additional meal expenses** (by absence duration, domestic/foreign rates) | yes | **in** — computed *[StB 1–3, 5, 6, 9]* |
| **Reduction of per diems for provided meals** (breakfast/lunch/dinner) | yes | **in** — per day *[StB 4]* |
| Business-meal receipts with occasion and participants | yes for entertainment | **in** (business-meal details) *[StB 11]* |
| Approval / signature | depends on the company process | owner submits and settles, audit trail, no signature, no four-eyes step *[StB 10]* |
| VAT split per item | depends | **out** (Phase 5 receipt facts) |

### Per diems and mileage allowance [R4]

**Decided (A2): v1 computes per diems (domestic + foreign), meal reductions and
mileage allowance.** The calculation ships **dark behind its own flag**
`EXPENSE_REPORTS_PER_DIEM_ENABLED`, independent of the plugin merely being loaded (R5), so reports
without calculation can go live first.

#### Hard go-live gate

Per diems and mileage go live on xidra **only after the tax advisor has reviewed**:
1. this calculation specification;
2. the golden test cases (inputs → expected lines);
3. the rate set(s) in use.

This is enforced in two places, not just by process:
- the flag must be on; **and**
- the rate set covering the trip's dates must carry a completed `review` block.

Without both, **no computed lines are produced**. The app and web show
"Berechnung nicht freigegeben", and the export states "Verpflegungspauschalen
und Kilometergeld nicht berechnet". A rate set edited after review loses its
review (the checksum changes), so the gate re-closes by itself.

#### Rate sets are configuration, never code

- A versioned **rate file** per instance, served from the instance's private
  configuration (a ConfigMap, like `agent_roles.yaml`). This repository holds only
  the **schema, a placeholder example, and tests with synthetic rates**.
- **Loaded and schema-validated at startup (fail-closed).** An invalid or
  overlapping file disables computation, reports on the health endpoint, and
  raises an `ops_alert`; the rest of the app keeps working.
- **Several sets with non-overlapping validity**; the rate is looked up per
  day/leg by date. A trip spanning a rate change uses each day's own set, and the
  lines say so.
- A file is used here rather than a DB table because a versioned, diffable file
  is what a tax advisor can review, and it is the project's config-not-code idiom.

```yaml
# SCHEMA EXAMPLE — placeholder values only, never real rates in this repository
rate_sets:
  - id: example-2026
    version: "2026.1"
    valid_from: 2026-01-01
    valid_to: 2026-12-31
    source: "<official publication reference>"
    currency: EUR
    review:                        # HARD GATE: computation refuses a set without it
      reviewed_by_role: tax_advisor
      reviewed_at: null            # date; null = not reviewed
      reference: null              # e.g. the advisor's written confirmation id
    rounding: {mode: <half_up|...>, places: 2}          # [StB 9]
    per_diem:
      countries:                   # ISO 3166-1 alpha-2; the domestic country is one entry
        DE: {full_day: <amount>, partial_day: <amount>}
        XX: {full_day: <amount>, partial_day: <amount>}
      absence_rules:               # parameters of the fixed algorithm, not code   [StB 2]
        single_day_min_hours_for_partial: <hours>
        multi_day_arrival_and_departure_day: partial_day
        intermediate_full_day: full_day
      day_country_rule: <last_location_before_midnight|...>  # [StB 3]
      meal_reduction:              # per provided meal, basis = full-day rate of that day's country   [StB 4]
        breakfast_pct: <pct>
        lunch_pct: <pct>
        dinner_pct: <pct>
        floor_at_zero: true
    mileage:
      vehicle_types:               # [StB 7]
        car: {per_km: <amount>}
        motorcycle: {per_km: <amount>}
```

#### Calculation (a pure function, no I/O)

Input: the report header, days, legs and the loaded rate sets. Output: the
`expense_computed_lines`. Monetary values use `Decimal` throughout.

1. **Validate.**
   - `per_diem_requested` → `start_at` and `end_at` with times, `end > start`.
   - Every calendar day in `[start, end]` (in the report `timezone`) has an
     `expense_report_days` row with a country.
   - Every leg's `vehicle_type` exists in the leg date's rate set.
   - Failure → no lines, plus a list of precise, localised problems shown in the
     app and web.
2. **Absence per day.** Intersect the trip interval with each calendar day in the
   report timezone. Durations come from absolute `timestamptz`, so a DST change
   does not add or lose an hour silently (tested).
3. **Per-diem tier per day** from `absence_rules` (single-day vs arrival/departure
   vs intermediate day) and the day's country per `day_country_rule` → the rate.
4. **Meal reductions per day.** For each provided meal: `pct × full_day rate of
   that day's country`. The day's per diem minus reductions is floored at zero
   when `floor_at_zero`.
5. **Mileage per leg.** `km × per_km(vehicle_type, leg_date)`.
6. **Rounding** per the rate set, applied per line (the rule itself is *[StB 9]*).

**Traceability.** Every line carries its inputs, the rate key and value, the rate
set id/version/validity/checksum, and a formula rendered from a fixed template,
e.g.:
- `"2026-10-05 · DE · 9,5 h abwesend → Teilstag · Satz <rate> (Satz gültig 2026-01-01–2026-12-31, Satzversion 2026.1)"`
- `"Frühstück gestellt: −<pct> % × Tagessatz <full_day> = −<amount>"`
- `"2026-10-05 · 142,0 km × <per_km>/km (Pkw) = <amount>"`

The export prints all of this, so every computed amount can be recomputed by
hand from the export alone.

**Live vs frozen.** While `open`, lines are recomputed on every change (preview).
At **submit** they are frozen into `expense_computed_lines` with `computed_at`
and an `expense_report_events` row. A later rate file change never alters a
submitted or settled report; reopening (admin) recomputes, and the event log
keeps the old snapshot reference.

Tests: golden files over synthetic rate sets — single-day thresholds,
multi-day, border-crossing days, all meal combinations incl. floor at zero, DST
change, rate-set boundary inside a trip, unknown vehicle type, unreviewed set
(no lines), and checksum change after review (gate re-closes).

#### Tax-advisor checklist [StB] — the calculation depends on these answers

None is guessed. Each answer lands in the rate file or in this specification
before the go-live gate opens.

1. **Rate values and source** for the domestic country and every foreign country
   in use; validity periods; the update cadence and who maintains the file.
2. **Absence thresholds and tiers**: single-day minimum hours; arrival/departure
   day of multi-day trips; intermediate days.
3. **Country of a day** for border-crossing and return days (which location
   counts).
4. **Meal reductions**: percentages, basis rate, whether they apply when the meal
   is paid by the traveller or invoiced to the company, floor at zero.
5. **Owner-managed company**: treatment of per diems when the traveller is the
   company's owner; how it is booked.
6. **Long-term assignment limit**: whether and how the multi-month same-location
   rule must cap per diems (Renfield does not track this in v1 — the advisor
   decides whether that is acceptable).
7. **Mileage**: rate per vehicle type; required proof of km (route
   documentation, logbook); exclusion of company cars.
8. **Hotel bills with breakfast included**: whether and how breakfast must be
   separated from lodging.
9. **Rounding rule** (per line vs per total).
10. **Required header fields and times**; whether a signature or approval step is
    required given there is **no four-eyes step** (owner submits and settles).
11. **Business meals**: form requirements (occasion, participants, place,
    signature) and their relation to per diems on the same day.
12. **Export**: required content, format and retention of the report and the
    receipt bundle.

### Export / handover

- `GET /api/expense-reports/{id}/export.pdf` and `.csv` — a **generic accounting
  export**:
  - header (traveller, purpose, destination, period, project, status);
  - one line per item (document date, merchant from facts, category, confirmed
    amount and currency, "unconfirmed" marker, Paperless document id if filed, KB
    document id);
  - sums over confirmed amounts only, per currency, with no conversion.
  - **[R4]** the computed per-diem, meal-reduction and mileage lines with full
    traceability (see above), or the explicit "not calculated" statement;
  - a "not included" list for items whose receipt the caller may not see or whose
    file is unavailable, without title or content.
- **[R4] Decided (A8): ZIP of the receipt PDFs, right away.**
  `GET /api/expense-reports/{id}/export.zip` (web session; owner, or an admin):
  - **Visibility per document.** Before streaming, each item's document is
    checked against the caller's circle filter.
    - Visible → included.
    - Not visible → left out and listed in the summary PDF as "Beleg <nn>: nicht
      enthalten (keine Berechtigung)".
    - Bytes unavailable → listed as "nicht enthalten (Datei nicht verfügbar)".
    - Source of the bytes: the ingest recovery copy, else the Paperless original
      via the Paperless MCP (Phase 1.5 verifies how long the recovery copy is
      retained).
  - **Bounded.** The summed size of included files is checked **before** the
    first byte is sent. Above `EXPENSE_REPORTS_EXPORT_ZIP_MAX_MB` the route answers 413
    with a clear message. The bound is a DoS/egress guard, set from a measured
    realistic report size in Phase 1.5, not a guessed product limit.
  - **Streamed, never stored.** A streaming ZIP writer: STORED entries, since
    PDFs are already compressed; files are read chunk-wise. No temp file, no
    cache, `Cache-Control: no-store`. It is generated on demand, every time.
  - **Filenames carry no personal data** beyond what the caller already sees,
    from a fixed template:
    - `Reisekosten_<report-id>.pdf`
    - `Reisekosten_<report-id>.csv`
    - `belege/<nn>_<yyyy-mm-dd>_<category>.pdf` (index, document date, category)
    - never a merchant, participant, purpose, destination or person name.
      Template-generated names also rule out path traversal inside the archive.
  - **Rate-limited**, and every export writes an `expense_report_events` row
    (`exported`, actor, via).
- **Instance-specific export adapters** (hand-over into a particular accounting
  system or tax portal) are out of this repository; they are documented in the
  private instance repository. The generic export is the contract they consume.

### Permissions and circles

- **A report is visible to its owner and to admins** in v1. It is not
  circle-shared, deliberately matching today's owner-only projects. `circle_tier`
  is stored (default = the owner's mobile-capture tier) so sharing can be enabled
  later without a migration.
- **Status model [R4 edited] — decided (A6): the owner settles.**
  - `open`: the owner edits the header, items, days and legs; from the app
    (own open trip) or the web.
  - `open → submitted` (owner, web): items, days and legs are locked, and
    computed lines are frozen.
  - `submitted → open` (owner, web): withdraw; the frozen snapshot is discarded
    and the event recorded.
  - `submitted → settled` (**owner**, web): read-only, `settled_by_user_id` =
    the owner.
  - `settled → open`: **admins only** (reopen, with a mandatory reason), recorded
    in `expense_report_events`.
  - **There is no four-eyes step.** One person can create, submit and settle
    their own report. This is a deliberate user decision, recorded here and in
    the tax-advisor checklist (item 10); the append-only event log is the
    compensating control.
- **A report never raises the visibility of a receipt.** Items reference
  documents; every read of an item goes through the document's circle filter. A
  viewer who cannot see a document sees "1 item not visible to you", never its
  content. The report tier does **not** cascade onto documents, and a document's
  tier does not change when it is added to a report.
- **Adding** a document to a report requires that the caller can see the
  document, the report is `open`, and the caller owns the report.

### Instances that do not use this [R5 edited]

The `expense_reports` plugin is **not in `PLUGIN_MODULES`** (the default;
household and association). It is not imported at all, so:
- no routes are mounted;
- no plugin tables exist (its migrations never ran);
- `/api/config/features` has no `plugins.expense_reports` entry, so the web UI
  renders no nav, route or chunk;
- the mobile health feature list has no `expense_reports`, so the app hides the
  "Reisekosten" section and "Reise starten";
- an `extensions.expense_reports` block in an upload is dropped with a log line.

Nothing else changes. The project picker stays core and follows
`projects_enabled` independently. See **Plugin architecture** below.

## Plugin architecture: `expense_reports` [R5]

User requirement (R5): travel expense reports — per diems, mileage, exports,
tables, routes, the web page and the app section — are a **plugin**. An instance
that does not need them has **none of it present**: no mounted routes, no tables,
no UI, no feature flag lingering in the core. A dark flag with core-owned tables
and routes is explicitly **not** enough.

### How plugins work in Renfield today (researched)

| Mechanism | Evidence | Relevance |
|---|---|---|
| **Startup plugin loading** | `utils/config.py` `plugin_module` / `plugin_modules` (`PLUGIN_MODULE`, `PLUGIN_MODULES`, comma-separated `"module:callable"`); `api/lifecycle.py::_load_one_plugin` imports the module, calls the callable (async supported), records `_plugin_status`, and **logs and swallows failures** (`failed_plugins()`) | the activation switch per instance: env/ConfigMap, no code change |
| **Hook API** | `utils/hooks.py` — `HOOK_EVENTS` is a **whitelisted frozenset** (a typo raises `ValueError`); `register_hook(event, fn, priority)`; `run_hooks` does `await fn(**kwargs)` for every handler and logs errors | new extension points = new whitelisted events |
| **Async-only gotcha** | a sync handler is caught, skipped and silently inert (the twin self-access guard shipped dead once for this reason) | the contract must enforce `async def` at registration |
| **Route mounting** | `api/lifecycle.py` runs `run_hooks("startup", app=app)` then `run_hooks("register_routes", app=app)`; `ha_glue/bootstrap.py::ha_glue_register_routes` does `app.include_router(...)` for rooms, presence, satellites, camera, admin | plugin routers exist only when the plugin registered |
| **In-repo plugin (ha_glue)** | `ha_glue/` package in the repo, explicitly bootstrapped from `api/lifecycle.py`; **its tables go through the core alembic chain** (e.g. room/presence migrations in `alembic/versions/`) and `alembic/env.py` imports `ha_glue.models` for metadata | precedent for code location — **not** for tables: ha_glue tables exist on every instance |
| **External plugin (twin_adapter)** | a private package staged into the image build context and loaded via `PLUGIN_MODULES`; `PLUGIN_MCP_BINDINGS` marks a bound MCP server `degraded` when its plugin failed to load; forgetting to stage it broke the twin silently **four times** | precedent — and a warning against out-of-repo staging |
| **External plugin metadata (Reva)** | `alembic/env.py` reads `PLUGIN_METADATA_MODULES` to add a plugin `Base.metadata` to `target_metadata`, **only to stop autogenerate from emitting `drop_table`**; plugin migrations are not run by the core | shows the autogenerate footgun a plugin's tables create |
| **Migrations** | `alembic.ini` has a single `script_location = alembic`, no `version_locations`; one linear chain; `k8s/alembic-upgrade-job.yaml` runs `alembic upgrade head`; `env.py` uses `transaction_per_migration=True` plus a bootstrap commit that must precede `context.configure` | a second head in the core chain would break `upgrade head`; a plugin needs its own environment |
| **Frontend features** | `api/routes/config.py::FeatureFlags` is a **static Pydantic allowlist**; presence of optional things is detected, not configured — `_reva_wissensbasis_mounted(request)` probes `app.routes`, another helper probes connected MCP tools | precedent for "the backend reports what is actually loaded" |
| **Frontend plugins** | **no plugin loader**: `App.tsx` lazily imports every page into the one shared image and gates routes/nav on `/api/config/features` | the minimal clean variant is a build-time registry, not runtime loading |

### 1. The split

**Stays core** (useful without expense reports; used by every instance with mobile capture):
- mobile capture upload, pairing, ingest credentials, `mobile_capture_log`,
  device registration, push emitter + relay, capture status and history;
- `capture_details` (business-meal occasion/participants/place/note). A business
  meal needs these fields with or without a trip, and they mirror to Paperless via
  the core filing leg;
- **`document_project_links` and the project picker.** Projects are a core
  entity (`projects` table, `api/routes/projects.py`,
  `services/project_timeline.py`), and meetings, notes and conversations already
  carry `project_id` in the core. Linking a document to a project is generic, not
  travel-specific, and the project timeline (a core service) reads it. Putting
  the link into the plugin would make a core timeline depend on a plugin, or
  duplicate the concept;
- the generic **extension seam**: an opaque `extensions` field in upload/PATCH,
  plugin feature reporting, and the host contract (below).

**Moves into the plugin** `expense_reports`:
- all report tables (reports, items, days, mileage legs, computed lines, events);
- the per-diem/mileage calculation, the rate-file loader, the review gate and
  plugin-owned settings;
- the report REST routes (web) and the app trip routes;
- the `trips` lookup contribution and trip validation of `extensions.expense_reports`;
- PDF/CSV/ZIP exports;
- the Paperless mirror of reports (tag + report/category custom fields);
- the web "Reisekosten" page and its i18n namespace;
- the app's "Reisekosten" section contract, shown only when the instance reports it.

### 2. Plugin mechanics

**Package location — recommendation: in the repository, as an isolated plugin
package** (`src/backend/plugins/expense_reports/`, frontend
`src/frontend/src/plugins/expense_reports/`), activated per instance via
`PLUGIN_MODULES=plugins.expense_reports.plugin:register`.

| Option | Verdict |
|---|---|
| **In-repo isolated package, loaded only via `PLUGIN_MODULES`** | **recommended.** One shared image, tested and built with the core in one pipeline, reviewed like core code; not imported, mounted or migrated unless activated |
| Separate private repo staged into the build (twin_adapter pattern) | rejected: the image is shared across instances, so the bytes end up in every instance anyway — no isolation gained, only the proven staging failure mode (four silent regressions) |
| Separate pip package baked into per-instance image variants | rejected: breaks the shared-image deploy model and doubles build/deploy paths |

Consequence stated honestly: the plugin's **code bytes are present in the shared
image on every instance, but inert** — never imported, no route, no table, no
UI. The same holds for ha_glue on instances without Home Assistant. **Decided
(R5-1 = a):** this is the accepted meaning of "not present".

**Isolation enforced by tests, not discipline:**
- a core test asserts that **no core module imports `plugins.*`** (in the spirit
  of the existing worker-module isolation test);
- a plugin test asserts that the plugin imports **only** `utils.hooks`, the host
  contract package (`services/plugin_host/`) and third-party libraries — never
  core services or models directly;
- the core suite runs with the plugin not loaded; the plugin suite runs against
  the contract.

**Host contract — versioned, the only core surface a plugin may use.**
`services/plugin_host/` exposes `HOST_CONTRACT_VERSION` (semver) plus typed
dataclasses and a `HostAPI` protocol, handed to the plugin's
`register(host: HostAPI)`:

| `HostAPI` member | Purpose |
|---|---|
| `db_session()` | async session factory (the plugin's own tables, same database) |
| `auth.current_user` / `auth.ingest_client(route="mobile_capture")` | FastAPI dependencies: web session user, or the app's ingest credential (own-data scope) |
| `documents.visible_ids(user, ids)` / `documents.meta(ids)` / `documents.open_bytes(id)` | the circle filter and read access the exports need — never raw SQL on core tables |
| `projects.visible_active(user)` | the same owner-only rule as `/api/projects` |
| `facts.amount_suggestion(document_id)` | Schicht-A total as a suggestion |
| `events.publish_user_event(target, type, reason)` | content-free user events |
| `ops.alert(key, text)` | the shared `ops_alert` path (e.g. an invalid rate file) |
| `settings_namespace("expense_reports")` | reads the plugin's own env-prefixed settings (`EXPENSE_REPORTS_*`); the core `utils/config.py` holds **no** expense settings |

**New hook events** (added to `HOOK_EVENTS`, each payload carries
`contract_version`):

| Event | Fired by core | Plugin returns |
|---|---|---|
| `capture_extensions_apply` | upload and PATCH, after the core commit, once per namespace present in `extensions` | `{accepted: bool, reason_code?: str}` → a rejection marks the capture `needs_review/assignment_invalid` (never rejects the upload); no handler for a namespace → dropped + logged |
| `capture_lookups` | `GET /api/mobile-capture/lookups` | `{namespace: data}` merged under `plugins.<namespace>` (e.g. open trips) |
| `plugin_features` | `/api/config/features` and mobile health | `{namespace: {version, capabilities: [...]}}` → `FeatureFlags.plugins: dict[str, PluginFeature]` (one additive field; plugin-specific keys never enter the static allowlist) |
| `capture_state_changed` | the push emitter's transitions (processed/needs_review/failed) | nothing (observer) |
| `document_deleted` | `RAGService.delete_document`, after commit | nothing (the plugin records an event and freezes the item snapshot) |
| `paperless_filing_metadata` | **[R7 corrected]** the new durable Paperless metadata-mirror pass (the same pass as the business-meal fields) | `{tags: [...], custom_fields: {...}}` (existing fields only) to merge |
| existing `startup` / `shutdown` / `register_routes` | lifecycle | router mount under `/api/plugins/expense-reports/...` |

**Contract rules:**
- `register_hook` gains a registration-time check that rejects non-coroutine
  handlers loudly. This hardens against the known silent-inert failure for every
  plugin.
- The plugin declares `REQUIRES_HOST_CONTRACT = ">=1,<2"`. On a major mismatch it
  refuses to register (a load failure in `_plugin_status`), so a core/plugin
  mismatch is loud, never half-working.
- A failed plugin load raises an `ops_alert` and is listed in
  `internal.system_health`. Today this path exists only for MCP-bound plugins via
  `PLUGIN_MCP_BINDINGS`; R5 extends it to all failed plugins.
- **No half-mounted plugin.** In `register`, before mounting routes, the plugin
  verifies that its migration head is applied (its version table at head). If
  not, it fails to load, raises the alert and mounts nothing.

### 3. Database and migrations

**Recommendation: a separate Alembic environment owned by the plugin, with its
own version table**, run as a separate step only where the plugin is active.

| Option | Verdict |
|---|---|
| Plugin revisions in the core chain (ha_glue pattern) | rejected: the tables would exist on every instance — violates R5 |
| An Alembic branch + `version_locations` in the shared `alembic_version` table | rejected: two heads break the existing `alembic upgrade head` job; `version_locations` is static per image, so the core job would still create plugin tables wherever the directory exists; downgrades entangle core and plugin |
| **Own environment**: `plugins/expense_reports/migrations/` with its own `env.py`, `script_location` and **`version_table="alembic_version_expense_reports"`** in the same database | **recommended** |

Details:
- **The core job is untouched.** `alembic upgrade head` in
  `k8s/alembic-upgrade-job.yaml` never sees plugin revisions. Instances without
  the plugin are byte-identical.
- **Plugin job:** a second manifest `k8s/alembic-upgrade-plugin-job.yaml`
  (`command: ["alembic", "-c", "plugins/expense_reports/alembic.ini", "upgrade",
  "head"]`), applied only on instances with the plugin, **after** the core job.
  Same image, same `SECRET_KEY` requirement, and no `namespace:` field (the same
  `-n` rule as the core job).
- **Same transaction model.** The plugin `env.py` must use
  `transaction_per_migration=True` **and** the bootstrap-commit-before-configure
  sequence, plus the widened `version_num`. To avoid copying fragile code, the
  bootstrap and configure steps of the core `alembic/env.py` are extracted into a
  shared helper exposed through the host contract (`plugin_host.migrations`),
  used by both environments.
- **Core version prerequisite.** The plugin `env.py` refuses to run unless the
  core `alembic_version` contains a revision at or after the plugin's declared
  `REQUIRES_CORE_REVISION` (checked against the core script directory in the same
  image). A wrong job order fails loudly.
- **Table naming.** All plugin tables are prefixed `plugin_expense_reports_*`
  (e.g. `plugin_expense_reports_items`). The core `env.py` gains a generic
  `include_object` rule that ignores tables prefixed `plugin_` and version tables
  prefixed `alembic_version_`. Core autogenerate can then never emit `drop_table`
  for plugin tables (the footgun the Reva block documents), and the core needs no
  knowledge of any plugin name.
- **Foreign keys point one way only.** The core has no FK to plugin tables, and
  core tables get no plugin columns. Plugin tables reference core tables
  (`documents.id`, `users.id`, `projects.id`) with **`ON DELETE SET NULL`**, and
  an item keeps a snapshot (document date, category, confirmed amount, file hash)
  — so deleting a receipt, even while the plugin is inactive, never silently
  removes a line from a settled report. Whether cross-`MetaData` FKs render
  cleanly in `op.create_table` is verified in the substrate spike; the fallback
  is explicit `op.create_foreign_key` or raw DDL.
- **Deactivation.** Removing the plugin from `PLUGIN_MODULES` leaves its tables
  and data in place, untouched and unread; reactivation resumes. Dropping them is
  an explicit operator action (`alembic -c … downgrade base` for the plugin
  environment), never automatic. Retention obligations make silent drops
  unacceptable.
- **Core-migration rule.** Core migrations must not change the type or identity
  of `documents.id`, `users.id` or `projects.id` without checking plugin FKs. This
  is recorded as a contract rule and covered by a test that runs core migrations
  on a database where the plugin schema exists.

### 4. Web frontend and iOS app

**Web — recommendation: a build-time plugin registry, no runtime module loading.**
- `src/frontend/src/plugins/registry.ts` lists the known frontend plugins. Each
  entry declares:
  - a namespace;
  - nav items;
  - routes via `lazy(() => import('./expense_reports/…'))`;
  - an i18n namespace loaded lazily (`plugins/expense_reports/i18n/{de,en}.json`).
- `App.tsx` and `Layout.tsx` iterate the registry and render an entry **only if**
  `features.plugins[namespace]` is present with a compatible version. The core
  components know only the registry interface, never "expense reports".
- The plugin's code is a **lazy chunk**, so it is never downloaded on instances
  without the plugin, because no route to it is ever rendered.
- Runtime remote-module loading (module federation, JS fetched from the backend)
  is rejected: it adds an executable-code channel from the backend into the
  browser, conflicts with the enforcing CSP, and adds build/version skew.
- Rules for the plugin UI: DESIGN.md, dark mode, i18n de+en, TypeScript strict,
  and RTL tests in `tests/frontend/react/plugins/expense_reports/`.

**iOS app:**
- The "Reisekosten" section is registered in `SectionRegistry`, but the shell shows
  it for an account **only if** that instance's health response lists
  `expense_reports` with a compatible contract version.
- Per-diem UI only if the capability list contains `per_diem`.
- The code ships in the single TestFlight build (one binary for all instances)
  and is invisible where the instance does not report the plugin.

### 5. Upload contract — how `trip_ref` reaches the plugin without the core knowing it

- Upload and `PATCH` accept `extensions`: a JSON object ≤ 8 KB, keys must match
  `^[a-z][a-z0-9_]{0,63}$`, each value an object. This is validated structurally
  only; the core never reads the values.
- After the core commit, for each namespace the core fires
  `capture_extensions_apply(namespace, payload, capture_id, document_id,
  owner_user_id, client_id, mode=upload|patch, contract_version)`:
  - **no handler registered** → dropped + logged (the "ignored and logged"
    behaviour of R3, now generic);
  - handler returns `accepted: false` with a reason code → the capture is marked
    `needs_review/assignment_invalid`, and a push goes out through the existing
    path;
  - handler raises or times out → treated like a rejection, logged, never a
    failed upload.
- **The core stores no extension values.** Assignment state lives in plugin
  tables; the capture history shows the plugin's assignment via the plugin's own
  lookups.
- The app writes `extensions.expense_reports.trip_ref` only when the instance
  reports the plugin.

### 6. Effort delta and phasing

- **New Phase 1.5-0 — plugin substrate (core), before 1.5a:**
  - `services/plugin_host/` contract + `HostAPI`;
  - six new hook events + the async-only registration check;
  - the `extensions` field and dispatch;
  - `FeatureFlags.plugins` + mobile health plugins;
  - failed-plugin alerting;
  - the shared migration helper, `plugin_` autogenerate exclusion and the
    plugin-migration job manifest;
  - the frontend plugin registry;
  - import-isolation and contract tests.
  - **Effort: human +2–2.5 wk / CC +3–4 d.**
- **MVP (M1) takes a small part of it:** the `extensions` field in the upload and
  PATCH schema, dropped + logged with no dispatch yet. The app contract is then
  stable from the first build. Human +2–3 d / CC +0.5 d.
- **1.5a/1.5b move into the plugin package** with roughly the same effort as R4,
  plus a little for going through `HostAPI` instead of direct core calls (already
  inside the substrate estimate).
- **Phase 1.5 total (R5): human ~9–11 wk / CC ~3–4 wk**, previously ~7–9 wk /
  ~2.5–3.5 wk. Tax-advisor review calendar time comes on top, as before.

## Receipts downstream [R2]

- **PDF-Split** covers one scan containing several receipts. The app nudges "one
  receipt per scan"; PDF-Split is the safety net.
- **Schicht-A.** Existing categories cover merchant, date and total. VAT
  rate/amount and payment method are not asked for today.
- **Paid receipts vs open payments (decision).** A paid till receipt must not
  create an open `zahlung` obligation. Invoices with a payment term are
  unchanged. Sequence:
  1. build an eval set from **real till receipts** (Phase 0 baseline);
  2. measure how many produce phantom obligations;
  3. only if the eval shows the problem, suppress obligation extraction for
     receipts identified as paid — content evidence ("bar", "EC", "bezahlt",
     payment-terminal slip), never "came from the phone" as a proxy — and
     re-measure against the existing Schicht-A eval for regressions.
- **Paperless** filing follows the instance setting via the async reconciler.

### GoBD on xidra — the paper is kept [R2]

**Decision (user):** on xidra the paper receipt is **kept**. The phone capture
is a **working copy** (faster filing, search, review), not a replacement for the
paper original. Therefore:

- no procedure documentation for replacing paper by scan
  ("ersetzendes Scannen") is required for this feature;
- retention obligations continue to be met by the paper; Paperless and the KB
  hold working copies.

**If replacing scanning is wanted later** (discarding the paper), that is a
separate project and needs:
- a **Verfahrensdokumentation** for the capture process;
- proof of **immutability and completeness** of the digital original: the
  unaltered received bytes, a hash chain or write-once storage, a gapless
  capture log, and retention for the statutory period;
- a check that no pipeline step (including PDF-Split, which archives the combined
  original) alters or loses the original.

Until then, nothing in this design may be described to users as replacing the
paper.

The design already avoids silent loss either way: the outbox deletes only on
`ingested|duplicate`, and the later router staging escalates instead of purging.

## User feedback and push [R2]

### The history is the truth, push is a nudge

**[R4] Local thumbnails (decided):** a small preview per capture is kept **30
days, on the device only**, in the app container, excluded from backup
(`isExcludedFromBackup`), same Data Protection class as the outbox. After that
the history row stays text-only. Thumbnails are never uploaded and never pushed.

The app's history lists every capture per account, with the states `queued` (in
the outbox), `uploading`, `accepted`, `processing`, `processed`,
`needs_review` and `failed`, plus a Paperless-filed marker. It is refreshed:
- on open and pull-to-refresh;
- on a push;
- after a background upload completes.

Push delivery is best-effort; a lost push only means the history is refreshed a
little later.

### Push texts — fixed, content-free

Push text travels through Apple and shows on the lock screen. So:

- **The payload contains no text at all**, only localisation keys resolved by
  the app on the device: `loc-key: "push.capture.processed"` → "Beleg
  verarbeitet"; `"push.capture.needs_review"` → "Prüfung nötig";
  `"push.capture.failed"` → "Beleg konnte nicht verarbeitet werden".
- Custom data: `capture_id` (a random UUID, meaningless outside the instance)
  and an account hint (a random per-pairing id, not the instance name). No
  title, amount, issuer, participant or instance label.
- `apns-push-type: alert`, `thread-id` per account, collapse id per capture +
  state.

### How an instance sends APNs — recommendation: a small push relay

| | APNs key in each backend | **Push relay** |
|---|---|---|
| Key copies | one `.p8` per instance secret (three today) | **one** |
| Blast radius of a compromised instance | the APNs auth key can sign pushes for the whole developer team with arbitrary text | the instance can ask the relay to send **one of a fixed set of template keys** to device tokens registered with it — no arbitrary text |
| Egress to Apple | from every backend namespace | from one place |
| New component | none | a small stateless service + its deployment |
| Availability | push fails with the instance | push fails if the relay is down (history still works) |
| Device tokens | stay in each instance | pass through the relay per send, **not stored** there |

**Decided (R4): the relay, in its own namespace** (egress only to APNs, ingress
only from the instance backends). The deciding factor is key scope: an APNs token
signing key is not per-app by default, and putting it into three
application-facing backends multiplies the highest-value push secret for no
functional gain. Phase 0 checks whether Apple can currently issue a key
restricted to one topic and environment; if it can, the backend option's blast
radius shrinks, but the relay's "template keys only" property still stands.

Relay shape:
- `POST /send {device_token, environment, template_key, capture_id,
  account_hint}`;
- authenticated per instance (its own `rfi`-style credential);
- `template_key` validated against an enum;
- per-instance rate limits;
- APNs HTTP/2 client with token (JWT) auth;
- `410 Unregistered` answers reported back so the instance deletes the stale token;
- no persistence;
- runs in its own namespace with egress only to Apple's APNs hosts and ingress
  only from instance backends;
- a private repository, as for the MCP servers.

### Device-token registration

- The app requests notification permission after the first successful pairing,
  not at launch.
- On every launch and on every token change: `PUT /api/mobile-capture/device`
  with `{apns_token, environment, client_build, locale}`, authenticated by the
  account's ingest credential.
- Stored per credential (`mobile_capture_devices`: `client_id` unique,
  `apns_token`, `environment`, `client_build`, `updated_at`). One device per
  credential, by construction.
- Revoking the credential deletes the row; a relay `410` deletes it; an account
  sign-out in the app calls `DELETE`.

### Trigger — from existing state transitions, event-driven

Emission points are the after-commit transitions that already exist:

| Capture state | Emitted from |
|---|---|
| `processed` | the ingest-complete path in `rag_service` (worker), aggregated over PDF-Split children |
| `failed` | the worker's terminal-failure path |
| `needs_review` | creation of a pending PDF-Split owner-review proposal; creation of a pending xidra review-flow proposal for a `mobile_capture` document |
| (Paperless marker) | the existing terminal `paperless_state` writes — updates the history only, no push |

Mechanism:

1. The emitter checks cheaply whether the document belongs to a capture
   (`mobile_capture_log`, or the split parent), then `XADD`s
   `{capture_id, state}` to the Redis stream `renfield:tasks:mobilepush`. Best
   effort, after commit — it never breaks the caller, like the user-events
   emitters.
2. **One consumer group** read by the API pods (blocking `XREADGROUP` — a
   subscription, not a poll). A stream plus consumer group gives each event to
   exactly one replica. Pub/sub would fan out to every replica and push
   duplicates.
3. The consumer does a **conditional update** on `mobile_capture_log`
   (`notified_state IS DISTINCT FROM :state` for that capture). Only a winning
   update sends, so redelivery after a crash cannot double-push; `processed` is
   sent at most once per capture.
4. The consumer calls the relay; it acknowledges the stream entry only after the
   relay accepted or the device is gone. Relay unavailable → the entry stays
   pending and is retried by the group's pending-entry claim, with a bounded
   age, after which it is dropped (the history remains correct).

### Re-fetch after a push

The app is woken by the notification, or opened from it, and calls
`GET /api/mobile-capture/captures?ids=<capture_id>` with the account's
credential. Everything shown comes from that authenticated response plus the
app's own local capture record. The push itself is never trusted as data.

## Rollout: business instance first [R2]

**Decision (user): xidra first, then household, then the association
instance.** The MVP therefore runs against an **auth-on** instance with real
business data from day one. What that adds or brings forward:

| Topic | Consequence |
|---|---|
| **Real user pairing** | self-service pairing behind login + CSRF is in the MVP (it was Phase 2 in revision 1); non-admin permission tests are MVP tests |
| **Circle tier from the credential** | the MVP tests that a paired user's captures land at that user's tier, invisible to other xidra users below that reach; owner is never taken from the request |
| **xidra review flow** | the review flow's post-ingest hook currently accepts only `source == "folder_ingest"` plus a `.pdf` filename. Its source condition becomes a **configurable allowlist** (default `folder_ingest`, byte-identical elsewhere); xidra's private config adds `mobile_capture`. Tests: a `mobile_capture` PDF creates a proposal when allowed and does not when not. If that flow is replaced by a successor, the opt-in moves with it. |
| **Processed-file handling** | the processed-file rename is folder-share-specific (it renames a file on the watched share); it does **not** apply to `mobile_capture` and stays keyed on `folder_ingest`. Paperless filing, titles and `document_date` do apply. |
| **Business-meal details + GoBD note** | are MVP scope, not a later phase |
| **Network path for a phone on the LAN** | Phase 0 verifies and the MVP configures: a LAN DNS name the iPhone resolves (not mDNS-dependent); a TLS certificate for that name that the app pins, or a private-CA profile; the ingress routing `/api/mobile-capture/*` to the xidra backend; netpol ingress from the ingress controller (usually already there — verify); and **egress from the xidra backend to the push relay**, relay egress to APNs. All of it lives in xidra's private deployment configuration. |
| **Risk to production data** | before the real sphere is used, a **technical dry run on xidra** against a dedicated test KB (tier self, Paperless filing off, review-flow opt-in off), then flip to the real KB/filing/review. Dark flag throughout. |
| **Contract stability** | the first client is on a production instance, so the contract version and the skew path are in place from the first build |

## Security / threat model [R2 edited]

| Threat | Mitigation | Residual |
|---|---|---|
| Stolen / lost phone | device-bound Keychain item, per-device credentials, self-service revocation; APNs token deleted on revoke | outbox readable by whoever unlocks the phone |
| Token extraction | `ThisDeviceOnly`, no backup; push-only + own-status scope; sphere fixed server-side | a queued background task holds the header in OS storage until it runs |
| QR photo / screenshot | QR carries a single-use, short-TTL code + public pin only; web page shows activation live | first scanner within the TTL wins; noticed immediately |
| Hostile Wi-Fi spoofing the instance name | TLS public-key pin checked before any header or byte is sent | none |
| Wrong instance chosen | explicit per-account choice with visible label/colour; later router + audit | a mis-tap is not undoable (same as scanner L1) |
| Hub leak | per-account API clients; no instance ever forwards | none in v1 |
| Malicious file via a leaked token | PDF-only, magic-byte check, size cap, parser in the worker | parser risk as for every ingest path; not internet-reachable |
| Prompt injection via details | never in chunks or extraction prompts; fenced data block on read; strict validation | the agent still reads user text as data (as for every document) |
| Push content leak via Apple / lock screen | payload has only loc-keys, a random capture id and an account hint | Apple learns that *something* happened at that time |
| APNs key compromise | one key, in the relay only; instances can send only template keys | relay compromise = arbitrary pushes to registered devices |
| Location profiling | opt-in per receipt; only the place name stored; coordinates never persisted or uploaded; offline option | with Apple lookup, Apple receives coordinates for that receipt (disclosed) |
| Expired TestFlight build | build-age alert at 75 days, in-app banner | a missed rebuild stalls uploads, loses nothing |
| Auth-off household pairing (Phase 2) | LAN-only | anyone on the LAN can pair (equivalent to their existing access) |
| DoS on push route | self-identifying tokens, `API_RATE_LIMIT_INGEST`, LAN/tunnel-only reachability | none material |

## Configuration [R2 edited]

| Key | Default | Meaning |
|---|---|---|
| `MOBILE_CAPTURE_ENABLED` | `false` | Per instance, dark |
| `MOBILE_CAPTURE_TO_PAPERLESS` | `true` | File mobile captures into Paperless |
| `MOBILE_CAPTURE_KB_NAME` | *(required when enabled)* | **[R7 corrected]** Name prefix of the **per-user** mobile-capture KB, created at pairing and written to the credential row |
| `MOBILE_CAPTURE_PAIRING_TTL_SECONDS` | `600` | Pairing code lifetime — tuned against the real flow in Phase 1 |
| `MOBILE_CAPTURE_PUSH_ENABLED` | `false` | Emit push events |
| `MOBILE_CAPTURE_PUSH_RELAY_URL` / credential ref | *(unset)* | Relay endpoint + this instance's relay credential (secret) |
| `MOBILE_CAPTURE_BUILD_MAX_AGE_DAYS` | `75` | Build-age alert threshold |
| `MOBILE_CAPTURE_PAPERLESS_FIELDS` | `Anlass,Teilnehmer,Ort,Notiz` | Custom field names (existing fields only) |
| review-flow source allowlist | `folder_ingest` | Private config adds `mobile_capture` on xidra |
| `PLUGIN_MODULES` (existing) | *(empty)* | **[R5]** add `plugins.expense_reports.plugin:register` to activate travel expense reports on an instance; absent ⇒ the feature does not exist there. Replaces the R3 `EXPENSE_REPORTS_ENABLED` flag |
| `EXPENSE_REPORTS_PAPERLESS_MIRROR` | `true` | **[R3; R5: plugin-owned setting, read via `HostAPI.settings_namespace`, not in core `utils/config.py`]** Mirror report tag + custom fields (existing fields only) |
| `projects_enabled` (existing) | `false` | **[R3]** Governs the project picker independently |
| `EXPENSE_REPORTS_PER_DIEM_ENABLED` | `false` | **[R4; R5: plugin-owned, renamed into the plugin prefix]** Per-diem / meal-reduction / mileage calculation; also needs a reviewed rate set (hard gate) |
| `EXPENSE_REPORTS_RATES_FILE` | *(unset → calculation off)* | **[R4; R5: plugin-owned]** Path of the instance rate file (private config, schema-validated, fail-closed) |
| `EXPENSE_REPORTS_EXPORT_ZIP_MAX_MB` | *(set from Phase 1.5 measurement)* | **[R4; R5: plugin-owned]** Pre-flight bound for the streamed ZIP export |
| `MOBILE_CAPTURE_THUMBNAIL_DAYS` (app setting) | `30` | **[R4]** Local preview retention on the device |
| Relay: `APNS_KEY_REF`, `APNS_TEAM_ID`, `APNS_KEY_ID`, `APNS_TOPIC` | — | Relay side only, secrets |

## Phases [R2]

- **Phase 0 — measurement spike (gate).**
  - **Capture quality:** real receipts (including faded thermal) captured with a
    VisionKit prototype; `pdfimages`/`pdfinfo`; OCR coverage and Schicht-A
    output through a xidra-equivalent pipeline on a test KB; the phantom-obligation
    baseline on real till receipts.
  - **iOS behaviour:** background `URLSession` on foreign Wi-Fi / cellular-wait /
    force-quit; TLS pin in background challenges vs private-CA profile; Keychain
    `AfterFirstUnlockThisDeviceOnly` from a background relaunch; APNs environment
    for TestFlight; permission prompts.
  - **Network (xidra):** iPhone name resolution on the LAN, certificate, ingress
    path, netpol ingress + egress to the relay, relay egress to APNs.
  - **Desk capture [R6]:**
    - measure real A4 letters and receipts (synthetic or consented) with every
      camera the user actually has: iPhone Desk View via Continuity Camera,
      built-in Desk View if a supported Mac/display is present, an external UVC
      document camera if available, and the iPad rear camera in an overhead
      stand;
    - record pixels on the short edge, OCR coverage through the real pipeline,
      glare, and the stability tolerance / hold time;
    - confirm `deskViewCamera` API and still-photo support;
    - test TestFlight for Mac.
  - **Apple [R4]:** **D-U-N-S number for xidra requested before Phase 0 starts**
    (external lead time: issuance plus organization verification); organization
    enrolment; APNs key scoping options; iOS 18 as the provisional minimum
    confirmed against the prototype.
  - **Paperless (xidra):** custom fields exist or are created; the deferred PATCH
    sets them.
  - **Review flow:** a `mobile_capture` test document through the hook with the
    allowlist.
- **Phase 1 — MVP on xidra** (milestones, each independently verifiable):
  - **M1 backend core:** route, credential route type, pairing, ledger, details
    table + validation, status routes, review-flow allowlist, Paperless
    custom-field mirror, KB search signal.
  - **M2 app v1 without push:** shell + accounts + Keychain, VisionKit + PDF
    assembly, details form + location suggestion, outbox + background uploads +
    home detection, history. fastlane + TestFlight.
  - **M3 xidra dry run → go-live:** test KB, then the real sphere; browser E2E of
    pairing; device E2E of capture → history.
  - **On-device type suggestion — inside M2 [R7-D-W4d]:**
    - **What it covers:**
      - tier detection (A image / B text / C manual);
      - on-device OCR + guided generation;
      - agreement confidence with `SuggestionPolicy` from the Phase 0 eval;
      - `client_hints` upload;
      - send-sheet states;
      - kill switch.
    - **Backend part in M1:** the `client_hints` contract, `capture_client_hints`
      table, deterministic comparator, and the mismatch display.
    - **Gated on the Phase 0 gates:** on-device guarantee, image input, German
      quality, eval set of ≥ 100 per type.
    - **Contingency:** M2 ships with manual chips if gate 1, 2 or 3 fails.
  - **M4 push:** relay, device registration, stream emitter + consumer, app push
    handling + re-fetch; build-age alert.
- **[R3] Project picker inside the MVP.** `document_project_links`, the
  `lookups` route, upload validation → `assignment_invalid`, the app picker and
  cache, and a project-timeline source. It is small, and xidra is first.
- **[R4] Capture write path inside the MVP (M1/M2).** `PATCH …/captures/{id}`
  for details and assignments, with the app edit screen.
- **[R5] Phase 1.5-0 — plugin substrate (core, before 1.5a).**
  - host contract `services/plugin_host/`;
  - new hook events + async-only registration check;
  - `extensions` dispatch;
  - `FeatureFlags.plugins`;
  - failed-plugin alerting;
  - separate plugin Alembic environment + migration job + `plugin_`
    autogenerate exclusion;
  - frontend plugin registry;
  - import-isolation and contract tests.
  - Human 2–2.5 wk / CC 3–4 d.
  - (M1 already ships the `extensions` field, dropped + logged.)
- **[R3 → R4 → R5] Phase 1.5 — travel expense reports, as the `expense_reports`
  plugin (right after M4 and 1.5-0).**
  - **1.5a reports** (can go live on xidra on its own):
    - `expense_reports`, `expense_report_items`, `expense_report_events`;
    - REST + owner status model (submit/withdraw/settle, admin reopen);
    - active trip in the app with offline `client_ref`, times, days, legs;
    - a web page "Reisekosten";
    - confirmed amounts with a Schicht-A suggestion;
    - PDF + CSV + streamed ZIP export;
    - Paperless mirror.
  - **1.5b calculation** (built together, **dark** behind
    `EXPENSE_REPORTS_PER_DIEM_ENABLED`):
    - rate-file schema, loader and validation;
    - pure calculation + golden tests;
    - computed-line snapshot and traceable export;
    - app/web per-diem and mileage UI.
    - **Go-live gate:** tax-advisor review of the specification, golden cases and
      rate set, recorded in the rate file's `review` block.
  - **Effort (R4 re-estimate):**
    - 1.5a: human 4–5 wk / CC 7–9 d;
    - 1.5b: human 3–4 wk / CC 6–8 d;
    - total R4: human ~7–9 wk / CC ~2.5–3.5 wk;
    - **total R5 incl. the plugin substrate: human ~9–11 wk / CC ~3–4 wk**;
    - plus calendar time for the tax-advisor review, which is not effort and
      not under our control.
- **[R6] Phase 1.6 — desk scan on Mac and iPad (after M4; parallel to Phase 1.5).**
  - **In M2 already:** `RenfieldCore` is kept multiplatform — it compiles for
    macOS in CI — so the Mac target is an addition, not a port. Cost: a few days.
  - **Phase 1.6 scope:** macOS app target (shell, pairing deep link + code field,
    Keychain, outbox on macOS); `DeskScanKit` (camera selection, live detection,
    auto-capture on stable edges, perspective correction once, quality gate,
    multi-page page-change detection, PDF assembly, fixture tests); iPad stand
    mode reusing `DeskScanKit`; fastlane macOS lane + TestFlight for Mac.
  - **Effort:**
    - macOS target: human 1.5–2 wk / CC 3–4 d;
    - `DeskScanKit`: human 3–4 wk / CC 6–8 d;
    - iPad stand mode: human 3–5 d / CC 1–2 d;
    - macOS distribution: human 2–3 d / CC ~1 d;
    - **total human ~6–7.5 wk / CC ~2–2.5 wk.**
  - Web capture is **not** part of Phase 1.6 (deferred, R6-1).
  - No backend change: the native path uses the same upload route.
- **Phase 2 — household, then the association instance.** Pair further
  accounts (multi-account is v1 architecture); the auth-off pairing residual on
  the household; per-instance Paperless fields; relay credentials per instance.
- **Phase 3 — on the road.** Self-hosted WireGuard on-demand, split DNS, the
  same names everywhere; retry backoff path becomes dormant.
- **Phase 4 — "Automatisch" via the capture router.** App target "Automatisch"
  → router HTTP intake; device registry; per-(person, instance) credentials on the
  router; L3 + review floor; content-free waiting notice; staging escalation
  instead of purge.
- **Phase 5 — receipt facts.** VAT/payment-method kinds; paid-receipt obligation
  suppression **if** the Phase 0/5 eval shows the problem; regression-checked.
- **Phase 6 — app section Fristen.** Introduces the `session` credential kind.
- **Phase 7 — app section Dokumente/Wissen.**
- **Phase 8 — app section Chat.**

## Risks / accepted residuals [R2 edited]

- **Production-first (xidra).** Bugs in the MVP land on a business instance.
  Mitigated by the dark flag, the test-KB dry run, PDF-only input, and the
  unchanged ingest bridge; reduced, not eliminated.
- **APNs relay** is a new component and a new egress path from the cluster to
  Apple. Push is best-effort; the history stays correct without it.
- **APNs key scope.** Until verified, assume a team-wide key; keep it in the
  relay only.
- **Push metadata to Apple.** Timing of processing events per device is visible
  to Apple, though content is not.
- **TestFlight expiry and tester friction.** A missed rebuild stalls uploads for
  every user; external testers need review per version.
- **Second codebase.** Swift + signing + Apple portal upkeep is ongoing cost;
  justified by push, Keychain, background uploads and the growth path.
- **Background upload limits.** Force-quit cancels queued transfers; arriving home
  with the app closed starts nothing until the app runs or a queued task gets a
  path. Removed by Phase 3.
- **Level-1 instance choice cannot be server-authoritative** (inherited).
- **Apple geocoding** receives coordinates when the user opts in per receipt
  (disclosed; offline alternative available).
- **Router concentration** (Phase 4) inherited from the scanner design.
- **[R3 → R4] Expense reports look authoritative — sharper now that Renfield
  computes money (A2).** A wrong threshold, country rule, meal reduction or
  rounding becomes a wrong amount in an accounting export, repeated across every
  trip. Mitigations:
  - the hard go-live gate (flag **and** a reviewed rate set; the checksum
    re-closes the gate on edit);
  - golden tests;
  - full per-line traceability;
  - a frozen snapshot at submit;
  - the tax-advisor checklist.
  - **Residual:** rate files must be maintained yearly by a person, and a stale
    or incorrectly reviewed set produces confidently wrong numbers.
- **[R4] No four-eyes step (A6).** The owner submits and settles their own report;
  nobody else sees it before it is final. Mitigated only by the append-only event
  log and admin-only reopen. Whether that is acceptable is *[StB 10]*.
- **[R4] Mileage is self-declared.** Kilometres are typed in, with no GPS and no
  route check (the location decision stands). Plausibility is the user's
  responsibility; proof requirements are *[StB 7]*.
- **[R4] ZIP export concentrates receipts.** A single download bundles every
  receipt of a trip, which makes a lost laptop or forwarded file more damaging
  than one document. Mitigations: per-document circle filtering, no personal
  data in filenames, a pre-flight size bound, no server-side copy, rate
  limiting, and an export audit event. The ZIP itself is unencrypted once
  downloaded; that is accepted.
- **[R4] Ingest credential write scope.** The phone can now edit its own captures
  and own open trips. A stolen token can re-assign that person's receipts,
  change their open trips' times, days and legs (which changes computed
  per-diem previews), or edit business-meal details. It cannot submit, settle,
  confirm amounts or export. Revocation per device remains the answer, and every
  app-originated change is logged with `via=app`.
- **[R4] D-U-N-S / organization enrolment** is an external dependency with
  unpredictable lead time; it gates the first TestFlight build (M2).
- **[R5] Contract drift between core and plugin.**
  - **Risk:** a core refactor changes a hook payload or a `HostAPI` behaviour that
    only the plugin relies on, which is invisible in the core suite.
  - **Mitigations:**
    - a semver `HOST_CONTRACT_VERSION` with the plugin refusing a major mismatch;
    - typed payload dataclasses;
    - contract tests in both suites;
    - the import-isolation tests;
    - loud load failure → `ops_alert`.
  - **Residual:** a behaviour change that keeps types intact still needs review
    discipline.
- **[R5] Forgotten or mis-ordered plugin migration.**
  - **Risk:** the plugin is activated but its migration job was not run, or ran
    before the core job.
  - **Mitigations:** the plugin verifies its version table at head before
    mounting anything (no half-mounted plugin), and the plugin `env.py` checks
    the required core revision.
  - **Residual:** an operator must still run a second job on plugin instances;
    it goes into the deploy script.
- **[R5] Plugin FKs constrain core migrations.** Core changes to `documents.id`,
  `users.id` or `projects.id` must account for plugin FKs; covered by a
  migration test with the plugin schema present.
- **[R5] Silent-inert hooks.** The async-only registration check turns the known
  "sync hook silently does nothing" failure into a startup error.
- **[R5] Inert bytes in the shared image.** Plugin code exists on every instance's
  image, unimported. Accepted by decision R5-1 (a), same as ha_glue.
- **[R5] Deactivation leaves data.** Tables and records stay after removing the
  plugin — intended for retention, but it means "turn it off" is not "delete it".
  Deletion is an explicit operator step.
- **[R6] Desk View may not be OCR-grade for receipts.** It is a de-warped crop of
  an ultra-wide camera, possibly limited to video frames. Mitigated by the Phase 0
  gate: if receipts fail, desk scan is positioned for A4 letters only, or needs a
  UVC document camera / the iPad stand. Residual until measured.
- **[R6] Hardware dependency.** A Mac without a built-in Desk View camera relies
  on an iPhone nearby via Continuity Camera (same Apple ID, Wi-Fi + Bluetooth) or
  extra hardware.
- **[R6] Auto-capture misfires** (a hand in the frame, a half-turned page, the same
  page twice) — mitigated by the motion/stability hold, the page-change detection,
  the perceptual-hash duplicate guard, and the quality gate; tuned on recorded
  sequences.
- **[R6] Low-contrast documents** (a white receipt on a white desk) can defeat edge
  detection; the hint to use a dark underlay is measured, not assumed.
- **[R6] A second platform to maintain.** The macOS target has its own TestFlight
  expiry and OS-version drift; one shared core and one fastlane pipeline limit
  the cost.
- **[R6] Camera privacy regression.** A capture session left running would keep
  the camera on; the stop-on-close/background/lock/sleep behaviour is tested
  explicitly.
- **[R3] Ingest credential read scope grows** to the owner's project and trip
  names. It is bounded, own data only, and enumerated; it must not grow further
  (later sections use the session credential).
- **[R3] Owner-only projects.** A colleague cannot assign a receipt to a project
  they do not own until project visibility is widened. That is a product decision,
  not an accident of this design.
- **[R3] Offline trip conflicts.** A trip created offline on the phone and one
  created on the web are two reports. `client_ref` makes retries idempotent, but
  it does not merge different trips; the owner resolves duplicates by hand.

## Decided (2026-09-14) [R4]

Every product question has been decided by the user. What remains is not a
decision: the **tax-advisor checklist** (see "Per diems and mileage allowance")
and the **Phase 0 checks** (see Phases).

**Revision 2 (native app):** native iOS app; TestFlight; Keychain + one-time
pairing code; home-Wi-Fi-only MVP, then WireGuard; manual target in v1; xidra
first; APNs push with content-free texts; paper kept on xidra; structured
business-meal details; `mobile_capture` source with xidra opt-in; paid-receipt
suppression after eval; location optional per receipt, place name only.

| # | Question | Decision |
|---|---|---|
| R2-1 | Apple Developer account holder | **Organization, xidra.** D-U-N-S number required — requested before Phase 0; gates M2 |
| R2-2 | Push relay placement | **Own namespace**; egress only to APNs, ingress only from instance backends |
| R2-3 | Testers | **Internal TestFlight testers** |
| R2-4 | Minimum iOS | **Current major − 1**, provisionally **iOS 18**, fixed after Phase 0 |
| R2-5 | Household pairing with auth off | **Any device on the LAN may pair, no PIN** |
| R2-6 | Local thumbnails | **30 days, device only, excluded from backup** |
| R2-7 | Editing after processing | **Yes, in the app.** Minimal write path on the capture itself (`PATCH …/captures/{id}`: details, project, trip), server-validated, re-mirrored to Paperless |
| A1 | Source of truth for reports | **Renfield tables + Paperless mirror** |
| A2 | Statutory scope in v1 | **Largest scope (deviates from recommendation): per diems (domestic + foreign), meal reductions, mileage allowance** — dark behind `EXPENSE_REPORTS_PER_DIEM_ENABLED` until the tax-advisor review (hard gate) |
| A3 | Project link | **Link row; the receipt stays in its KB** |
| A4 | Project visibility | **Owner-only for now** |
| A5 | Ingest credential read scope | **Ids/names of the owner's own active projects and open trips** |
| A6 | Who settles | **The owner (deviates from recommendation).** Admins can still reopen. **No four-eyes step**, recorded |
| A7 | Placement | **Project picker in the MVP; reports as Phase 1.5 after push** |
| A8 | Export | **PDF + CSV + ZIP of receipt PDFs right away (deviates from recommendation)** — streamed, bounded, circle-filtered per document, template filenames, never stored |
| A9 | Offline trip start | **Allowed** (`client_ref`) |
| A10 | Trip times | **Optional now; required as soon as a trip requests per-diem calculation** (validated at "end trip" and submit, surfaced in the app) |

### What remains (not decisions)

- **Tax-advisor checklist, items 1–12**, which blocks only the per-diem/mileage
  go-live (1.5b), not reports (1.5a) and not the MVP.
- **Phase 0 checks** — capture quality, iOS background/Keychain/TLS behaviour,
  xidra network path, APNs key scope, Paperless custom fields, the review-flow
  hook — plus the D-U-N-S / organization enrolment lead time.
- **Phase 1.5 measurements** — the ZIP size bound; retention of the ingest
  recovery byte copy (the ZIP's byte source).
- **[R5] Substrate spike** — whether plugin tables' FKs to core tables render
  cleanly in the separate Alembic environment (cross-`MetaData` FK); fallback is
  explicit `op.create_foreign_key` or raw DDL.

### Decided from R5

| # | Question | Decision |
|---|---|---|
| R5-1 | What "not present" means for code bytes | **(a):** on instances without the plugin it is not imported, mounted, migrated or shown; its code sits unused in the one shared image, as ha_glue does today. Per-instance images and out-of-repo staging are rejected. |

### Decided from R6

| # | Question | Decision |
|---|---|---|
| R6-1 | Web capture in the PWA (zero install) | **Not now (deferred).** The native Mac app + iPad cover the need. The assessment stays in the doc as a rejected/deferred option (lower Safari image quality, extra WASM bundle, second auth path); revisit only if a no-install use case appears. |
| R6-2 | macOS distribution | **TestFlight for Mac on the xidra organization account**: one pipeline, internal testers, same build-age/expiry alert. Notarized direct distribution is rejected. |

### Decided from R7 (/autoplan gate, 2026-09-14)

| # | Question | Decision |
|---|---|---|
| R7-1 | Five review challenges (per diems in v1, general plugin substrate, usage gate before 1.5/1.6, tunnel earlier, owner self-settle) | **All rejected. R2–R6 stand unchanged.** |
| R7-2 | First instance | **xidra first** (kept) |
| R7-3 | Native visual language | **Apple platform conventions + Renfield colour palette**; no Cormorant in the apps |
| R7-4 | Capture order | **Scan first, on-device type suggestion** with one-tap confirm; optional capability with a manual fallback (see "On-device document type suggestion") |
| R7-5 | Quality gate | **"Trotzdem aufnehmen" after 3 rejections**, marked low quality → review |
| R7-6 | Trip days entry | **Day list with "wie Vortag" copy-forward** |
| R7-7 | Plugin developer experience | **Scaffold, manifest, typed payload dataclasses, FakeHost test harness, `docs/PLUGINS.md` built in Phase 1.5-0** |
| R7-8 | Mobile-capture KB | **Per user**; a duplicate never links across owners (review finding, security) |
| R7-9 (D-W4a) | Eval size | **≥ 100 real captures per type, before M2** (deviates from recommendation). Pre-selection from the first build for types whose Wilson lower bound ≥ 0.95; others rank first |
| R7-10 (D-W4b) | Devices without Apple Intelligence | **Manual choice**, no own classifier in v1 |
| R7-11 (D-W4c) | Suggested fields | **Sent as `client_hints`** (deviates from recommendation). Validated, stored separately, tier-inheriting, a comparison signal only, never facts, never prompt text |
| R7-12 (D-W4d) | Placement | **Inside M2** (deviates from recommendation). M2 gated on the Phase 0 gates; documented manual-chips contingency |
| R7-13 (D-W4e) | Document types | **`rechnung`, `sonstiges` added as hint types**, sent only when the instance advertises them |
