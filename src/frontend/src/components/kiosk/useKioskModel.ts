// Kiosk model = the ring model (CommandCenterModel shape) + a voice-reactive
// core state derived from the satellites' own state (idle/listening/processing/
// speaking). The wall display reacts to real household voice activity.
//
// DATA SOURCE: reads from the PUSH socket useKioskSocket() (the `/ws/kiosk`
// snapshot + deltas) — never a poll. The derivation below (voice-state priority
// merge, telemetry counts, ring assembly of roles / tools / rooms / peers) was
// inherited from the now-decommissioned admin board's `useCommandCenterModel`
// (deleted with the Command Center, 2026-07) and re-sourced from the
// snapshot+delta reducer. See tasks/kiosk-active-subsystem-plan.md §3.
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { roleLabel } from '../chat/AgentRoleBadge';
import {
  parseServerMs,
  useKioskSocket,
  type KioskLiveModel,
  type KioskPeer,
  type KioskPeerClock,
} from './useKioskSocket';
import type { KioskWeather, KioskNowPlaying } from '../../api/resources/kiosk';
import { peerLabelParts } from '../../utils/peerLabel';
import type { CommandCenterModel, NodeHealth, PeerNode, PeerReachability, ToolNode } from './types';

export type CoreState = 'idle' | 'listening' | 'processing' | 'speaking' | 'busy';

/** Voice states in ascending priority — a speaking satellite wins over a
 *  merely-listening one when several are active at once. ('error' is handled
 *  separately, not here, so a fleet error can't read as idle.) */
const STATE_PRIORITY: Record<string, number> = {
  idle: 0,
  listening: 1,
  processing: 2,
  speaking: 3,
};

// ---- ring-assembly constants (mirrored from useCommandCenterModel) ---------
// NOTE: SATELLITE liveness is no longer wall-clock derived — the backend pushes
// `satellite_online`/`satellite_offline` (a satellite in the roster IS online).
// FEDERATION PEERS get the `peer_status_changed` delta (#969, consumed in
// useKioskSocket) and ADDITIONALLY keep the wall-clock staleness backstop
// below: a delta lost on a dropped socket would otherwise freeze a
// since-gone-down peer green until the next reconnect.
/** An activation older than this no longer lights the core (the turn is over). */
const ACTIVE_WINDOW_MS = 90_000;
/** Trail entries older than this are fully decayed and dropped from the board. */
const TRAIL_WINDOW_MS = 15 * 60_000;
/** Wall-clock recompute cadence: active-role expiry / trail decay / peer
 *  staleness must advance by the PASSAGE OF TIME, not only on new data. */
const CLOCK_TICK_MS = 15_000;
/** Reachability check OFF: a federation peer unseen for longer than this
 *  renders unreachable (the freshness backstop behind the peer_status_changed
 *  delta). Not applied while the check is on -- see `peerNodeFor`. */
const PEER_OFFLINE_MS = 10 * 60_000;
/** Reachability check ON: a verdict whose check is older than this renders
 *  `unknown` with the reason `stale`. The backend is the authority: it checks
 *  every pairing every 5 min while a display is connected, re-affirms an
 *  unchanged list at least every 10 min, and pushes `unknown` itself when a
 *  result expires. So a healthy socket never gets near this; it is the guard
 *  for a socket that went SILENT without closing. Measured without the
 *  device's wall clock -- see `peerAgeMs`. */
const PEER_CHECK_STALE_MS = 20 * 60_000;
/** Aggregated per-server success rate below this (with enough calls) = degraded. */
const DEGRADED_SUCCESS_RATE = 0.8;
const DEGRADED_MIN_CALLS = 3;

/** Display names for MCP servers whose ids don't title-case cleanly. */
const SERVER_LABELS: Record<string, string> = {
  homeassistant: 'Home Assistant',
  dlna: 'DLNA',
  n8n: 'n8n',
  searxng: 'SearXNG',
  tts: 'TTS',
};

/** The INTERNAL-only subsystems (`internal.*` tools with no MCP server) that the
 *  active-subsystem pulse can name. Rendered as always-present, pulse-only
 *  pseudo-nodes on the tools ring so a knowledge/presence/media turn has a node
 *  to light. MUST stay in sync with the backend `INTERNAL_SUBSYSTEM_LABELS`
 *  internal-only value set (api/websocket/chat_handler.py). `homeassistant` /
 *  `weather` are excluded here — they are real MCP servers with their own nodes. */
