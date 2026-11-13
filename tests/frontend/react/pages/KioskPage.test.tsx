/**
 * KioskPage — the fullscreen wall-display command center. Now fed by the PUSH
 * socket (useKioskSocket) instead of react-query polls: the fixtures moved from
 * MSW HTTP handlers to a `snapshot` message pushed over a mock WebSocket. The
 * derivation math (voice-core priority, telemetry counts) is unchanged, so this
 * is the same behavioural coverage — only the data source differs.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor, act } from '@testing-library/react';
import { renderWithProviders } from '../test-utils';
import KioskPage from '../../../../src/frontend/src/pages/KioskPage';

// Security audit M2: useKioskSocket now fetches a short-lived WS-scoped token
// (fetchWsToken → POST /api/ws/token) and AWAITS it before constructing the
// socket, so the socket is created asynchronously. Mock it to resolve
// immediately; pushSnapshot() waits for the socket to exist before driving it.
vi.mock('../../../../src/frontend/src/utils/wsToken', () => ({
  fetchWsToken: vi.fn().mockResolvedValue('test-ws-token'),
}));

type WsListener<E = unknown> = ((event: E) => void) | null;

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  url: string;
  readyState = 0;
  onopen: WsListener<Event> = null;
  onclose: WsListener<CloseEvent> = null;
  onmessage: WsListener<MessageEvent> = null;
  onerror: WsListener<Event> = null;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  fireOpen(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }
  fireMessage(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
}

const roles = [
  { name: 'presence', description: { de: 'Presence', en: 'Presence' }, mcp_servers: [], internal_tools: null, has_agent_loop: true },
  { name: 'general', description: { de: 'General', en: 'General' }, mcp_servers: null, internal_tools: null, has_agent_loop: true },
];
const mcp = {
  enabled: true,
  total_tools: 20,
  servers: [
    { name: 'homeassistant', connected: true, transport: 'stdio', tool_count: 10 },
    { name: 'radio', connected: false, transport: 'stdio', tool_count: 0 },
  ],
};

interface SnapOverrides {
  satellites?: unknown[];
  weather?: unknown;
  now_playing?: unknown[];
  mcp?: unknown;
  internal_health?: unknown[];
  peers?: unknown[];
}

/** A naive-UTC stamp `minutes` ago, the shape the backend sends. */
const minutesAgo = (minutes: number): string =>
  new Date(Date.now() - minutes * 60_000).toISOString().replace('Z', '');

function snapshot(satState: string, over: SnapOverrides = {}) {
  return {
    type: 'snapshot',
    at: '2026-07-04T21:00:00Z',
    satellites: over.satellites ?? [
      { satellite_id: 'sat-wohnzimmer', room: 'Wohnzimmer', room_id: 1, state: satState },
      { satellite_id: 'sat-esszimmer', room: 'Esszimmer', room_id: 2, state: 'idle' },
    ],
    presence: {
      rooms: [{ room_id: 1, room_name: 'Wohnzimmer', occupants: 1 }],
      people_present: 1,
      occupied_rooms: 1,
    },
    mcp: over.mcp ?? mcp,
    tool_health: [],
    internal_health: over.internal_health ?? [],
    roles,
    activity: [],
    peers: over.peers ?? [],
    weather: over.weather ?? null,
    now_playing: over.now_playing ?? [],
  };
}

