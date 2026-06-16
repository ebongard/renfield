# Chat Artifacts — Sandbox & Sanitization Boundary (Design)

Status: **Design / decision-lock** — DESIGN PASS ONLY. No renderer or backend
is built by this doc. Its single job is to lock the **trust boundary** for
chat-UI roadmap **item 5 (Artifacts / canvas)** before anyone writes a line of
rendering code. See `docs/design/chat-ui-modernization.md` §Tier 2 item 5 and
its design-decisions row.

Scope: how Renfield renders generated **tables / lists / key-value / charts /
SVG / small HTML** inline in a chat turn instead of as a fenced code block —
**safely**, given that the model output is untrusted.

This doc does NOT touch agent routing, circles enforcement, voice/satellite
transport, or the existing card mechanism's behavior. It *reuses* them.

---

## 0. TL;DR — the recommendation

**Two-lane design, typed-first:**

- **Lane A — typed/schema artifacts (the default, ~all v1 traffic).** `table`,
  `list`, `keyvalue`, `chart` are transported as **structured JSON data** and
  rendered by **real React components**. No model HTML, no model SVG, no
  sanitizer, no iframe. This is the same trust model the existing
  `AdaptiveCardRenderer` already ships — every dynamic value flows through
  React's escape boundary. **HTML/SVG injection is impossible because there is
  no HTML/SVG to inject.** This is the right default and covers the shopping
  list / weekly plan / smart-home status / chart use cases item 5 names.

- **Lane B — free-form HTML/SVG (the exception, deferred past v1).** When the
  artifact genuinely cannot be expressed as typed data, render it in a
  **sandboxed iframe** (`sandbox` WITHOUT `allow-scripts` and WITHOUT
  `allow-same-origin`) loaded via `srcdoc`, with a restrictive **CSP `<meta>`
  injected at the top of the frame document**, and the payload **sanitized
  before** it ever reaches the frame (DOMPurify) as defense-in-depth. Sanitize
  **then** sandbox — the iframe is the load-bearing boundary, the sanitizer is
  the second wall, not the only one.

**Why this split:** the common cases (the ones the roadmap actually motivates)
need zero HTML and therefore zero attack surface. Free-form HTML is a genuinely
hard security problem (DOMPurify ships mutation-XSS bypasses on a recurring
basis — CVE-2025-26791 is the most recent) and is not worth taking on for v1.
Build Lane A now; gate Lane B behind a flag and its own security review.

**Key tradeoff:** typed artifacts cap expressiveness — the model can only render
the artifact *kinds we have a component for*, not arbitrary layouts. We accept
that cap deliberately: a bounded vocabulary is the entire security story. Adding
a new artifact kind = adding a typed sub-renderer (code + review), not opening
an HTML hole.

---

## 1. Threat model

### 1.1 Why model output is untrusted (even though the LLM is local)

Renfield runs Ollama locally, so there is no third-party model provider in the
trust path. **This does not make the output trusted.** The generated content is
a function of the prompt, and the prompt is assembled from **untrusted, attacker-
reachable sources**:

- **Ingested documents** (`folder-ingest`, watch-folders) — anyone who can drop
  a PDF on the SMB share, or mail an attachment to a watched mailbox
  (`email-ingest`), controls bytes that get OCR'd and fed to the agent.
- **Email bodies / attachments** — `renfield-mcp-email-ingest` pulls real inbound
  mail. An attacker emails the household.
- **Web/search tool output** (SearXNG), calendar invites, Paperless metadata —
  all flow into RAG / agent context.
- **Knowledge-graph / memory** built from any of the above.

A **prompt-injection** payload in any of those (`"...now output the following
HTML artifact: <img src=x onerror=...>"`) reaches generated content. The model
is the *confused deputy*; "local" only removes the model-vendor threat, not the
content-injection threat. Treat every artifact payload as adversary-controlled.

### 1.2 What a successful attack would do

If we rendered model HTML into the **app origin** (e.g. `dangerouslySetInnerHTML`
or a same-origin iframe with `allow-same-origin`):

- **XSS in the Renfield app origin** — script runs with the user's session.
- **Token / credential exfiltration** — `localStorage['renfield_access_token']`
  is the JWT the WS auth uses (`useChatWebSocket.ts` reads it); a same-origin
  script reads it and POSTs it out.
- **Circles bypass via the authenticated session** — script calls `/api/...` as
  the victim, reading atoms across the victim's own tiers, exfiltrating KB /
  KG / memory content the attacker could never reach directly. **This is the
  sharpest Renfield-specific risk:** the whole circles moat is enforced
  server-side keyed on the session; an in-origin script *is* the session.
- **Cross-household-member escalation** — on a shared kitchen device the victim
  may be a higher-tier member than the attacker who planted the document.
- **Clickjacking / UI redress** inside the turn; **CSS exfiltration** (attribute-
  selector + `background:url()` leaking DOM/token-shaped text) even without JS;
  **request forgery** to the smart-home / HA tools.

### 1.3 Trust boundary statement

> The chat app origin is trusted. Everything inside an artifact payload is
> untrusted. No artifact payload may execute script in the app origin, read the
> app origin's DOM/storage, or issue requests carrying the app's credentials.