const INTERNAL_SUBSYSTEM_NODES: { id: string; labelKey: string; fallback: string }[] = [
  { id: 'knowledge', labelKey: 'kiosk.subsystem.knowledge', fallback: 'Knowledge' },
  { id: 'presence', labelKey: 'kiosk.subsystem.presence', fallback: 'Presence' },
  { id: 'media', labelKey: 'kiosk.subsystem.media', fallback: 'Media' },
];

function prettifyServerName(name: string): string {
  const known = SERVER_LABELS[name.toLowerCase()];
  if (known) return known;
  return name
    .split(/[_-]/)
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : part))
    .join(' ');
}

/** The backend emits naive-UTC ISO strings (no zone suffix); anchor them so
 *  Date.parse doesn't read them as LOCAL time. */
function parseNaiveUtcMs(iso: string): number {
  return Date.parse(iso.endsWith('Z') ? iso : `${iso}Z`);
}

const PEER_STATES: readonly PeerReachability[] = ['reachable', 'unreachable', 'unknown'];

/** How old a server timestamp is NOW, without trusting the device's wall
 *  clock: its age on the server's clock when the list arrived, plus the time
 *  that has passed on the device since then. Both terms are differences on
 *  ONE clock each, so a device that runs fast or slow reads the same age.
 *
 *  The device's part is taken on its MONOTONIC clock (`monoNow`,
 *  `performance.now()`), not on `Date.now()`: a wall clock that is stepped
 *  while the page is open -- NTP, daylight saving, a manual change -- must
 *  not age a reading by the size of the step, nor rejuvenate it.
 *
 *  Without a server stamp (a backend older than this build) there is nothing
 *  to anchor on, and the two wall clocks are compared directly as before
 *  (`now`, the only use of it). */
export function peerAgeMs(
  iso: string,
  now: number,
  clock?: KioskPeerClock | null,
  monoNow: number = performance.now(),
): number {
  const at = parseServerMs(iso);
  if (at === null) return NaN;
  if (!clock || clock.serverAt === null) return now - at;
  return Math.max(0, clock.serverAt - at) + Math.max(0, monoNow - clock.receivedMono);
}

/** The arc node of one paired instance -- everything except the localised
 *  tooltip. Pure, so the rules below can be tested without a socket.
 *
 *  A paired instance has ONE node. The backend keeps its `peer_<id>` tool
 *  server out of `mcp.servers`, so nothing here has to hide it; what a turn
 *  that asked the peer still names is the subsystem `peer_<id>` of the asking
 *  account's pairing. Those are the node's `pulseIds`: one for a single
 *  pairing, all of them when several accounts are paired with this instance.
 *
 *  Two rules for the state, chosen by what the backend sent:
 *
 *  - `reachability` present (the signed check is on): the backend's verdict
 *    stands, three-valued. `last_seen_at` plays NO part -- a ping does not move
 *    it, so gating on it would paint a reachable but quiet instance red. The
 *    backstop is on the AGE of `checked_at` instead (`peerAgeMs`), and it
 *    degrades to `unknown`, never to `unreachable`: a reading nobody refreshed
 *    is not a finding about the far side. Its reason is `stale` -- "checked,
 *    but the result is old" -- which is not the same as never checked.
 *  - absent (check off, or an older backend): the rule the wall has always
 *    had -- the peer queried us recently (`reachable`) and `last_seen_at` is
 *    younger than `PEER_OFFLINE_MS`. Two-valued; it cannot say `unknown`.
 */
export function peerNodeFor(
  peer: KioskPeer,
  now: number,
  clock?: KioskPeerClock | null,
  monoNow: number = performance.now(),
): PeerNode {
  const id = String(peer.id);
  const { primary, secondary } = peerLabelParts(peer.instance_name, peer.name);
  const sent = Array.isArray(peer.pulse_ids)
    ? peer.pulse_ids.filter((p): p is string => typeof p === 'string' && p !== '')
    : [];
  const pulseIds = sent.length > 0 ? sent : [`peer_${id}`];
  const base = { id, label: primary, sublabel: secondary, pulseIds };

  if (typeof peer.reachability !== 'string') {
    const seenAge = peer.last_seen_at ? peerAgeMs(peer.last_seen_at, now, clock, monoNow) : NaN;
    const fresh = Number.isFinite(seenAge) && seenAge < PEER_OFFLINE_MS;
    return {
      ...base,
      reachability: peer.reachable && fresh ? 'reachable' : 'unreachable',
      recentlyActive: false,
    };
  }

  const known = PEER_STATES.find((state) => state === peer.reachability);
  const checkedAge = peer.checked_at ? peerAgeMs(peer.checked_at, now, clock, monoNow) : NaN;
  // A verdict with no check behind it was never checked; one whose check is
  // too old to stand behind is stale. Either way the wall says unknown -- and
  // so it does for a state this build does not know.
  const verdict = known === 'reachable' || known === 'unreachable';
  const unchecked = verdict && !Number.isFinite(checkedAge);
  const stale = verdict && Number.isFinite(checkedAge) && checkedAge >= PEER_CHECK_STALE_MS;
  return {
    ...base,
    reachability: !known || unchecked || stale ? 'unknown' : known,
    reason: stale ? 'stale' : unchecked ? 'not_checked' : (peer.reachability_reason ?? null),
    recentlyActive: peer.recently_active === true,
  };
}