async function pushSnapshot(snap: unknown) {
  // The socket is opened after an awaited WS-token fetch now, so wait for it to
  // be constructed before driving it.
  await waitFor(() => expect(MockWebSocket.instances.length).toBeGreaterThan(0));
  const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
  act(() => {
    ws.fireOpen();
    ws.fireMessage(snap);
  });
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('KioskPage', () => {
  // The core no longer renders a state WORD (its LED colour conveys the state);
  // the live state is exposed as `data-core-state` on the core group.
  const coreState = () =>
    document.querySelector('[data-core-state]')?.getAttribute('data-core-state');

  it('renders the fullscreen kiosk with wordmark, telemetry and rings', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle'));
    await waitFor(() => {
      expect(screen.getAllByText('RENFIELD').length).toBeGreaterThan(0);
    });
    // ambient telemetry corner (content-free counts): both satellites online
    expect(screen.getByText('2/2 online')).toBeInTheDocument();
    // tools: 1 healthy of 2 (radio down)
    expect(screen.getByText('1/2 gesund')).toBeInTheDocument();
    const svg = document.querySelector('svg[role="img"]') as SVGElement;
    expect(svg).toBeTruthy();
  });

  it('renders the internal subsystem nodes with their real health, and reacts to a delta', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(
      snapshot('idle', {
        internal_health: [
          { id: 'presence', health: 'degraded', impaired_code: 'presence_satellite_unauthenticated' },
          { id: 'knowledge', health: 'healthy', impaired_code: null },
          // disabled-by-config is 'off' (muted), NOT 'down' (red/outage)
          { id: 'media', health: 'off', impaired_code: 'media_disabled' },
        ],
      }),
    );
    const health = (id: string) =>
      document.querySelector(`[data-tool-id="${id}"]`)?.getAttribute('data-tool-health');
    await waitFor(() => {
      // no longer the permanent gray 'unknown' — each carries a real verdict
      expect(health('presence')).toBe('degraded');
    });
    expect(health('knowledge')).toBe('healthy');
    expect(health('media')).toBe('off');
    // the localized impaired reason renders as the node's <title> tooltip
    expect(
      document.querySelector('[data-tool-id="presence"] title')?.textContent,
    ).toBeTruthy();

    // a pushed internal_health_changed delta re-colours the node live
    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    act(() => {
      ws.fireMessage({
        type: 'internal_health_changed',
        subsystems: [{ id: 'presence', health: 'healthy', impaired_code: null }],
      });
    });
    await waitFor(() => {
      expect(health('presence')).toBe('healthy');
    });
  });

  it('drives the core into a voice state from a listening satellite', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('listening'));
    await waitFor(() => {
      expect(coreState()).toBe('listening');
    });
    expect(screen.getAllByText(/Wohnzimmer/).length).toBeGreaterThan(0);
  });

  it('shows the ready core when every satellite is idle', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle'));
    await waitFor(() => {
      expect(coreState()).toBe('idle');
    });
  });

  it('drops a satellite from the roster on a satellite_offline delta (core + counts)', async () => {
    renderWithProviders(<KioskPage />);
    // 3 satellites, one of them (c) reporting 'listening'.
    await pushSnapshot(snapshot('idle', {
      satellites: [
        { satellite_id: 'a', room: 'Wohnzimmer', room_id: 1, state: 'idle' },
        { satellite_id: 'b', room: 'Wohnzimmer', room_id: 1, state: 'idle' },
        { satellite_id: 'c', room: 'Esszimmer', room_id: 2, state: 'listening' },
      ],
    }));
    await waitFor(() => expect(screen.getByText('3/3 online')).toBeInTheDocument());
    expect(coreState()).toBe('listening'); // c drives the core while online

    // the backend detects c dropped (unregister / heartbeat timeout) and pushes
    // satellite_offline → it leaves the roster, stops counting AND driving core.
    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    act(() => {
      ws.fireMessage({ type: 'satellite_offline', satellite_id: 'c', room: 'Esszimmer', room_id: 2, online: false });
    });
    await waitFor(() => expect(screen.getByText('2/2 online')).toBeInTheDocument());
    expect(coreState()).toBe('idle');
  });

  it('shows the weather tile when a reading is available', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle', {
      weather: { location: 'Musterstadt', temp: 21.4, unit: '°C', code: 0, condition: 'Klarer Himmel', high: 24, low: 13 },
    }));
    await waitFor(() => expect(screen.getByText('21°C')).toBeInTheDocument());
    expect(screen.getByText(/Klarer Himmel/)).toBeInTheDocument();
    expect(screen.getByText(/Musterstadt/)).toBeInTheDocument();
  });

  it('shows the now-playing tile for a live media session', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle', {
      now_playing: [{ room: 'Wohnzimmer', kind: 'radio', title: 'Radio Beispiel', subtitle: null, track: null, total: null }],
    }));
    await waitFor(() => expect(screen.getByText('Radio Beispiel')).toBeInTheDocument());
    expect(screen.getAllByText(/Wohnzimmer/).length).toBeGreaterThan(0);
  });

  it('surfaces a live-satellite error as busy, not a false ready', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle', {
      satellites: [{ satellite_id: 'a', room: 'Wohnzimmer', room_id: 1, state: 'error' }],
    }));
    await waitFor(() => {
      expect(coreState()).toBe('busy');
    });
  });

  it('pulses the MCP node named by a live turn_activity delta', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle'));
    await waitFor(() => expect(screen.getByText('2/2 online')).toBeInTheDocument());
    // no pulse yet
    expect(document.querySelector('[data-tool-id="homeassistant"][data-tool-active="1"]')).toBeNull();
    // a turn touches Home Assistant → its node lights up
    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    act(() => {
      ws.fireMessage({ type: 'turn_activity', role: 'smart_home', subsystems: ['homeassistant'], ok: true, at: new Date().toISOString() });
    });
    await waitFor(() =>
      expect(document.querySelector('[data-tool-id="homeassistant"][data-tool-active="1"]')).not.toBeNull(),
    );
  });

  it('renders synthetic internal-subsystem nodes and pulses them (knowledge)', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle'));
    await waitFor(() => expect(screen.getByText('2/2 online')).toBeInTheDocument());
    // the knowledge/presence/media pseudo-nodes are always present (no MCP server)
    expect(document.querySelector('[data-tool-id="knowledge"]')).not.toBeNull();
    expect(document.querySelector('[data-tool-id="presence"]')).not.toBeNull();
    expect(document.querySelector('[data-tool-id="media"]')).not.toBeNull();
    // idle until named — and synthetic nodes don't inflate the tool-health count
    expect(document.querySelector('[data-tool-id="knowledge"][data-tool-active="1"]')).toBeNull();
    expect(screen.getByText('1/2 gesund')).toBeInTheDocument(); // still 2 MCP tools, not 5
    // an internal.knowledge_search turn → subsystems:['knowledge'] lights it
    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    act(() => {
      ws.fireMessage({ type: 'turn_activity', role: 'knowledge', subsystems: ['knowledge'], ok: true, at: new Date().toISOString() });
    });
    await waitFor(() =>
      expect(document.querySelector('[data-tool-id="knowledge"][data-tool-active="1"]')).not.toBeNull(),
    );
  });

  it('drives the core to processing on a chat_activity delta (typed web-chat)', async () => {
    renderWithProviders(<KioskPage />);
    await pushSnapshot(snapshot('idle')); // all satellites idle, no voice activity
    await waitFor(() => expect(coreState()).toBe('idle'));
    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    // a web-chat turn starts processing → the core thinks even with no satellite
    act(() => {
      ws.fireMessage({ type: 'chat_activity', active: true });
    });
    await waitFor(() => expect(coreState()).toBe('processing'));
    // turn ends → core returns to idle
    act(() => {
      ws.fireMessage({ type: 'chat_activity', active: false });
    });
    await waitFor(() => expect(coreState()).toBe('idle'));
  });

  it('places the clock tile on the board, fed by the device clock alone', async () => {
    renderWithProviders(<KioskPage />);
    // Not while booting: the tile belongs to the constellation's composition.
    expect(screen.queryByTestId('kiosk-clock')).toBeNull();
    await pushSnapshot(snapshot('idle'));
    const clock = await screen.findByTestId('kiosk-clock');
    expect(clock.tagName).toBe('TIME');
    // The snapshot is stamped 2026-07-04; the tile must show the DEVICE's time,
    // not anything carried by the socket.
    const shown = new Date(clock.getAttribute('dateTime') ?? '').getTime();
    expect(Math.abs(shown - Date.now())).toBeLessThan(2 * 60_000);
    // Exactly one clock — the tile replaced the former inline readout.
    expect(screen.getAllByTestId('kiosk-clock')).toHaveLength(1);
  });

  describe('a paired instance has ONE node, on the peers arc', () => {
    const peerNode = (id: number | string) => document.querySelector(`[data-peer-id="${id}"]`);
    const lastSocket = () => MockWebSocket.instances[MockWebSocket.instances.length - 1];

    /** The check is ON: the backend sends the verdict of its signed round trip. */
    const probedPeer = (over: Record<string, unknown> = {}) => ({
      id: 3,
      name: 'alex',
      instance_name: 'Instance B',
      last_seen_at: null,
      reachable: true,
      reachability: 'reachable',
      reachability_reason: null,
      checked_at: minutesAgo(1),
      recently_active: false,
      ...over,
    });

    it('labels the node with the instance first and the person second', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer()] }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      const texts = [...peerNode(3)!.querySelectorAll('text')].map((el) => el.textContent);
      expect(texts).toEqual(['Instance B', 'alex']);
    });

    it('shows the person alone for a peer without an instance label', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', {
        peers: [{ id: 3, name: 'alex', last_seen_at: minutesAgo(1), reachable: true }],
      }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      const texts = [...peerNode(3)!.querySelectorAll('text')].map((el) => el.textContent);
      expect(texts).toEqual(['alex']);
      // The check is off: the old two-valued light, and no reachability tooltip.
      expect(peerNode(3)!.getAttribute('data-peer-reachability')).toBe('reachable');
      expect(peerNode(3)!.querySelector('title')).toBeNull();
    });

    it('keeps the label of a single peer inside the screen', async () => {
      // One peer sits at the very top of the arc (y = 20 of 1080). A label
      // ABOVE it is cut off by the screen edge -- and since the peer has no
      // second node any more, that label is the only place its name appears.
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer()] }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      for (const text of peerNode(3)!.querySelectorAll('text')) {
        const baseline = Number(text.getAttribute('y'));
        const size = Number(text.getAttribute('font-size'));
        expect(baseline - size).toBeGreaterThanOrEqual(0);
      }
    });

    it.each([
      ['reachable', null, 'erreichbar'],
      ['unreachable', 'pin_mismatch', 'nicht erreichbar — Das Zertifikat der Instanz ist nicht mehr das bei der Kopplung gemerkte.'],
      ['unknown', 'unsupported', 'Erreichbarkeit unbekannt — Die Instanz beantwortet keine Erreichbarkeitsprüfung (ältere Version oder dort abgeschaltet).'],
      ['unknown', 'not_checked', 'Erreichbarkeit unbekannt — Noch nicht geprüft.'],
    ])('shows the state %s (%s) on the node and says it in words', async (state, reason, words) => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', {
        peers: [probedPeer({
          reachability: state,
          reachability_reason: reason,
          reachable: state === 'reachable',
          checked_at: reason === 'not_checked' ? null : minutesAgo(1),
        })],
      }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      expect(peerNode(3)!.getAttribute('data-peer-reachability')).toBe(state);
      expect(peerNode(3)!.querySelector('title')?.textContent).toBe(words);
    });

    it.each([
      ['reachable', null, null],
      ['unreachable', '8 5', null],
      ['unknown', '1 6', 'round'],
    ])('draws the state %s in the ring\u2019s LINE, not in its colour alone', async (state, dash, cap) => {
      // WCAG 1.4.1: the state rides two channels. This pins the second one --
      // without it the line could go and only the data attribute would notice.
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', {
        peers: [probedPeer({ reachability: state, reachable: state === 'reachable' })],
      }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      const ring = peerNode(3)!.querySelector('[data-peer-ring]')!;
      expect(ring.getAttribute('stroke-dasharray')).toBe(dash);
      expect(ring.getAttribute('stroke-linecap')).toBe(cap);
    });

    it('explains the three ring lines in the legend while the check is on', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer()] }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      const rows = [...screen.getByTestId('kiosk-peer-legend').querySelectorAll('[data-peer-legend]')];
      expect(rows.map((row) => row.getAttribute('data-peer-legend'))).toEqual(
        ['reachable', 'unreachable', 'unknown']);
      expect(rows.map((row) => row.textContent)).toEqual(
        ['Instanz erreichbar', 'Instanz nicht erreichbar', 'Instanz: unbekannt']);
      // Each swatch is a ring in its state's own line.
      expect(rows.map((row) => (row.querySelector('span') as HTMLElement).style.borderStyle)).toEqual(
        ['solid', 'dashed', 'dotted']);
    });

    it('leaves "unknown" out of the legend while the check is off, and shows none without peers', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', {
        peers: [{ id: 3, name: 'alex', last_seen_at: minutesAgo(1), reachable: true }],
      }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      const states = () => [...document.querySelectorAll('[data-peer-legend]')]
        .map((row) => row.getAttribute('data-peer-legend'));
      expect(states()).toEqual(['reachable', 'unreachable']);

      act(() => { lastSocket().fireMessage({ type: 'peer_status_changed', peers: [] }); });
      await waitFor(() => expect(peerNode(3)).toBeNull());
      expect(screen.queryByTestId('kiosk-peer-legend')).toBeNull();
    });

    it('clips a long name on the wall and keeps the full one as tooltip TEXT only', async () => {
      const long = 'Instance-With-A-Very-Long-Name-That-Its-Owner-Chose-0123456789-ab';
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer({ instance_name: long })] }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      const node = peerNode(3)!;
      const primary = node.querySelector('[data-peer-label="primary"]')!.textContent!;
      expect(Array.from(primary)).toHaveLength(22);
      expect(primary.endsWith('…')).toBe(true);
      expect(node.querySelector('[data-peer-label="secondary"]')!.textContent).toBe('alex');
      // The full name is there for whoever points at the node ...
      expect(node.querySelector('title')!.textContent).toBe(`${long} · alex — erreichbar`);
      // ... and in no attribute: it is text the far side chose.
      for (const el of [node, ...node.querySelectorAll('*')]) {
        for (const attr of el.attributes) expect(attr.value).not.toContain(long);
      }
    });

    it('lights the one node of a group whichever account\u2019s pairing the turn ran through', async () => {
      // Two accounts here are paired with the same far instance: one node,
      // two tool servers. The turn names the ASKING account's `peer_<id>`.
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', {
        peers: [probedPeer({ pulse_ids: ['peer_3', 'peer_8'] }), probedPeer({ id: 31 })],
      }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      expect(peerNode(8)).toBeNull();
      act(() => {
        lastSocket().fireMessage({
          type: 'turn_activity', role: 'general', subsystems: ['peer_8'], ok: true,
          at: new Date().toISOString(),
        });
      });
      await waitFor(() => expect(peerNode(3)!.getAttribute('data-peer-active')).toBe('1'));
      expect(peerNode(31)!.getAttribute('data-peer-active')).toBeNull();
    });

    it('measures the age of a check on the server\u2019s clock, not the tablet\u2019s', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer()] }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      const serverNow = Date.now() - 25 * 60_000;   // this tablet runs 25 minutes fast
      const naive = (ms: number) => new Date(ms).toISOString().replace('Z', '');

      // Checked one minute ago BY THE SERVER'S CLOCK: a real outage must show.
      act(() => {
        lastSocket().fireMessage({
          type: 'peer_status_changed',
          at: new Date(serverNow).toISOString().replace('Z', '+00:00'),
          peers: [probedPeer({
            reachability: 'unreachable', reachability_reason: 'pin_mismatch', reachable: false,
            checked_at: naive(serverNow - 60_000),
          })],
        });
      });
      await waitFor(() =>
        expect(peerNode(3)?.getAttribute('data-peer-reachability')).toBe('unreachable'));

      // Checked 21 minutes ago by the server's clock: old, and it says so.
      act(() => {
        lastSocket().fireMessage({
          type: 'peer_status_changed',
          at: new Date(serverNow).toISOString().replace('Z', '+00:00'),
          peers: [probedPeer({ checked_at: naive(serverNow - 21 * 60_000) })],
        });
      });
      await waitFor(() =>
        expect(peerNode(3)?.getAttribute('data-peer-reachability')).toBe('unknown'));
      expect(peerNode(3)!.querySelector('title')?.textContent).toBe(
        'Erreichbarkeit unbekannt — Geprüft, aber das Ergebnis ist veraltet.');
    });

    it('never renders a raw reason code it has no text for', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', {
        peers: [probedPeer({
          reachability: 'unreachable', reachability_reason: 'some_future_code', reachable: false,
        })],
      }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      expect(peerNode(3)!.querySelector('title')?.textContent).toBe('nicht erreichbar');
    });

    it('marks a recently active peer, in the tooltip too', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer({ recently_active: true })] }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      expect(peerNode(3)!.getAttribute('data-peer-recent')).toBe('1');
      expect(peerNode(3)!.querySelector('title')?.textContent).toBe('erreichbar · kürzlich aktiv');
    });

    it('follows a peer_status_changed delta live', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer()] }));
      await waitFor(() =>
        expect(peerNode(3)?.getAttribute('data-peer-reachability')).toBe('reachable'));
      act(() => {
        lastSocket().fireMessage({
          type: 'peer_status_changed',
          peers: [probedPeer({
            reachability: 'unreachable', reachability_reason: 'unreachable', reachable: false,
          })],
        });
      });
      await waitFor(() =>
        expect(peerNode(3)?.getAttribute('data-peer-reachability')).toBe('unreachable'));
    });

    it('lights the ARC node when a turn asked the peer; the pulse itself creates no tool node', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', { peers: [probedPeer()] }));
      await waitFor(() => expect(peerNode(3)).not.toBeNull());
      expect(peerNode(3)!.getAttribute('data-peer-active')).toBeNull();
      const toolNodesBefore = document.querySelectorAll('[data-tool-id]').length;

      act(() => {
        lastSocket().fireMessage({
          type: 'turn_activity', role: 'general', subsystems: ['peer_3'], ok: true,
          at: new Date().toISOString(),
        });
      });
      await waitFor(() => expect(peerNode(3)!.getAttribute('data-peer-active')).toBe('1'));
      // What this proves: a PULSE naming `peer_3` does not materialise a node
      // on the tools ring and moves no count. That the backend keeps a peer's
      // server out of `mcp.servers` in the first place is the backend's
      // doing and is tested there (tests/backend/test_kiosk_peer_node.py);
      // this fixture never contained such a server.
      expect(document.querySelector('[data-tool-id="peer_3"]')).toBeNull();
      expect(document.querySelectorAll('[data-tool-id]')).toHaveLength(toolNodesBefore);
      expect(screen.getByText('1/2 gesund')).toBeInTheDocument();
    });

    it('lights only the peer that was asked', async () => {
      renderWithProviders(<KioskPage />);
      await pushSnapshot(snapshot('idle', {
        peers: [probedPeer(), probedPeer({ id: 31, instance_name: 'Instance C' })],
      }));
      await waitFor(() => expect(peerNode(31)).not.toBeNull());
      act(() => {
        lastSocket().fireMessage({
          type: 'turn_activity', role: 'general', subsystems: ['peer_3'], ok: true,
          at: new Date().toISOString(),
        });
      });
      await waitFor(() => expect(peerNode(3)!.getAttribute('data-peer-active')).toBe('1'));
      // `peer_3` must not light peer 31 (a prefix is not a match).
      expect(peerNode(31)!.getAttribute('data-peer-active')).toBeNull();
    });

  });

  it('does not duplicate a synthetic node when a real MCP server owns its id', async () => {
    renderWithProviders(<KioskPage />);
    // an operator adds an output-provider MCP server literally named 'media'
    await pushSnapshot(snapshot('idle', {
      mcp: {
        enabled: true,
        total_tools: 5,
        servers: [{ name: 'media', connected: true, transport: 'stdio', tool_count: 5 }],
      },
    }));
    await waitFor(() => expect(document.querySelector('[data-tool-id="media"]')).not.toBeNull());
    // the real server wins — exactly one 'media' node, no duplicate React key
    expect(document.querySelectorAll('[data-tool-id="media"]')).toHaveLength(1);
    // knowledge / presence still get their synthetic nodes
    expect(document.querySelector('[data-tool-id="knowledge"]')).not.toBeNull();
  });
});