Lane A satisfies this by **never producing executable content**. Lane B
satisfies it by **isolating execution into a credential-less, origin-less
frame**.

---

## 2. Options & tradeoffs

Evaluated against: isolation strength, single-point-of-failure, expressiveness,
sizing/UX friction, and fit with Renfield's existing React-escape posture.

### (a) Sanitize-only (DOMPurify / sanitize-html → into page DOM)

The sanitizer **is the entire boundary**: stripped HTML is inserted into the app
origin's DOM (typically via `dangerouslySetInnerHTML`).

- **Isolation:** none beyond the allow-list. One sanitizer bypass = **XSS in the
  app origin** (§1.2 — full session/circles compromise).
- **Failure modes:** mutation-XSS (mXSS) is a *recurring* class, not a settled
  one. DOMPurify's serialize→reparse step creates mutation windows;
  documented namespace-confusion bypasses via **SVG/MathML `foreignObject`**,
  and **CVE-2025-26791** (template-literal regex bug, mXSS, fixed only in
  3.2.4). **CSS exfiltration** is not even in scope for an HTML sanitizer —
  a sanitized-but-styled payload can still leak via attribute selectors +
  `url()`.
- **Verdict:** **rejected as a standalone boundary.** A library with a steady
  stream of CVEs cannot be the *only* wall in front of the circles moat. Keep
  DOMPurify, but only as the *inner* wall behind the iframe (Lane B), never
  alone.

### (b) Sandboxed iframe (`sandbox` without `allow-scripts`, `srcdoc`)

Untrusted HTML goes into an `<iframe srcdoc=...>` with a restrictive `sandbox`.

- **Isolation:** strong. Without `allow-scripts`, **no JS runs at all** — pure
  presentational HTML/SVG/CSS. Without `allow-same-origin`, the frame is a
  unique opaque origin: it cannot touch the parent DOM, parent `localStorage`,
  or send credentialed same-origin requests. **`allow-scripts` +
  `allow-same-origin` together is forbidden — that combination lets the frame
  remove its own `sandbox` attribute and fully negates isolation** (MDN; Google
  Cloud iframe-sandbox guidance).
- **Caveat — srcdoc CSP inheritance:** a `srcdoc` frame **inherits the parent
  page's CSP**. Renfield has **no CSP today** (see §3.4), so inheritance buys
  nothing. We therefore inject a CSP **`<meta>` at the top of the frame
  document**. Simon Willison's 2026 test confirms a `<meta>` CSP in a frame is
  **enforced at parse time and cannot be removed or escaped by script inside the
  frame**, even with `allow-scripts`. So the meta-CSP is a real second wall.