/** Stable empty default so `?? EMPTY` doesn't mint a fresh array reference each
 *  render (which would defeat the useMemo below). */
const EMPTY_NOW_PLAYING: KioskNowPlaying[] = [];

export interface KioskState {
  model: CommandCenterModel;
  bootLoading: boolean;
  backendUnreachable: boolean;
  /** What the glowing core shows right now. */
  core: CoreState;
  /** Room where the active voice interaction is happening (if any). */
  activeRoom: string | null;
  /** Localized label of the agent role answering this turn (if any). */
  activeRoleLabel: string | null;
  /** Home-location weather for the ambient tile (null = hide the tile). */
  weather: KioskWeather | null;
  /** Live media-follow sessions for the now-playing tile (empty = hide). */
  nowPlaying: KioskNowPlaying[];
  /** subsystem id → epoch-ms last active. Drives the active-subsystem pulse in
   *  KioskConstellation (a MCP-server node glows when its subsystem is named by
   *  a `turn_activity` event). The view fades it on its own render tick. */
  subsystemPulses: Record<string, number>;
  /** At-a-glance counts for the ambient telemetry corner. */
  telemetry: {
    satellitesOnline: number;
    satellitesTotal: number;
    peoplePresent: number;
    occupiedRooms: number;
    toolsHealthy: number;
    toolsTotal: number;
  };
}

/** Assemble the ring model (roles / tools / rooms / peers / trail / activeRole)
 *  from the pushed live model. Mirrors useCommandCenterModel's derivation,
 *  adapted to the snapshot section shapes. */
