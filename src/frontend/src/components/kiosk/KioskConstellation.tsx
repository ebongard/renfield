// KioskConstellation — the FULLSCREEN, cinematic "stunning" command-center for
// a wall display. Deliberately breaks DESIGN.md (glow, bloom, radial gradients)
// per TODOS.md line 315 — the marketing/kiosk aesthetic that must stay OUT of
// the restrained admin `AgentConstellation`. This is a separate component so
// that boundary holds.
//
// It renders full-bleed on a dark cosmic field: a glowing core that reacts to
// real household voice activity (idle / listening / processing / speaking, from
// the satellites' own state), concentric rings of roles / tools / rooms / peers,
// and an ambient telemetry corner. Content-free by design (counts, role names,
// room names only) so it is safe on a shared screen.
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Music2, Radio as RadioIcon, Film, ListMusic } from 'lucide-react';

import { iconForCode } from '../chat/artifacts/WeatherArtifact';
import type { NodeHealth, SatelliteState } from './types';
import type { CoreState, KioskState } from './useKioskModel';
import type { KioskNowPlaying } from '../../api/resources/kiosk';
import KioskClock from './KioskClock';
import { PEER_ARC, PEER_LABEL_FONT, PEER_RING_DASH, layoutPeerArc } from './peerArc';

const VW = 1920;
const VH = 1080;
const CX = VW / 2;
const CY = VH / 2;
const R_ROLES = 210;
const R_TOOLS = 340;
const R_ROOMS = 452;
const R_PEERS = PEER_ARC.radius;
const R_CORE = 96;

const C = {
  active: '#00e4b8', // turquoise (tool-health accent, a different axis than LED state)
  healthy: '#00e4b8',
  degraded: '#f7a4ae',
  down: '#e63e54',
  unknown: '#5b6472',
  crimson: '#e63e54',
  amber: '#f2a63d', // warm ambient wash
  cream: '#f0e6d3',
  dim: '#7d8794',
  off: '#4a5361', // an offline satellite — its LED ring is dark
} as const;

// Satellite LED ring colours (src/satellite/renfield_satellite/hardware/led.py):
// the kiosk colour-codes voice STATUS to match the LEDs the household actually
// sees on the physical devices — idle=blue, listening=green, processing=yellow,
// speaking=cyan, error=red.
const LED: Record<SatelliteState, string> = {
  idle: '#2f6bff',
  listening: '#25de5f',
  processing: '#f4cd2a',
  speaking: '#22e0e0',
  error: '#ff4d4d',
};

const CORE_COLOR: Record<CoreState, string> = {
  idle: LED.idle,
  listening: LED.listening,
  processing: LED.processing,
  speaking: LED.speaking,
  busy: LED.error, // fleet error / backend unreachable
};

/** A very dark tint of the core colour (colour × amount, over black) — used to
 *  tint the whole ambient field to the CORE's LED colour so the background
 *  tracks the state (blue at idle, green while listening, …) instead of a
 *  fixed hue. `amount` ~0.06–0.16 keeps it near-black so the rings stay legible. */
function tintDark(hex: string, amount: number): string {
  const n = parseInt(hex.replace('#', ''), 16);
  const r = Math.round(((n >> 16) & 255) * amount);
  const g = Math.round(((n >> 8) & 255) * amount);
  const b = Math.round((n & 255) * amount);
  return `rgb(${r}, ${g}, ${b})`;
}

function polar(r: number, deg: number): [number, number] {
  const a = ((deg - 90) * Math.PI) / 180;
  return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
}
/** Like polar but around the origin (0,0) — for groups translated to the core. */
function ray(r: number, deg: number): [number, number] {
  const a = ((deg - 90) * Math.PI) / 180;
  return [r * Math.cos(a), r * Math.sin(a)];
}
function healthColor(h: NodeHealth): string {
  if (h === 'healthy') return C.healthy;
  if (h === 'degraded') return C.degraded;
  if (h === 'down') return C.down;
  if (h === 'off') return C.off; // disabled-by-config: muted, not the red of 'down'
  return C.unknown;
}
function anchorFor(x: number): 'start' | 'middle' | 'end' {
  const dx = x - CX;
  if (dx > 4) return 'start';
  if (dx < -4) return 'end';
  return 'middle';
}

// Deterministic star field (no Math.random — SSR/rebuild-stable). `d` is a
// per-star twinkle delay so the field shimmers out of phase, not in lockstep.
const STARS = Array.from({ length: 140 }, (_, i) => {
  const a = (i * 2654435761) % 2147483647;
  const b = (i * 40503 + 12345) % 2147483647;
  return {
    x: (a % VW),
    y: (b % VH),
    r: 0.5 + ((a >> 8) % 10) / 8,
    o: 0.06 + ((b >> 6) % 30) / 120,
    d: ((a >> 4) % 60) / 10, // 0–6s
  };
});