- **Friction:** **sizing** (frame has no natural height — needs a fixed/aspect
  box, or `embed-size` postMessage which itself needs `allow-scripts`), and
  **theming** (frame doesn't inherit Tailwind/dark-mode; must inline tokens).
- **Verdict:** **accepted as the Lane B mechanism.** No-script + no-same-origin +
  inner meta-CSP is the practical sweet spot for *presentational* free-form
  HTML/SVG without taking on a JS-execution surface.

### (c) iframe + separate / null origin + strict CSP (gold standard)

Serve artifact HTML from a **distinct origin** (e.g. a dedicated
`artifacts.renfield.local` / a `blob:` or `data:` origin) with a strict
server-set CSP header, embedded cross-origin.

- **Isolation:** the strongest available — true origin separation, so even if we
  *did* want `allow-scripts`, scripts run in an origin with zero access to app
  cookies/storage/DOM and no ambient credentials.
- **Cost:** a second served origin (ingress route, cert, deploy surface) for a
  self-hosted single-box household product is real operational weight. For
  **`srcdoc` without `allow-same-origin`**, option (b) already yields an opaque
  unique origin, capturing **most** of (c)'s benefit at none of the deploy cost.
- **Verdict:** **deferred / documented as the upgrade path.** If Lane B ever
  needs *interactive* (script-bearing) artifacts — roadmap item 10 generative-UI
  widgets — graduate from (b) to (c) rather than adding `allow-scripts` to a
  `srcdoc` frame. Out of scope for v1.

### (d) Shadow DOM — NOT a security boundary

Shadow DOM gives **style/markup encapsulation**, nothing more. **Scripts in a
shadow tree run in the host origin with full access.** `<script>`/`onerror`/
`javascript:` execute exactly as in the light DOM.

- **Verdict:** **explicitly forbidden as a sandbox.** Called out here so no one
  reaches for it thinking "encapsulated = isolated." It may be used purely for
  *CSS scoping of trusted, React-rendered Lane A content* if ever needed — never
  to contain untrusted markup.

### (e) Typed / schema rendering — render from structured DATA via React

For tables/lists/key-value/charts, transport **structured JSON** and render with
**real React components**. There is **no model HTML at all**.

- **Isolation:** total for the injection class — there is no HTML/SVG string to
  parse, so HTML injection is **not applicable**. Every value is a string/number
  rendered as a React text child → React auto-escapes. This is **exactly** how
  `AdaptiveCardRenderer.tsx` already works today (its docstring: *"No raw HTML
  injection — every dynamic value flows through React's escape boundary"*), and
  how `CitationChip` validates the one attribute it forwards (`CITE_ENTITY_RE`).
- **Residual risks (real, but small and bounded):**
  - **URL-shaped fields** (a `chart` data point or `keyvalue` value that is a
    link) — guard `href` with an allow-list scheme check (`https:`/`http:`/
    `mailto:`; reject `javascript:`/`data:`), exactly as the existing
    link-render path and `CitationChip` do.
  - **`<img src>` in a value** → SSRF/tracking-pixel. v1 typed artifacts do NOT
    render arbitrary images inside cells (text only); images stay the existing
    chat image path with its own URL regex.
  - **Quantity/DoS** — a 10k-row table. Cap rows/series server-side and client-
    side (§5 partial state, §6 limits).
- **Verdict:** **the default and the bulk of v1.** Almost certainly the right
  answer for every artifact item 5 actually motivates.

### SVG specifically

SVG is **not** "just an image." Inline SVG can carry `<script>`, event-handler
attributes (`onload`), `javascript:` in `href`/`xlink:href`, and — most
dangerously — **`<foreignObject>`**, which embeds arbitrary HTML inside the SVG
namespace and is the vehicle for the DOMPurify namespace-confusion mXSS bypasses
above.

- **Lane A `chart`** does NOT accept model SVG. Charts are rendered from typed
  series data by a charting component (or hand-rolled SVG **we** emit from data),
  so the SVG markup is **ours**, never the model's.
- **Free-form SVG** is **Lane B only** (iframe), treated with the same
  no-script/no-same-origin/meta-CSP boundary as HTML, AND sanitized with
  DOMPurify configured to **drop `<foreignObject>`, `<script>`, `<use href>` to
  external refs, event handlers, and external resource references** before it
  reaches the frame.

### Defense in depth (Lane B)

Sanitize **then** sandbox, in this order:
1. **Server-side:** the backend marks the part `artifact` with an explicit
   `kind`; for `html`/`svg` kinds it MAY pre-strip obvious payloads, but the
   server is not the security boundary (it can't fully parse like a browser).
2. **Client sanitize (inner wall):** DOMPurify (pinned ≥ latest patched,
   currently ≥ 3.2.4) with a tight allow-list, `FORBID_TAGS`/`FORBID_ATTR` for
   script/foreignObject/event-handlers, no `data:`/`javascript:` URLs.
3. **Sandbox (outer, load-bearing wall):** inject the sanitized markup as
   `srcdoc` into an iframe with `sandbox` (no `allow-scripts`, no
   `allow-same-origin`) and an inner `<meta http-equiv="Content-Security-Policy">`
   (`default-src 'none'; img-src data:; style-src 'unsafe-inline'; ...`).

A bypass must defeat **both** the sanitizer AND the no-script sandbox AND the
meta-CSP to do anything — and even then it lands in an origin with no credentials
and no parent access. That is the property single-layer sanitize-only lacks.

---

## 3. Backend contract

### 3.1 How an artifact rides the wire — reuse the `card` frame shape

Renfield already has the exact precedent: a **typed JSON payload pushed on its
own WS frame and rendered by a React component, not as HTML**. The artifact
contract mirrors the `card` frame
(`useChatWebSocket.ts::CardMessage`, emitted in
`api/websocket/chat_handler.py` ~L1424 / L1644, rendered by
`AdaptiveCardRenderer`). We add an **`artifact` frame** alongside it rather than
overloading `card` (so the renderers, validators and feature flags are
independent).

```jsonc
// WS frame — emitted after `stream`, before/with `done` (like `card`)
{
  "type": "artifact",
  "artifact": {
    "id": "art_01H...",          // stable id (idempotent re-emit / streaming patch)
    "kind": "table",             // table | list | keyvalue | chart  (v1)
                                 // html | svg                       (Lane B, deferred)
    "title": "Wochenplan",       // optional, i18n-safe plain text
    "data": { /* per-kind typed schema, see 3.3 */ },
    "partial": false             // true while streaming (see §5)
  },
  "replace_text": "Hier ist dein Wochenplan."  // optional, same semantics as card
}
```

- **Persistence:** like `sources` and `agentRole`, the artifact is stored in
  `message_metadata` so a history reload rehydrates it (mirror the
  `metadata.sources` rehydrate path in `types/chat.ts`). It is **not** an atom —
  no `circle_tier`/`atom_id`, no `circle_sql` (same reasoning as message search
  in the roadmap: messages aren't atoms).
- **`done` frame:** unchanged. The artifact does not need a field on `done`; the
  separate `artifact` frame carries it, exactly as `card` does today.

### 3.2 Where the backend produces it

The artifact is produced the same way cards are: by the **sub-intent /
orchestration / hook path** that already builds `card` payloads
(`utils/hooks.py` documents the `{"card": ...}` hook return; `chat_handler`
emits it). A new artifact is a **typed dict the hook/role returns** — the agent
free-text answer is NOT parsed for artifacts in v1 (no fragile "extract a table
from prose" step; the structured-output distrust the roadmap notes for follow-up
chips applies here too). The backend **validates the typed schema** before
emitting (reject unknown `kind`, cap sizes) so a malformed/oversized payload
never reaches the client.

### 3.3 v1 artifact kinds (typed, Lane A only)

| kind | data schema (sketch) | renders as |
|---|---|---|
| `table` | `{ columns: string[], rows: string[][] }` | `<table>` (DESIGN.md table styling), header + zebra rows |
| `list` | `{ ordered?: boolean, items: string[] }` | `<ul>`/`<ol>` |
| `keyvalue` | `{ pairs: {key,value}[] }` | reuse the `FactSet` two-col grid from `AdaptiveCardRenderer` |
| `chart` | `{ chartType: 'bar'\|'line', series: {label, points:{x,y}[]}[] }` | data-driven SVG/`<canvas>` we emit (never model SVG) |

All values are **plain strings/numbers**. No nested HTML, no markup tokens
honored (or markdown via the existing inline parser only, which itself escapes).
URL-valued fields go through the existing scheme allow-list.

### 3.4 CSP — adopt a real one regardless

**Finding: Renfield ships NO `Content-Security-Policy` today.** `nginx.conf`
sets `X-Frame-Options`, `X-Content-Type-Options`, `X-XSS-Protection`, but no CSP
(verified). That is a pre-existing gap and a prerequisite for Lane B (and good
hygiene for Lane A):

- Add a baseline app-origin CSP to `nginx.conf` (`default-src 'self'`,
  `frame-src 'self' data:` to permit the artifact iframe, no `unsafe-eval`,
  tighten `script-src` to the hashed bundles). **Caveat from the recent PWA
  work:** nginx `add_header` does **not** inherit once a `location` sets its own
  — the CSP header must be re-declared in every `location` block that already
  re-declares the security headers (`/`, `=/index.html`), per the documented
  inheritance trap in `nginx.conf`.
- This is itself a small security review item; sequence it **before** Lane B,
  and it benefits Lane A immediately (a baseline `default-src 'self'` is a net
  win even with only typed artifacts).

---

## 4. Frontend component shape

Reuse the AdaptiveCard precedent: one dispatcher + per-kind sub-renderers, all
through React's escape boundary.

```
components/chat/artifacts/
  ArtifactRenderer.tsx        // dispatcher: switch(kind) → sub-renderer; error boundary; fallback
  TableArtifact.tsx           // Lane A
  ListArtifact.tsx            // Lane A
  KeyValueArtifact.tsx        // Lane A (can delegate to the FactSet grid)
  ChartArtifact.tsx           // Lane A — renders OUR svg/canvas from typed series
  SandboxedHtmlArtifact.tsx   // Lane B (deferred) — sanitize→iframe srcdoc→meta-CSP
  artifactSchema.ts           // zod/io-ts runtime validation of the typed `data`
```

- **`ArtifactRenderer`** is wrapped in a **React error boundary**. A sub-renderer
  that throws (bad data shape) does NOT crash the chat thread — it renders the
  fallback (§5). It also **runtime-validates** `data` against the per-kind schema
  before rendering; a validation failure → fallback, never a partial render of
  attacker-shaped data.
- **Placement:** rendered inside the assistant bubble in `ChatMessages.tsx`,
  right where `AdaptiveCardRenderer` is mounted today (the `{message.card && ...}`
  block ~L369). It sits in a clearly **bounded container** (DESIGN.md `.card`-
  family border + a small "generiert" affordance) so it reads as generated
  content, not chrome.
- **`SandboxedHtmlArtifact` (Lane B)** is the *only* component that ever touches
  untrusted markup. It: (1) DOMPurifies the payload, (2) prepends the meta-CSP,
  (3) sets it as `srcdoc` on an `<iframe sandbox="">` (empty sandbox = max
  restriction; explicitly NOT `allow-scripts`/`allow-same-origin`), (4) sizes via
  a bounded/aspect box (no postMessage, since no scripts). It is **flag-gated and
  not built in v1.**

---

## 5. Required interaction states (the four from the roadmap row) + a11y

The roadmap design-decisions table requires: **loading**, **render-fail /
sandbox-blocked fallback**, **partial / streaming**, **a11y** (contrast/zoom +
focus trap). All four, specified:

- **Loading.** While the `artifact` frame's `partial:true` is in flight (or the
  turn is still streaming and the artifact hasn't arrived), show a skeleton sized
  to the artifact's eventual box — reuse the existing typing/skeleton motion
  tokens, honor `prefers-reduced-motion` per DESIGN.md. Never a layout-shifting
  spinner that pushes the composer (DESIGN.md / the follow-up-chips lesson).
- **Render-fail / sandbox-blocked fallback (THE security-critical state).** If
  (a) schema validation fails, (b) a sub-renderer throws (error boundary), (c)
  Lane B sanitization strips everything / the iframe is blocked by CSP, or (d)
  the `kind` is unknown — **fall back to the raw payload shown as a plain,
  escaped, monospace code block** with a quiet "konnte nicht als Artefakt
  dargestellt werden" note. The fallback is **always** the escaped-text path
  (which is the pre-item-5 behavior — a fenced code block), so the *failure mode
  degrades to exactly today's safe rendering*. **A render failure must never
  fall back to injecting the markup unsandboxed.** Fail closed.
- **Partial / streaming.** Two sub-cases: (1) **typed artifacts** are emitted
  whole (a table is built server-side, not token-streamed) → `partial` is for
  large artifacts assembled in chunks; render incrementally only for `table`/
  `list` (append rows), never re-parse. (2) An artifact that never completes
  (turn errored mid-stream) → after the turn's error/`done`, a `partial`
  artifact resolves to the fallback, not a perpetual skeleton.
- **a11y.**
  - **Typed (Lane A)** content is real semantic DOM (`<table>` with
    `<th scope>`, `<ul>`, dl-style key/value) → screen-reader-navigable for free,
    inherits app contrast/zoom/dark-mode tokens.
  - **Contrast/zoom in the sandbox (Lane B):** the iframe does **not** inherit
    Tailwind/dark-mode. The meta-CSP'd `srcdoc` must **inline the DESIGN.md color
    tokens + a `prefers-color-scheme` block + relative (`em`/`%`) sizing** so
    sandboxed content still meets contrast and reflows on zoom. This is a hard
    requirement of the roadmap row and a real Lane-B cost.
  - **Focus trap for interactive artifacts:** v1 typed artifacts are
    non-interactive (display only) → no trap needed; interactive cells (links)
    are normal focusable anchors in document order. If Lane B ever gains
    interactivity (→ option (c), out of scope), focus management lives at the
    iframe boundary and must be specified then.
  - The artifact container gets an `aria-label` ("Generiertes Artefakt: {title}")
    and the bounded region is announced; it lives inside the existing `role="log"`
    `aria-live="polite"` thread, so don't double-announce — the artifact frame
    arrives after the prose, mark it `aria-live="off"` within the live region.

### DESIGN.md fit, dark mode, i18n

- **DESIGN.md fit.** The artifact container uses the `.card` family
  (border + radius `lg` for a prominent container per the radius scale), the
  turquoise `accent` axis for the "generiert" affordance (info semantic), and the
  differentiated radius scale (NOT uniform bubbly — AI-slop blacklist #5). No new
  ad-hoc chip rows. A new `.artifact` / `.artifact-fallback` class, if needed,
  must use only DESIGN.md tokens with explicit `dark:` variants and respect
  `prefers-reduced-motion` (the §"Component classes" contract).
- **Dark mode.** Lane A inherits `dark:` variants like every other component.
  Lane B must inline a dark palette in the `srcdoc` (it can't inherit) — see a11y
  above.
- **i18n.** All chrome strings (`title` fallbacks, the "generiert" label, the
  fallback note, error copy, `aria-label`s) via `useTranslation()` in both
  `de.json` + `en.json`. Note the artifact **`data` values are model-generated
  content**, not chrome — they are not translated (they're already in the user's
  conversation language) but they ARE escaped.

---

## 6. Phased build plan, out-of-scope, test strategy

### v1 (what to build)

- **Lane A only:** `table`, `list`, `keyvalue`, `chart` typed artifacts.
- Backend: `artifact` WS frame + `message_metadata` persistence + server-side
  schema validation + size caps; produced from the existing hook/sub-intent/
  orchestration card path (no prose-parsing).
- Frontend: `ArtifactRenderer` + the four typed sub-renderers + error boundary +
  runtime schema validation + the fallback-to-escaped-code-block path.
- Infra: add a baseline app-origin **CSP** to `nginx.conf` (good hygiene; also a
  Lane B prerequisite).
- Flag: gate behind a feature flag (mirror `wissen_workspace_enabled` /
  `role_surfacing_enabled` in `/api/config/features`) so it ships dark.

### Deferred (explicitly NOT v1)

- **Lane B (free-form `html` / `svg` in a sandboxed iframe).** Build only when a
  real use case needs it, and only after its **own security review**
  (sanitizer config + sandbox attrs + meta-CSP + dark/contrast inlining + the
  XSS negative-test suite below). Do NOT ship Lane B "while we're in here."
- **Interactive / script-bearing artifacts** (roadmap item 10 generative-UI
  widgets) → that's option (c) separate/null origin, a separate design.
- **Agent free-text → artifact extraction** (parsing a table out of prose).
- **postMessage auto-resize** (needs `allow-scripts`; defer with Lane B).

### Out of scope

Voice/satellite artifact rendering (artifacts are a web-chat surface; voice
turns speak the prose, the artifact is web-only — like the existing card),
artifact editing/export, cross-conversation artifact reuse.

### Test strategy

- **Unit (Lane A):** each sub-renderer renders a known typed payload; runtime
  schema validator rejects malformed/oversized/unknown-`kind` payloads → fallback.
- **Escape / injection negative tests (Lane A):** payloads with HTML/markup in
  every string field (`<img src=x onerror=alert(1)>`, `</td><script>`,
  `javascript:` and `data:` in URL-valued fields) MUST render as **inert escaped
  text**, never execute, never produce a live element. Assert no `<script>` in
  the rendered DOM and that `href`s with disallowed schemes are dropped/neutered.
- **Fallback tests:** a throwing sub-renderer / failed validation renders the
  escaped code-block fallback and does NOT crash the thread; the fallback markup
  is escaped (no injection through the fallback).
- **CSP test:** assert the new `nginx.conf` CSP header is present on `/`,
  `/index.html`, and the SPA fallback (and that it survived the add_header
  inheritance trap — re-declared per location).
- **Lane B negative suite (when Lane B is built, gate merge on it):** known
  DOMPurify mXSS/`foreignObject`/namespace-confusion bypass payloads must NOT
  escape the iframe; assert the iframe has NO `allow-scripts`/`allow-same-origin`;
  assert the meta-CSP is present and `default-src 'none'`; assert a script in the
  payload cannot read parent `localStorage` (the token) or call `/api`.
- **a11y:** Lane A sub-renderers pass axe (table headers, list semantics, label);
  contrast in dark mode; zoom reflow.
- Frontend tests live in `tests/frontend/react/`; per the project's flaky-suite
  note, judge by isolated-file runs.

---

## 7. Open questions (need a decision before scheduling)

1. **Is Lane B (free-form HTML/SVG) ever actually needed?** If every motivated
   use case (shopping list, weekly plan, chart, smart-home status) is expressible
   as a typed kind — and it appears to be — we may **never** build Lane B and
   should say so, deleting the iframe complexity from the roadmap entirely.
   **Recommendation: ship Lane A, treat Lane B as YAGNI until a concrete case
   forces it.** Confirm?

2. **Charting dependency.** `chart` via a new lib (Recharts/visx — bundle cost,
   adds a dependency) vs. hand-rolled SVG from typed series (no dep, more code,
   limited chart types). Given the PWA bundle-size discipline and that v1 likely
   needs only bar/line, **lean hand-rolled**. Acceptable?

3. **Do we adopt the baseline CSP now, independent of artifacts?** It's a
   pre-existing gap and a security win on its own. Recommend yes, as its own
   small PR ahead of item 5. Approve splitting it out?

4. **Where does artifact production live** — only the structured hook/sub-intent/
   orchestration path (my recommendation, no prose-parsing), or do we also let
   the `conversation`/writing role emit artifacts? The latter needs a reliable
   structured-output contract from small local models, which the codebase
   distrusts elsewhere. Constrain v1 to hook-produced artifacts?

5. **Feature-flag granularity:** one `artifacts_enabled` flag, or per-lane
   (`artifacts_typed_enabled` always-on-able vs. `artifacts_html_sandbox_enabled`
   defaulting off behind security review)? Recommend per-lane so Lane A can ship
   without ever enabling the HTML path.

---

## References

- Roadmap: `docs/design/chat-ui-modernization.md` §Tier 2 item 5 + decisions table.
- Existing precedent: `src/frontend/src/components/AdaptiveCardRenderer.tsx`
  (typed JSON → React, no HTML injection), `components/wissensbasis/CitationChip.tsx`
  (attribute allow-list regex), `pages/ChatPage/ChatMessages.tsx` (render path).
- Wire: `pages/ChatPage/hooks/useChatWebSocket.ts` (`CardMessage` frame),
  `api/websocket/chat_handler.py` (card emit ~L1424/L1644), `utils/hooks.py`
  (`{"card": ...}` hook contract), `types/chat.ts` (metadata rehydrate).
- Security posture: `src/frontend/nginx.conf` (security headers; **no CSP**).
- DESIGN.md: tokens, radius scale, `.card` family, AI-slop blacklist, a11y/contrast.
- External (security-critical, 2025/2026):
  - DOMPurify mXSS — CVE-2025-26791 (template-literal regex, < 3.2.4); historical
    SVG/MathML `foreignObject` namespace-confusion bypasses (PortSwigger / Securitum).
  - srcdoc meta-CSP cannot be escaped by in-frame JS — Simon Willison, 2026-04
    (test-csp-iframe-escape).
  - `allow-scripts` + `allow-same-origin` negates the sandbox — MDN `<iframe>`,
    Google Cloud iframe-sandbox tutorial.
  - Open WebUI `IFRAME_CSP` env var (injects meta-CSP into srcdoc artifact frames)
    — prior-art confirmation of the Lane B pattern.

---

## 8. Eng-review decisions (locked 2026-06-16, `/plan-eng-review`)

These supersede/resolve the open questions in §7 and pin three architecture
choices. The §1-§6 prose above is the exploration; this section is the contract.

### Locked architecture decisions
1. **Streaming KEPT, protocol now specified.** Artifacts MAY stream via multiple
   `artifact` frames sharing one `id`. Protocol: first frame may carry `partial:true`;
   subsequent same-`id` frames **append** for `table` (rows) / `list` (items) — never
   re-parse, never replace; a frame with `partial:false` (or the turn's `done`/error)
   **finalizes**. A `partial` artifact that never finalizes (turn errored mid-stream)
   resolves to the **fallback** (§5), not a perpetual skeleton. Frames are applied in
   arrival order; an out-of-order/duplicate `id` patch is idempotent (append is keyed
   so re-delivery doesn't double-append). Client should **coalesce** rapid same-`id`
   patches (one render per animation frame) to avoid render thrash.
2. **`message_metadata.artifacts[]` — an ARRAY, keyed by `id`.** One assistant turn
   MAY carry multiple artifacts (e.g. a `table` + a `chart`); the frontend renders
   each in arrival order. Streaming patches (decision 1) match by `id` within the
   array. Rehydrate maps `metadata.artifacts[]` in `historyToUiMessage`. (NOT a
   singular field — avoids a later singular→array metadata migration.)
3. **Schema validation split by concern (DRY).** The backend validates ONLY the
   kind-allowlist + size/row/series/point caps (the DoS gate; cheap, no full shape).
   The frontend `artifactSchema.ts` (zod) is the **authoritative shape validator** —
   it is what renders, and a shape failure → fallback. This is an intentional
   separation (backend = caps/DoS, frontend = shape/render), not duplication; no
   shared-schema codegen.

### Resolved open questions (§7)
- **Q1 Lane B:** deferred / YAGNI — kept in the doc as the documented upgrade path,
  built only on a concrete need + its own security review. Not in v1.
- **Q2 chart:** **build all four kinds in v1**, `chart` **hand-rolled** bar/line SVG
  from typed series (no charting dependency — PWA bundle discipline).
- **Q3 CSP:** ship the baseline app-origin CSP as its **own small PR BEFORE** the
  artifacts PR (closes a pre-existing gap; re-declared per-`location` for the nginx
  `add_header` inheritance trap; verify header on `/`, `/index.html`, SPA fallback).
- **Q4 production path:** artifacts are produced **only** from the hook/sub-intent/
  orchestration path (typed dict return), **never** by parsing the agent free-text
  answer and **not** from the `conversation` role in v1.
- **Q5 flags:** **per-lane** — `artifacts_typed_enabled` (Lane A, shippable dark→on)
  and `artifacts_html_sandbox_enabled` (Lane B, defaults off, gated on security review).

### Test additions (mandatory, fold into §6)
The §6 strategy is sound for the renderer/escape/fallback/CSP/a11y core. Add, from
the decisions above:
- **Streaming (decision 1):** multi-frame same-`id` append for table/list; idempotent
  re-delivery (no double-append); out-of-order patch handling; a `partial` that never
  finalizes → fallback (not perpetual skeleton).
- **Multiple artifacts/turn (decision 2):** a turn emitting 2 artifacts renders both
  in order; `metadata.artifacts[]` rehydrates on history reload (round-trip).
- **Backend caps (decision 3):** backend rejects oversized (10k-row table, huge
  series, too-many points) and unknown `kind` BEFORE emit; frontend zod rejects the
  malformed shape → fallback.
- **chart numeric validation:** non-numeric / `NaN` / `Infinity` `x,y` are
  rejected/coerced (a huge value must not blow the SVG viewBox = DoS).
- **Persistence round-trip:** an artifact survives history reload via
  `historyToUiMessage` mapping `metadata.artifacts[]`.

### Sequencing (build order)
PR 1 — baseline CSP (Q3, standalone). PR 2 — Lane A artifacts (all four kinds, the
`artifact` array frame + caps + zod + fallback + `artifacts_typed_enabled` flag).
Lane B is a later, separate, security-reviewed PR if ever needed.

### NOT in scope (v1)
- Lane B free-form HTML/SVG sandboxed iframe (deferred — own security review).
- Interactive/script-bearing artifacts (roadmap item 10 → option (c) separate origin).
- Agent free-text → artifact extraction (prose parsing).
- postMessage auto-resize (needs `allow-scripts`; with Lane B).
- Voice/satellite artifact rendering (web-chat surface only, like cards); artifact
  editing/export; cross-conversation artifact reuse.

### What already exists (reused, not rebuilt)
- `AdaptiveCardRenderer.tsx` — typed-JSON→React no-HTML-injection pattern = Lane A's model.
- `FactSet` grid (in AdaptiveCardRenderer) — reuse for `keyvalue`; check for an
  existing table renderer before building `TableArtifact` (reuse if present).
- `card` WS frame + `chat_handler` emit + `utils/hooks.py` `{"card":...}` contract —
  the `artifact` frame mirrors it.
- `message_metadata` (sources/agentRole) persistence + `historyToUiMessage` rehydrate.
- `CitationChip` `CITE_ENTITY_RE` — URL-scheme allow-list precedent.

### Failure modes (each must fail closed)
- Schema/shape invalid → fallback to escaped code block (never raw markup). **Covered.**
- Sub-renderer throws → error boundary → fallback; thread does not crash. **Covered.**
- Oversized payload → backend cap rejects before emit; client cap as backstop. **Add backend test.**
- `partial` never finalizes → fallback after `done`/error. **Add test (decision 1).**
- chart `NaN`/`Infinity` → viewBox DoS. **Add numeric-validation test.**
None should be silent: each renders the visible "konnte nicht als Artefakt dargestellt
werden" fallback.

### Parallelization
PR 1 (CSP, nginx-only) and the PR 2 backend lane (`artifact` frame + caps + persistence)
are independent of the PR 2 frontend lane (renderer + sub-renderers + zod) **except**
the wire contract (frame shape + `artifacts[]`). Lock the contract first, then:
`Lane A: backend frame+caps+persist` ‖ `Lane B: frontend renderer+sub-renderers+zod`
in parallel, integration-test on join. CSP PR is fully independent (ship anytime).

### Implementation Tasks
- [ ] **T1 (P1)** — infra/nginx — baseline app-origin CSP, its own PR, re-declared per
  `location`. Verify: CSP header on `/`, `/index.html`, SPA fallback.
- [ ] **T2 (P1)** — backend — `artifact` WS frame + `message_metadata.artifacts[]`
  persistence/rehydrate + kind-allowlist & size/row/series/point caps; produced from the
  hook/sub-intent path only. Verify: backend cap/kind reject test + persistence round-trip.
- [ ] **T2b (P1)** — backend — streaming patch protocol (same-`id` append, idempotent,
  finalize on `partial:false`/`done`). Verify: streaming + never-finalize tests.
- [ ] **T3 (P1)** — frontend — `ArtifactRenderer` (error boundary + fallback) + `artifactSchema.ts`
  zod (authoritative shape) + Table/List/KeyValue/Chart sub-renderers (chart = hand-rolled
  SVG) + `artifacts_typed_enabled` flag. Verify: per-kind render, escape/injection negative
  suite, URL-scheme drop, fallback, chart numeric validation, a11y (axe).
- [ ] **T4 (P2)** — frontend — coalesce rapid same-`id` streaming patches (one render/frame).

## 9. Design-review decisions (locked 2026-06-16, `/plan-design-review`)

Initial 8/10 → 9/10. Calibrated against `DESIGN.md`. Two genuine choices + four
obvious fixes folded into §3-§5 above.

- **Valid-but-empty artifacts → warm per-kind empty state.** A well-formed artifact
  with no data (0-row `table`, 0-item `list`, 0-point `chart`) renders the artifact
  frame + title + a quiet, kind-specific message ("Keine Zeilen" / "Keine Einträge" /
  "Keine Datenpunkte") in the info/accent token — NOT an empty grid shell, NOT the
  error fallback (empty ≠ render-failure; the artifact produced fine, it just had no
  data — e.g. an empty shopping list is meaningful). Distinct state from schema-invalid
  (→ fallback). i18n de+en.
- **Responsive (wide artifact in the chat column) → horizontal scroll, bounded.**
  `TableArtifact` wraps in an `overflow-x:auto` container bounded to the bubble width:
  preserves all columns, touch-scrollable on mobile, shows a scroll affordance, never
  widens the chat layout. `ChartArtifact` scales to container width via SVG `viewBox`
  (never overflows). ~375px is the design target; applies at all viewports. (Rejected:
  truncate-columns hides data; per-kind mobile reflow is heavy + changes identity.)
- **"generiert" affordance.** A small DM Sans text label ("generiert") + a subtle
  inline icon in the accent/info token — explicitly NOT icon-in-a-colored-circle
  (AI-slop #3), not centered, not decorative; quiet metadata on the bounded `.card`.
- **Design-system tokens (Pass 5).** `table`/`chart` numerics use DM Sans
  `tabular-nums` (DESIGN.md §45/§72); Cormorant never on small artifact text (≥24px
  display only); container = `.card` family at radius `lg`; every state has explicit
  `dark:` variants; the loading skeleton respects `prefers-reduced-motion`.
- **Chart visual spec (Pass 7).** Hand-rolled bar/line SVG: axes labelled from the
  series data; gridlines minimal/optional; legend only when >1 series; values in
  tabular-nums; the SVG carries `role="img"` + a `<title>`/`aria-label` summarizing it;
  colors from the accent/data tokens (no new hues).
- **Multi-series chart colorblind a11y (Pass 6, re-run).** A multi-series chart must
  NOT distinguish series by color alone (WCAG 1.4.1). Differentiate by color PLUS a
  non-color channel: **direct end-of-line labels** for line charts, **pattern/label**
  for bars; the legend stays. Must remain readable in grayscale / for CVD users. The
  hand-rolled SVG owns its rendering, so this is a cheap addition, not a dependency.
- **Design NOT in scope:** mockups (token-bound in-bubble micro-components, AdaptiveCard
  precedent); interactive/editable artifacts; per-kind mobile reflow (chose scroll);
  Lane B sandbox visual theming (deferred with Lane B).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 3 arch issues (all resolved), 7 test gaps added, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 2 | CLEAR | 8→9 (run 1: empty-state, responsive + 4 fixes); 9 (re-run: +multi-series chart colorblind a11y, WCAG 1.4.1) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **SCOPE:** all four kinds in v1 (chart hand-rolled, no dep); Lane B deferred (YAGNI).
- **ARCH (locked §8):** streaming kept + protocol specified · `message_metadata.artifacts[]` array · schema split by concern (backend caps / frontend zod authoritative).
- **DESIGN (locked §9):** warm per-kind empty state · wide-artifact horizontal-scroll (bounded) · "generiert" affordance (no icon-in-circle) · tabular-nums + `.card`/`lg` tokens · chart a11y/visual spec.
- **SEQUENCING:** baseline CSP as its own PR first, then Lane A artifacts.
- **UNRESOLVED:** none.
- **VERDICT:** ENG + DESIGN CLEARED — design locked, ready to implement (PR 1 CSP → PR 2 Lane A artifacts). Outside voice not run (design already had an independent design-agent pass; offer stands if wanted).