function buildCommandCenterModel(
  live: KioskLiveModel,
  t: ReturnType<typeof useTranslation>['t'],
  lang: 'de' | 'en',
  now: number,
): CommandCenterModel {
  // ---- roles ring -------------------------------------------------------
  const roles = live.roles.map((role) => ({
    id: role.name,
    label: roleLabel(t, role.name),
    reachServers: role.mcp_servers,
    hint: role.description?.[lang] ?? role.description?.en,
  }));

  // ---- pulse trail ------------------------------------------------------
  const trail = live.activity
    .map((entry) => ({
      roleId: entry.role,
      at: parseNaiveUtcMs(entry.at),
      ok: entry.ok,
    }))
    .filter((entry) => Number.isFinite(entry.at) && now - entry.at < TRAIL_WINDOW_MS);
  const head = trail[0];
  const activeRoleId =
    head && now - head.at < ACTIVE_WINDOW_MS ? head.roleId : undefined;

  // ---- tools ring -------------------------------------------------------
  // Health = MCP connection state, downgraded when a connected server's tools
  // are failing. The snapshot pre-classifies per tool (total + success_rate);
  // reconstruct per-tool succ/fail and aggregate per server so the SAME 0.8 /
  // min-3 threshold the admin board uses still decides "degraded".
  const failing = new Map<string, { succ: number; fail: number }>();
  for (const stat of live.toolHealth) {
    const match = /^mcp\.([^.]+)\./.exec(stat.tool_name);
    if (!match) continue;
    const succ = Math.round(stat.total * stat.success_rate);
    const fail = stat.total - succ;
    const agg = failing.get(match[1]) ?? { succ: 0, fail: 0 };
    agg.succ += succ;
    agg.fail += fail;
    failing.set(match[1], agg);
  }
  const tools: ToolNode[] = live.mcp.servers.map((server) => {
    // The backend now folds connectivity AND functionality into `health`
    // (e.g. a connected server whose backing plugin failed → 'degraded'); it is
    // authoritative for down/degraded. On a healthy (or absent) backend verdict
    // the frontend still layers its tool-call success-rate degradation on top.
    const beHealth = server.health;
    let health: NodeHealth;
    if (!server.connected || beHealth === 'down') {
      health = 'down';
    } else if (beHealth === 'degraded' || server.last_error) {
      health = 'degraded';
    } else {
      const agg = failing.get(server.name);
      const total = agg ? agg.succ + agg.fail : 0;
      health =
        agg && total >= DEGRADED_MIN_CALLS &&
        agg.succ / total < DEGRADED_SUCCESS_RATE
          ? 'degraded'
          : 'healthy';
    }
    // Localize the backend's machine reason code (never render a raw backend
    // string — i18n rule); fall back to the tool count.
    const toolHint = t('kiosk.toolHint', {
      count: server.tool_count,
      defaultValue: '{{count}} tools',
    });
    const impairedHint =
      health === 'degraded' && server.impaired_code
        ? t(`kiosk.impaired.${server.impaired_code}`, { defaultValue: toolHint })
        : toolHint;
    return {
      id: server.name,
      label: prettifyServerName(server.name),
      health,
      hint: !server.connected
        ? server.last_error || t('kiosk.legend.down')
        : impairedHint,
    };
  });

  // Append the internal-only subsystem pseudo-nodes (knowledge / presence /
  // media) so an `internal.*` turn has a node to light. They now carry a REAL
  // health verdict pushed by the backend (`internalHealth`): healthy/degraded/
  // down, defaulting to 'unknown' (gray) until the first verdict lands. Still
  // `synthetic` so they stay out of the MCP tool-health telemetry counts. Skip
  // any id a REAL MCP server already owns (e.g. an operator adds an
  // output-provider stanza named `media`) — the real node wins and handles that
  // pulse, and we never emit a duplicate `data-tool-id` / React key.
  const realServerIds = new Set(tools.map((tool) => tool.id));
  for (const node of INTERNAL_SUBSYSTEM_NODES) {
    if (realServerIds.has(node.id)) continue;
    const verdict = live.internalHealth[node.id];
    const health: NodeHealth = verdict?.health ?? 'unknown';
    // Localize the machine reason code (never render a raw backend string —
    // i18n rule); fall back to a generic per-health hint.
    const hint = verdict?.impaired_code
      ? t(`kiosk.impaired.${verdict.impaired_code}`, {
          defaultValue:
            health === 'degraded'
              ? t('kiosk.legend.degraded', { defaultValue: 'Degraded' })
              : t('kiosk.legend.offline', { defaultValue: 'Off' }),
        })
      : undefined;
    tools.push({
      id: node.id,
      label: t(node.labelKey, { defaultValue: node.fallback }),
      health,
      synthetic: true,
      hint,
    });
  }

  // ---- rooms ring -------------------------------------------------------
  // Union of satellite rooms (online state) and presence rooms (occupants).
  const occupantsByRoom = new Map<string, number>();
  for (const room of live.presence.rooms) {
    if (!room.room_name) continue;
    occupantsByRoom.set(room.room_name.toLowerCase(), room.occupants);
  }
  const STATE_RANK: Record<string, number> = {
    idle: 0, listening: 1, processing: 2, speaking: 3, error: 4,
  };
  const rooms = new Map<
    string,
    {
      id: string; label: string; online: boolean; occupants: number;
      state?: 'idle' | 'listening' | 'processing' | 'speaking' | 'error';
      hint?: string;
    }
  >();
  for (const sat of live.satellites) {
    // A satellite without a room binding yet (online delta before its DB sync)
    // can't be placed on the rooms ring — skip until a state/snapshot names it.
    if (!sat.room) continue;
    const key = sat.room.toLowerCase();
    // Every satellite in the roster is online — the backend removed it via a
    // `satellite_offline` delta the moment it dropped (no wall-clock decay).
    const existing = rooms.get(key);
    let state = existing?.state;
    if (!state || (STATE_RANK[sat.state] ?? 0) > (STATE_RANK[state] ?? 0)) {
      state = sat.state;
    }
    rooms.set(key, {
      id: key,
      label: sat.room,
      online: true,
      occupants: occupantsByRoom.get(key) ?? 0,
      state,
      hint: sat.satellite_id,
    });
  }
  for (const [key, occupants] of occupantsByRoom) {
    if (rooms.has(key)) continue;
    const label = live.presence.rooms.find(
      (room) => room.room_name?.toLowerCase() === key,
    )?.room_name;
    rooms.set(key, {
      id: key,
      label: label ?? key,
      online: true, // presence saw someone here; there's just no satellite
      occupants,
      hint: t('kiosk.noSatellite', { defaultValue: 'No satellite' }),
    });
  }

  // ---- peers arc --------------------------------------------------------
  // The peer_status_changed delta (#969) updates the peers live; a wall-clock
  // backstop stays as a second guard behind it, so a delta lost on the socket
  // cannot freeze a verdict until the next reconnect. Rules: `peerNodeFor`.
  const peers = live.peers.map((peer) => {
    const node = peerNodeFor(peer, now, live.peersClock);
    // The tooltip says what the ring shows, in words -- only while the signed
    // check is on. Without it the ring is the old "asked us recently" light,
    // which never had a tooltip and gets no reachability wording now.
    if (typeof peer.reachability !== 'string') return node;
    const state = t(`kiosk.peer.state.${node.reachability}`);
    // Localise the machine code (never render a raw backend string); a code
    // this build has no text for leaves the state standing alone.
    const reason = node.reason
      ? t(`circles.peerReachReason.${node.reason}`, { defaultValue: '' })
      : '';
    const parts = [reason ? `${state} — ${reason}` : state];
    if (node.recentlyActive) parts.push(t('kiosk.peer.recentlyActive'));
    return { ...node, hint: parts.join(' · ') };
  });

  return {
    core: { label: 'Renfield', activeRoleId },
    roles,
    tools,
    rooms: [...rooms.values()].sort((a, b) => a.label.localeCompare(b.label)),
    peers,
    trail,
  };
}