interface Props {
  kiosk: KioskState;
}

/** How long a subsystem node stays lit after a turn_activity names it. The
 *  fade is driven by a UI-only render tick (NOT a network poll). */
const PULSE_WINDOW_MS = 6_000;

export default function KioskConstellation({ kiosk }: Props) {
  const { t } = useTranslation();
  const { model, core, activeRoom, activeRoleLabel, telemetry, weather, nowPlaying, subsystemPulses } = kiosk;
  const { roles, tools, rooms, peers = [] } = model;

  const reduced = usePrefersReducedMotion();
  // Fast render tick so an active-subsystem pulse visibly fades on its own,
  // with no data fetch. Content-free — it just advances wall time.
  const pulseNow = usePulseTick(1_000);
  /** 0 (idle) → 1 (just fired): the live pulse intensity for a subsystem id. */
  const pulseFor = (id: string): number => {
    const at = subsystemPulses[id];
    if (!at) return 0;
    const age = pulseNow - at;
    if (age <= 0) return 1;
    if (age >= PULSE_WINDOW_MS) return 0;
    return 1 - age / PULSE_WINDOW_MS;
  };

  const at = (i: number, n: number, offset = 0) => (n > 0 ? (360 / n) * i + offset : 0);
  const toolOffset = tools.length > 0 ? 180 / tools.length : 0;
  // Positions, clipped texts and label placement of the peers arc (peerArc.ts).
  const peerPlaces = layoutPeerArc(peers);
  // The third state only exists while the backend runs the signed check; the
  // tooltip is set exactly then (useKioskModel).
  const peersChecked = peers.some((peer) => peer.hint !== undefined);
  const coreColor = CORE_COLOR[core];

  // Radiating "voice" spikes when listening/speaking.
  const voiceActive = core === 'listening' || core === 'speaking';
  const spikes = useMemo(
    () => Array.from({ length: 48 }, (_, i) => (360 / 48) * i),
    [],
  );

  return (
    <div
      className="relative w-full h-full overflow-hidden select-none"
      // Base as a CSS gradient on the DIV (not the SVG) so it fills ANY aspect
      // ratio — the wall TVs are landscape, the room tablets portrait, where the
      // `meet` SVG letterboxes; those bands must stay coloured, not black. The
      // tint tracks the CORE's LED colour so the whole field matches the state.
      style={{
        background: `radial-gradient(ellipse 82% 82% at 50% 46%, ${tintDark(coreColor, 0.16)} 0%, ${tintDark(coreColor, 0.06)} 52%, #050406 100%)`,
      }}
    >
      {/* meet (not slice): the whole constellation is always visible and never
          cropped — portrait tablets show all rings, just scaled to fit width. */}
      <svg viewBox={`0 0 ${VW} ${VH}`} className="w-full h-full" preserveAspectRatio="xMidYMid meet" role="img"
        aria-label={t('kiosk.srSummary', {
          defaultValue: 'Renfield command center. {{present}} people present, {{online}} of {{total}} satellites online.',
          present: telemetry.peoplePresent, online: telemetry.satellitesOnline, total: telemetry.satellitesTotal,
        })}>
        <defs>
          {/* The big wash across the whole field — what stops the background
              reading as flat black. Tinted to the CORE's LED colour so the
              field matches the state (blue idle, green listening, …). */}
          <radialGradient id="k-halo" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={coreColor} stopOpacity={0.32} />
            <stop offset="32%" stopColor={coreColor} stopOpacity={0.15} />
            <stop offset="64%" stopColor={coreColor} stopOpacity={0.04} />
            <stop offset="100%" stopColor={coreColor} stopOpacity={0} />
          </radialGradient>
          {/* Translucent hologram sphere: the centre lets the warm halo glow
              through, the body luminesces, and a bright thin rim reads as the
              globe's edge — a light-globe, not a solid disc. */}
          <radialGradient id="k-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={coreColor} stopOpacity={0.10} />
            <stop offset="55%" stopColor={coreColor} stopOpacity={0.16} />
            <stop offset="83%" stopColor={coreColor} stopOpacity={0.42} />
            <stop offset="94%" stopColor="#ffffff" stopOpacity={0.72} />
            <stop offset="100%" stopColor={coreColor} stopOpacity={0} />
          </radialGradient>
          <radialGradient id="k-bloom" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={coreColor} stopOpacity={0.5} />
            <stop offset="100%" stopColor={coreColor} stopOpacity={0} />
          </radialGradient>
          {/* Drifting nebula clouds — core-coloured so the field reads in the
              state's colour, not black. Three for depth (positions/drift/opacity
              differ). */}
          <radialGradient id="k-neb1" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={coreColor} stopOpacity={0.24} />
            <stop offset="100%" stopColor={coreColor} stopOpacity={0} />
          </radialGradient>
          <radialGradient id="k-neb2" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={coreColor} stopOpacity={0.20} />
            <stop offset="100%" stopColor={coreColor} stopOpacity={0} />
          </radialGradient>
          <radialGradient id="k-neb3" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={coreColor} stopOpacity={0.14} />
            <stop offset="100%" stopColor={coreColor} stopOpacity={0} />
          </radialGradient>
          {/* Slow radar sweep: a one-sided soft glow rotated around the core,
              in the core's LED colour so it belongs to the same light. */}
          <linearGradient id="k-sweep" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor={coreColor} stopOpacity={0} />
            <stop offset="86%" stopColor={coreColor} stopOpacity={0} />
            <stop offset="100%" stopColor={coreColor} stopOpacity={0.10} />
          </linearGradient>
          <filter id="k-glow" x="-120%" y="-120%" width="340%" height="340%">
            <feGaussianBlur stdDeviation="6" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <filter id="k-soft" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="2.4" />
          </filter>
        </defs>

        <style>{`
          .k-breathe { transform-box: fill-box; transform-origin: center; animation: kBreathe 6s ease-in-out infinite; }
          .k-bloom  { transform-box: fill-box; transform-origin: center; animation: kBloom 4.5s ease-in-out infinite; }
          .k-occ    { animation: kPulse 3s ease-in-out infinite; }
          .k-active-edge { stroke-dasharray: 7 10; animation: kDash 1s linear infinite; }
          .k-spike  { transform-box: fill-box; transform-origin: center; animation: kSpin 22s linear infinite; }
          .k-spike2 { transform-box: fill-box; transform-origin: center; animation: kSpin 30s linear infinite reverse; }
          .k-sweep  { transform-box: fill-box; transform-origin: center; animation: kSpin 60s linear infinite; }
          .k-halo   { transform-box: fill-box; transform-origin: center; animation: kHalo 11s ease-in-out infinite; }
          .k-globe  { transform-box: fill-box; transform-origin: center; animation: kSpin 44s linear infinite; }
          .k-globe2 { transform-box: fill-box; transform-origin: center; animation: kSpin 64s linear infinite reverse; }
          .k-twinkle { animation: kTwinkle 5s ease-in-out infinite; }
          .k-neb-a { transform-box: fill-box; transform-origin: center; animation: kNebA 34s ease-in-out infinite alternate; }
          .k-neb-b { transform-box: fill-box; transform-origin: center; animation: kNebB 46s ease-in-out infinite alternate; }
          .k-neb-c { transform-box: fill-box; transform-origin: center; animation: kNebC 40s ease-in-out infinite alternate; }
          .k-tool-pulse { transform-box: fill-box; transform-origin: center; animation: kToolPulse 1.4s ease-out infinite; }
          @keyframes kToolPulse { 0% { transform: scale(0.85); opacity: .9; } 100% { transform: scale(1.35); opacity: .25; } }
          @keyframes kBreathe { 0%,100% { transform: scale(1); } 50% { transform: scale(1.03); } }
          @keyframes kBloom { 0%,100% { opacity: .55; transform: scale(1); } 50% { opacity: .85; transform: scale(1.08); } }
          @keyframes kPulse { 0%,100% { opacity: .5; } 50% { opacity: 1; } }
          @keyframes kDash { to { stroke-dashoffset: -34; } }
          @keyframes kSpin { to { transform: rotate(360deg); } }
          @keyframes kTwinkle { 0%,100% { opacity: var(--o,0.3); } 50% { opacity: calc(var(--o,0.3) * 0.28); } }
          @keyframes kNebA { from { transform: translate(0,0) scale(1); } to { transform: translate(70px,44px) scale(1.12); } }
          @keyframes kNebB { from { transform: translate(0,0) scale(1.05); } to { transform: translate(-64px,-38px) scale(1); } }
          @keyframes kNebC { from { transform: translate(0,0) scale(1); } to { transform: translate(40px,-52px) scale(1.1); } }
          @keyframes kHalo { 0%,100% { opacity: .82; transform: scale(1); } 50% { opacity: 1; transform: scale(1.035); } }
          @media (prefers-reduced-motion: reduce) {
            .k-breathe, .k-bloom, .k-occ, .k-active-edge, .k-spike, .k-spike2,
            .k-sweep, .k-twinkle, .k-neb-a, .k-neb-b, .k-neb-c, .k-halo,
            .k-globe, .k-globe2, .k-tool-pulse { animation: none; }
          }
        `}</style>

        {/* the core's big warm ambient wash — lights the whole field so it
            never reads as flat black (JARVIS-style glow). Core-coloured. */}
        <circle cx={CX} cy={CY} r={1180} fill="url(#k-halo)" className={reduced ? undefined : 'k-halo'} />

        {/* drifting nebula clouds (behind stars) — kills the flat-black look */}
        <ellipse cx={540} cy={360} rx={620} ry={460} fill="url(#k-neb1)" className={reduced ? undefined : 'k-neb-a'} />
        <ellipse cx={1430} cy={760} rx={680} ry={520} fill="url(#k-neb2)" className={reduced ? undefined : 'k-neb-b'} />
        <ellipse cx={1360} cy={250} rx={520} ry={420} fill="url(#k-neb3)" className={reduced ? undefined : 'k-neb-c'} />

        {/* slow radar sweep around the core (symmetric disc → clean rotation) */}
        {!reduced && (
          <g transform={`translate(${CX} ${CY})`}>
            <g className="k-sweep">
              <circle cx={0} cy={0} r={R_PEERS + 60} fill="url(#k-sweep)" />
            </g>
          </g>
        )}

        {STARS.map((s, i) => (
          <circle
            key={i}
            cx={s.x}
            cy={s.y}
            r={s.r}
            fill="#cfe6ff"
            opacity={s.o}
            className={reduced ? undefined : 'k-twinkle'}
            style={reduced ? undefined : ({ '--o': s.o, animationDelay: `${s.d}s` } as React.CSSProperties)}
          />
        ))}

        {/* faint ring guides */}
        {[R_ROLES, R_TOOLS, R_ROOMS].map((r) => (
          <circle key={r} cx={CX} cy={CY} r={r} fill="none" stroke="#2a3550" strokeOpacity={0.35} strokeWidth={1} />
        ))}

        {/* core → role connectors, active one animates */}
        {roles.map((role, i) => {
          const [x, y] = polar(R_ROLES - 14, at(i, roles.length));
          const [cx, cy] = polar(R_CORE + 4, at(i, roles.length));
          const isActive = role.id === model.core.activeRoleId;
          return (
            <line key={`e-${role.id}`} x1={cx} y1={cy} x2={x} y2={y}
              stroke={isActive ? C.active : '#2f3a55'} strokeOpacity={isActive ? 0.9 : 0.28}
              strokeWidth={isActive ? 3 : 1.5} className={isActive && !reduced ? 'k-active-edge' : undefined} />
          );
        })}

        {/* ROOMS ring */}
        {rooms.map((room, i) => {
          const deg = at(i, rooms.length, toolOffset / 2);
          const [x, y] = polar(R_ROOMS, deg);
          const [lx, ly] = polar(R_ROOMS + 30, deg);
          const occupied = room.online && room.occupants > 0;
          // Dot colour = the satellite's live LED state (idle=blue, listening=
          // green, processing=yellow, speaking=cyan, error=red). Offline = the
          // LED is dark → a dim dashed ring. Occupancy adds a presence halo +
          // count, but colour always tracks the LED so the wall mirrors the
          // physical devices.
          const st: SatelliteState | undefined = room.online ? (room.state ?? 'idle') : undefined;
          const col = st ? LED[st] : C.off;
          return (
            <g key={`room-${room.id}`}>
              {occupied && <circle cx={x} cy={y} r={30} fill={col} opacity={0.16} filter="url(#k-soft)" className={reduced ? undefined : 'k-occ'} />}
              <circle cx={x} cy={y} r={occupied ? 13 : 9}
                fill={room.online ? col : 'none'}
                fillOpacity={room.online ? (occupied ? 1 : 0.85) : 1}
                stroke={col} strokeWidth={2.5}
                strokeDasharray={room.online ? undefined : '4 4'}
                filter={room.online ? 'url(#k-glow)' : undefined} />
              {occupied && <text x={x} y={y + 5} textAnchor="middle" fontSize={15} fontWeight={700} fill="#ffffff">{room.occupants}</text>}
              <text x={lx} y={ly + 6} textAnchor={anchorFor(lx)} fontSize={19} fontWeight={occupied ? 600 : 500}
                fill={occupied ? '#eaf2ff' : room.online ? '#aeb9c6' : C.dim} letterSpacing="0.02em">{room.label}</text>
            </g>
          );
        })}

        {/* TOOLS ring */}
        {tools.map((tool, i) => {
          const deg = at(i, tools.length, toolOffset);
          const [x, y] = polar(R_TOOLS, deg);
          const [lx, ly] = polar(R_TOOLS + 22, deg);
          // Internal pseudo-nodes (knowledge / presence / media) now carry a
          // real health verdict too. Only fall back to the dim, dotted "no data"
          // placeholder when the health is genuinely unknown (no verdict pushed
          // yet); otherwise render exactly like a monitored MCP tool.
          const known = tool.health !== 'unknown';
          const col = known ? healthColor(tool.health) : C.unknown;
          const on = tool.health === 'healthy' || tool.health === 'degraded';
          // A subsystem awaiting a verdict ('unknown') OR intentionally off
          // ('off') renders muted — neither is a fault to alarm on.
          const muted = !known || tool.health === 'off';
          // Active-subsystem pulse: a turn_activity naming this MCP server lights
          // its node. The signal rides TWO channels (WCAG 1.4.1 — not colour
          // alone): the turquoise ACTIVE accent AND an expanding concentric ring
          // (a shape/opacity channel). Under reduced motion the ring is present
          // but static (its bloom animation is disabled in the CSS block below).
          const pulse = pulseFor(tool.id);
          const active = pulse > 0;
          const ringR = reduced ? 20 : 14 + (1 - pulse) * 16;
          return (
            <g key={`tool-${tool.id}`} data-tool-id={tool.id} data-tool-health={tool.health}
              data-tool-active={active ? '1' : undefined}>
              {/* Native tooltip: the localized health reason (degraded/off) or
                  the MCP tool-count hint. Inspectable + accessible on the board. */}
              {tool.hint && <title>{tool.hint}</title>}
              {active && (
                <circle cx={x} cy={y} r={ringR} fill="none" stroke={C.active}
                  strokeWidth={2} strokeOpacity={0.28 + 0.55 * pulse}
                  className={reduced ? undefined : 'k-tool-pulse'} />
              )}
              <rect x={x - 8} y={y - 8} width={16} height={16} rx={3} transform={`rotate(45 ${x} ${y})`}
                fill={active ? C.active : on ? col : 'none'}
                stroke={active ? C.active : col} strokeWidth={2.5}
                strokeOpacity={muted && !active ? 0.55 : 1}
                strokeDasharray={!known ? '1 3' : tool.health === 'down' ? '3 3' : undefined}
                filter={on || active ? 'url(#k-glow)' : undefined} />
              <text x={lx} y={ly + 5} textAnchor={anchorFor(lx)} fontSize={16}
                fill={active ? '#eafffb' : C.dim} fontWeight={active ? 600 : 400}>{tool.label}</text>
            </g>
          );
        })}

        {/* ROLES ring */}
        {roles.map((role, i) => {
          const deg = at(i, roles.length);
          const [x, y] = polar(R_ROLES, deg);
          const [lx, ly] = polar(R_ROLES - 30, deg);
          const isActive = role.id === model.core.activeRoleId;
          return (
            <g key={`role-${role.id}`}>
              {isActive && <circle cx={x} cy={y} r={22} fill={C.active} opacity={0.22} filter="url(#k-soft)" />}
              <circle cx={x} cy={y} r={12} fill={isActive ? C.active : '#0d1524'} stroke={isActive ? C.active : '#3a4763'}
                strokeWidth={2.5} filter={isActive ? 'url(#k-glow)' : undefined} />
              <text x={lx} y={ly + 6} textAnchor={anchorFor(lx)} fontSize={18} fontWeight={isActive ? 700 : 500}
                fill={isActive ? '#eafffb' : C.dim} letterSpacing="0.04em">{role.label.toUpperCase()}</text>
            </g>
          );
        })}

        {/* PEERS outer arc */}
        {peers.map((peer, i) => {
          const place = peerPlaces[i];
          const { x, y, label } = place;
          // A paired instance has this ONE node; it carries the name, the
          // reachability and the activity. The state rides two channels (WCAG
          // 1.4.1 — not colour alone): colour AND the ring's line -- solid =
          // reachable, long dashes = unreachable, dots and muted = unknown
          // (PEER_RING_DASH; wide enough to be told apart across a room).
          const reachable = peer.reachability === 'reachable';
          const unknown = peer.reachability === 'unknown';
          const col = reachable ? C.active : unknown ? C.unknown : C.down;
          // A turn that asked this peer names `peer_<id>` in turn_activity --
          // the id of the ASKING account's pairing, so any of the node's ids
          // lights it; an expanding ring runs, as on a tool node.
          const pulse = Math.max(0, ...peer.pulseIds.map(pulseFor));
          const active = pulse > 0;
          const ringR = reduced ? 20 : 14 + (1 - pulse) * 16;
          // A clipped label keeps its full text reachable -- as TEXT in the
          // tooltip, never in an attribute: the name is chosen by the far side.
          const fullLabel = place.clipped
            ? [peer.label, peer.sublabel].filter(Boolean).join(' · ')
            : '';
          const tooltip = [fullLabel, peer.hint].filter(Boolean).join(' — ');
          return (
            <g key={`peer-${peer.id}`} data-peer-id={peer.id} data-peer-reachability={peer.reachability}
              data-peer-active={active ? '1' : undefined} data-peer-recent={peer.recentlyActive ? '1' : undefined}>
              {tooltip && <title>{tooltip}</title>}
              {active && (
                <circle cx={x} cy={y} r={ringR} fill="none" stroke={C.active}
                  strokeWidth={2} strokeOpacity={0.28 + 0.55 * pulse}
                  className={reduced ? undefined : 'k-tool-pulse'} />
              )}
              <circle cx={x} cy={y} r={9} fill={active ? C.active : 'none'}
                stroke={active ? C.active : col} strokeWidth={2.5}
                strokeOpacity={unknown && !active ? 0.55 : 1}
                strokeDasharray={PEER_RING_DASH[peer.reachability]}
                strokeLinecap={unknown ? 'round' : undefined}
                data-peer-ring=""
                filter={reachable || active ? 'url(#k-glow)' : undefined} />
              {/* Recently active (a real request went either way): a filled
                  centre -- a second fact, independent of the ring around it. */}
              {peer.recentlyActive && !active && <circle cx={x} cy={y} r={3.5} fill={C.cream} />}
              <text x={label.x} y={label.primaryY} textAnchor={label.anchor}
                fontSize={PEER_LABEL_FONT.primary} data-peer-label="primary"
                fill={active ? '#eafffb' : C.dim} fontWeight={active ? 600 : 400}>{place.primary}</text>
              {place.secondary && (
                <text x={label.x} y={label.secondaryY} textAnchor={label.anchor}
                  fontSize={PEER_LABEL_FONT.secondary} data-peer-label="secondary"
                  fill={C.dim} fillOpacity={0.75}>{place.secondary}</text>
              )}
            </g>
          );
        })}

        {/* voice spikes when listening/speaking — a burst ring around the core.
            Coordinates are around the ORIGIN and the group is translated to the
            core, so the CSS rotation (transform-origin: center) spins around the
            core instead of the group's own displaced bbox. */}
        {voiceActive && !reduced && (
          <g opacity={0.85} transform={`translate(${CX} ${CY})`}>
            <g className="k-spike">
              {spikes.map((deg, i) => {
                const len = 22 + ((i * 37) % 40);
                const [x1, y1] = ray(R_CORE + 6, deg);
                const [x2, y2] = ray(R_CORE + 6 + len, deg);
                return <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} stroke={coreColor} strokeWidth={2} strokeLinecap="round" />;
              })}
            </g>
            <g className="k-spike2" opacity={0.5}>
              {spikes.filter((_, i) => i % 2 === 0).map((deg, i) => {
                const len = 14 + ((i * 53) % 28);
                const [x1, y1] = ray(R_CORE + 10, deg);
                const [x2, y2] = ray(R_CORE + 10 + len, deg);
                return <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} stroke={coreColor} strokeWidth={1.5} strokeLinecap="round" />;
              })}
            </g>
          </g>
        )}

        {/* CORE — a translucent light-globe (JARVIS), NOT a solid disc. Outer
            bloom → luminous translucent body → rotating meridian filaments →
            bright rim → a small bright heart. No text stamped on it; the state
            word sits as an elegant caption below. */}
        <circle cx={CX} cy={CY} r={R_CORE * 2.8} fill="url(#k-bloom)" className={reduced ? undefined : 'k-bloom'} />
        {/* outer group positions the globe; inner group owns the breathe scale
            (a CSS-animated transform would otherwise clobber an inline one).
            data-core-state exposes the live state (conveyed visually by colour)
            for tests + tooling, since there's no longer a caption word. */}
        <g transform={`translate(${CX} ${CY})`} data-core-state={core}>
          <g className={reduced ? undefined : 'k-breathe'}>
            {/* luminous translucent body */}
            <circle cx={0} cy={0} r={R_CORE} fill="url(#k-core)" />
            {/* rotating meridian filaments — the wireframe-globe structure */}
            <g className={reduced ? undefined : 'k-globe'} fill="none" stroke={coreColor} filter="url(#k-glow)">
              <ellipse cx={0} cy={0} rx={R_CORE} ry={R_CORE * 0.34} strokeOpacity={0.45} strokeWidth={1.4} />
              <ellipse cx={0} cy={0} rx={R_CORE * 0.34} ry={R_CORE} strokeOpacity={0.45} strokeWidth={1.4} />
              <ellipse cx={0} cy={0} rx={R_CORE * 0.94} ry={R_CORE * 0.66} strokeOpacity={0.28} strokeWidth={1.2} transform="rotate(32)" />
              <ellipse cx={0} cy={0} rx={R_CORE * 0.66} ry={R_CORE * 0.94} strokeOpacity={0.28} strokeWidth={1.2} transform="rotate(-32)" />
            </g>
            <g className={reduced ? undefined : 'k-globe2'} fill="none" stroke="#ffffff" filter="url(#k-glow)">
              <ellipse cx={0} cy={0} rx={R_CORE * 0.86} ry={R_CORE * 0.5} strokeOpacity={0.18} strokeWidth={1} transform="rotate(-14)" />
            </g>
            {/* bright rim = the globe's edge */}
            <circle cx={0} cy={0} r={R_CORE} fill="none" stroke={coreColor} strokeOpacity={0.9} strokeWidth={2} filter="url(#k-glow)" />
            {/* bright heart */}
            <circle cx={0} cy={0} r={9} fill="#fff" opacity={0.92} filter="url(#k-glow)" />
          </g>
        </g>
        {/* No state word on the core — its LED colour + the legend carry the
            status. Only the active room surfaces (which room is talking), and
            only during a live voice interaction. */}
        {core !== 'idle' && activeRoom && (
          <text x={CX} y={CY + R_CORE + 50} textAnchor="middle" fontSize={18}
            fill={coreColor} opacity={0.85} letterSpacing="0.14em" className="font-display">
            {activeRoom}
          </text>
        )}
      </svg>

      {/* ---- HTML overlays (crisp typography over the SVG) ---- */}
      {/* wordmark */}
      <div className="absolute top-8 left-10 leading-tight">
        <div className="font-display text-4xl text-white tracking-wide">RENFIELD</div>
        <div className="text-[13px] tracking-[0.35em] mt-1" style={{ color: C.active }}>COMMAND CENTER</div>
      </div>

      {/* clock tile — wall time + date from the device clock; owns its own
          minute-aligned tick, so it re-renders alone (see KioskClock). */}
      <div className="absolute bottom-8 left-10">
        <KioskClock />
      </div>

      {/* ambient telemetry corner */}
      <div className="absolute top-8 right-10 text-right space-y-3">
        <Telem label={t('kiosk.telemetry.satellites', { defaultValue: 'Satellites' })}
          value={`${telemetry.satellitesOnline}/${telemetry.satellitesTotal} ${t('kiosk.online', { defaultValue: 'online' })}`}
          alert={telemetry.satellitesOnline < telemetry.satellitesTotal} />
        <Telem label={t('kiosk.telemetry.present', { defaultValue: 'Present' })}
          value={t('kiosk.presentValue', { defaultValue: '{{people}} in {{rooms}} rooms', people: telemetry.peoplePresent, rooms: telemetry.occupiedRooms })} />
        <Telem label={t('kiosk.telemetry.tools', { defaultValue: 'Tools' })}
          value={`${telemetry.toolsHealthy}/${telemetry.toolsTotal} ${t('kiosk.healthy', { defaultValue: 'healthy' })}`}
          alert={telemetry.toolsHealthy < telemetry.toolsTotal} />
        <Telem label={t('kiosk.telemetry.agent', { defaultValue: 'Agent' })}
          value={activeRoleLabel ?? t('kiosk.idle', { defaultValue: 'idle' })} />
      </div>

      {/* weather tile (under the wordmark) — hidden when no reading */}
      {weather && <WeatherTile weather={weather} />}

      {/* now-playing (bottom center) — media-follow sessions, one per room */}
      {nowPlaying.length > 0 && (
        <div className="absolute bottom-8 left-1/2 -translate-x-1/2 flex flex-col items-center gap-2">
          {nowPlaying.slice(0, 3).map((s, i) => (
            <NowPlaying key={`${s.room}-${i}`} session={s} />
          ))}
        </div>
      )}

      {/* legend — the satellite LED status colours (matches the physical ring) */}
      <div className="absolute bottom-8 right-10 flex flex-col gap-1.5 text-[13px]">
        {([
          { label: t('kiosk.state.idle', { defaultValue: 'ready' }), swatch: { background: LED.idle } },
          { label: t('kiosk.state.listening', { defaultValue: 'listening' }), swatch: { background: LED.listening } },
          { label: t('kiosk.state.processing', { defaultValue: 'thinking' }), swatch: { background: LED.processing } },
          { label: t('kiosk.state.speaking', { defaultValue: 'speaking' }), swatch: { background: LED.speaking } },
          { label: t('kiosk.state.error', { defaultValue: 'error' }), swatch: { background: LED.error } },
          { label: t('kiosk.legend.offline', { defaultValue: 'Offline' }), swatch: { border: `1.5px dashed ${C.off}` } },
        ]).map((row) => (
          <span key={row.label} className="inline-flex items-center justify-end gap-2 text-white/45 capitalize">
            {row.label}
            <span className="inline-block w-2.5 h-2.5 rounded-full" style={row.swatch} />
          </span>
        ))}
        {/* The peers arc speaks in line AND colour; the legend shows both, and
            only while there is a peer to read them on. */}
        {peers.length > 0 && (
          <div className="mt-1.5 flex flex-col gap-1.5" data-testid="kiosk-peer-legend">
            {([
              { state: 'reachable', border: `2px solid ${C.active}`, opacity: 1 },
              { state: 'unreachable', border: `2px dashed ${C.down}`, opacity: 1 },
              ...(peersChecked ? [{ state: 'unknown', border: `2px dotted ${C.unknown}`, opacity: 0.55 }] : []),
            ]).map((row) => (
              <span key={row.state} data-peer-legend={row.state}
                className="inline-flex items-center justify-end gap-2 text-white/45">
                {t(`kiosk.peer.legend.${row.state}`)}
                <span className="inline-block w-3 h-3 rounded-full"
                  style={{ border: row.border, opacity: row.opacity }} />
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function WeatherTile({ weather }: { weather: NonNullable<KioskState['weather']> }) {
  const Icon = iconForCode(weather.code);
  const hasRange = weather.high != null && weather.low != null;
  return (
    <div className="absolute top-[7.5rem] left-10 flex items-center gap-4 text-white">
      <Icon className="w-11 h-11 shrink-0" strokeWidth={1.4} style={{ color: C.active }} aria-hidden="true" />
      <div className="leading-tight">
        <div className="flex items-baseline gap-2">
          <span className="text-4xl font-display tabular-nums">{Math.round(weather.temp)}{weather.unit}</span>
          {hasRange && (
            <span className="text-sm text-white/45 tabular-nums">
              {Math.round(weather.high as number)}° / {Math.round(weather.low as number)}°
            </span>
          )}
        </div>
        <div className="text-sm text-white/55 mt-0.5">
          {weather.condition}
          {weather.location ? <span className="text-white/35"> · {weather.location}</span> : null}
        </div>
      </div>
    </div>
  );
}

function nowPlayingIcon(kind: string) {
  if (kind === 'radio') return RadioIcon;
  if (kind === 'dlna_video') return Film;
  if (kind === 'dlna_album') return ListMusic;
  return Music2;
}

function NowPlaying({ session }: { session: KioskNowPlaying }) {
  const Icon = nowPlayingIcon(session.kind);
  const line = session.title || session.subtitle || '';
  return (
    <div className="inline-flex items-center gap-2.5 px-4 py-1.5 rounded-full
      bg-white/[0.04] border border-white/10 backdrop-blur-sm max-w-[42rem]">
      <Icon className="w-4 h-4 shrink-0" style={{ color: C.active }} aria-hidden="true" />
      <span className="text-[13px] tracking-wide font-medium" style={{ color: C.active }}>
        {session.room}
      </span>
      {line && (
        <>
          <span className="text-white/25">·</span>
          <span className="text-[13px] text-white/70 truncate">{line}</span>
        </>
      )}
    </div>
  );
}

function Telem({ label, value, alert }: { label: string; value: string; alert?: boolean }) {
  return (
    <div>
      <div className="text-[11px] tracking-[0.25em] text-white/35 uppercase">{label}</div>
      <div className={`text-lg font-medium tabular-nums ${alert ? 'text-[#f7a4ae]' : 'text-[#00e4b8]'}`}>{value}</div>
    </div>
  );
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const on = () => setReduced(mq.matches);
    on();
    // Older WebKit (some wall-display browsers) only has the legacy add/remove
    // Listener API — fall back so the effect never throws and blanks the kiosk.
    if (mq.addEventListener) {
      mq.addEventListener('change', on);
      return () => mq.removeEventListener('change', on);
    }
    mq.addListener(on);
    return () => mq.removeListener(on);
  }, []);
  return reduced;
}

/** A bare epoch-ms render tick (default 1s). UI-only — it advances wall time so
 *  time-based visuals (the active-subsystem pulse fade) recompute; it makes NO
 *  network call. */
function usePulseTick(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}