export function useKioskModel(): KioskState {
  const { t, i18n } = useTranslation();
  const { live, bootLoading, backendUnreachable } = useKioskSocket();
  const lang = i18n.language?.startsWith('de') ? 'de' : 'en';

  // Wall-clock input for the memo: active-role expiry / trail decay must
  // advance even when no new event arrives (liveness is now delta-driven).
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowTick(Date.now()), CLOCK_TICK_MS);
    return () => clearInterval(id);
  }, []);

  const weather = live.weather;
  const nowPlaying = live.nowPlaying.length > 0 ? live.nowPlaying : EMPTY_NOW_PLAYING;
  const subsystemPulses = live.subsystemPulses;

  return useMemo<KioskState>(() => {
    const model = buildCommandCenterModel(live, t, lang, nowTick);

    // Every satellite in the roster is online — the backend drops a dead one
    // via a `satellite_offline` delta, so there is no stale-heartbeat to filter.
    const onlineSats = live.satellites;
    // Telemetry counts only satellites that carry a room, so the "N/M online"
    // corner never exceeds the number of room dots actually drawn (the rooms
    // ring skips a just-registered satellite whose room binding hasn't landed).
    const placedSats = onlineSats.filter((s) => s.room);

    // ---- voice-reactive core state from the ONLINE satellites' own state ---
    let core: CoreState = backendUnreachable ? 'busy' : 'idle';
    let activeRoom: string | null = null;
    let best = 0;
    let anyError = false;
    for (const sat of onlineSats) {
      if (sat.state === 'error') { anyError = true; continue; }
      const p = STATE_PRIORITY[sat.state] ?? 0;
      if (p > best) {
        best = p;
        core = sat.state as CoreState;
        activeRoom = sat.room;
      }
    }
    if (!backendUnreachable && best === 0 && anyError) core = 'busy';

    // A typed web-chat turn has no satellite/room, but Renfield IS working — show
    // the core "thinking" when no satellite outranks it and nothing's errored.
    if (
      !backendUnreachable &&
      live.chatActive &&
      best < STATE_PRIORITY.processing &&
      !anyError
    ) {
      core = 'processing';
    }

    const activeRoleLabel = model.core.activeRoleId
      ? roleLabel(t, model.core.activeRoleId)
      : null;

    // People + occupied rooms from the SAME online-filtered set so the two
    // telemetry numbers can never disagree ("2 in 0 rooms").
    const liveOccupiedRooms = model.rooms.filter((r) => r.online && r.occupants > 0);

    return {
      model,
      bootLoading,
      backendUnreachable,
      core,
      activeRoom,
      activeRoleLabel,
      weather,
      nowPlaying,
      subsystemPulses,
      telemetry: {
        satellitesOnline: placedSats.length,
        satellitesTotal: placedSats.length,
        peoplePresent: liveOccupiedRooms.reduce((n, r) => n + r.occupants, 0),
        occupiedRooms: liveOccupiedRooms.length,
        // Synthetic internal pseudo-nodes have no health → excluded from counts.
        toolsHealthy: model.tools.filter((tool) => !tool.synthetic && tool.health === 'healthy').length,
        toolsTotal: model.tools.filter((tool) => !tool.synthetic).length,
      },
    };
  }, [t, lang, nowTick, live, bootLoading, backendUnreachable, weather, nowPlaying, subsystemPulses]);
}
