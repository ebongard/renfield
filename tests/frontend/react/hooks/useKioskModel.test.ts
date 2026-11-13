/**
 * The arc node of a paired instance: its label, its three-valued reachability
 * and the id a tool call lights it with. `peerNodeFor` is the pure rule; the
 * rendering is covered in pages/KioskPage.test.tsx.
 */
import { describe, expect, it } from 'vitest';

import { peerAgeMs, peerNodeFor } from '../../../../src/frontend/src/components/kiosk/useKioskModel';
import {
  parseServerMs,
  type KioskPeer,
  type KioskPeerClock,
} from '../../../../src/frontend/src/components/kiosk/useKioskSocket';

const NOW = Date.parse('2026-11-13T12:00:00Z');
/** A naive-UTC stamp `minutes` before NOW, the shape the backend sends. */
const ago = (minutes: number): string =>
  new Date(NOW - minutes * 60_000).toISOString().replace('Z', '');

/** A peer as the backend sends it with the reachability check OFF. */
const legacy = (over: Partial<KioskPeer> = {}): KioskPeer => ({
  id: 3,
  name: 'alex',
  last_seen_at: ago(1),
  reachable: true,
  ...over,
});

/** The same peer with the check ON. */
const probed = (over: Partial<KioskPeer> = {}): KioskPeer => ({
  ...legacy(),
  reachability: 'reachable',
  reachability_reason: null,
  checked_at: ago(1),
  recently_active: false,
  ...over,
});

describe('peerNodeFor — the label', () => {
  it('shows the person alone when the instance has no label', () => {
    const node = peerNodeFor(legacy(), NOW);
    expect(node.label).toBe('alex');
    expect(node.sublabel).toBeUndefined();
  });

  it('puts the instance first and the person second', () => {
    const node = peerNodeFor(legacy({ instance_name: 'Instance B' }), NOW);
    expect(node.label).toBe('Instance B');
    expect(node.sublabel).toBe('alex');
  });

  it('keeps a hyphen and non-ASCII letters as they are', () => {
    expect(peerNodeFor(legacy({ instance_name: 'büro-süd' }), NOW).label).toBe('büro-süd');
  });

  it('treats a blank instance name as none', () => {
    const node = peerNodeFor(legacy({ instance_name: '   ' }), NOW);
    expect(node.label).toBe('alex');
    expect(node.sublabel).toBeUndefined();
  });

  it('does not say the same name twice', () => {
    const node = peerNodeFor(legacy({ instance_name: 'alex' }), NOW);
    expect(node.label).toBe('alex');
    expect(node.sublabel).toBeUndefined();
  });
});

describe('peerNodeFor — the pulse ids', () => {
  it('is the subsystem id a turn_activity names for this peer', () => {
    // The backend sends the row id as a JSON number.
    expect(peerNodeFor(legacy({ id: 3 }), NOW).pulseIds).toEqual(['peer_3']);
    expect(peerNodeFor(legacy({ id: '12' }), NOW).pulseIds).toEqual(['peer_12']);
    expect(peerNodeFor(legacy({ id: 3 }), NOW).id).toBe('3');
  });

  it('takes every pairing of a group when several accounts share one far instance', () => {
    // A turn runs through the ASKING account's pairing -- any of them lights the node.
    const node = peerNodeFor(legacy({ id: 3, pulse_ids: ['peer_3', 'peer_8'] }), NOW);
    expect(node.pulseIds).toEqual(['peer_3', 'peer_8']);
    expect(node.id).toBe('3');
  });

  it('falls back to its own id when the list is empty or not a list of names', () => {
    expect(peerNodeFor(legacy({ id: 3, pulse_ids: [] }), NOW).pulseIds).toEqual(['peer_3']);
    const junk = { ...legacy({ id: 3 }), pulse_ids: [7, '', null] } as unknown as KioskPeer;
    expect(peerNodeFor(junk, NOW).pulseIds).toEqual(['peer_3']);
  });
});

describe('parseServerMs', () => {
  it('reads a naive-UTC stamp as UTC and leaves a zoned one alone', () => {
    const ms = Date.parse('2026-11-13T12:00:00Z');
    expect(parseServerMs('2026-11-13T12:00:00')).toBe(ms);
    expect(parseServerMs('2026-11-13T12:00:00Z')).toBe(ms);
    // The snapshot's own `at` carries an offset; appending a `Z` broke it.
    expect(parseServerMs('2026-11-13T12:00:00.123456+00:00')).toBe(ms + 123);
    expect(parseServerMs('2026-11-13T13:00:00+01:00')).toBe(ms);
  });

  it('never falls back to the device clock', () => {
    expect(parseServerMs(null)).toBeNull();
    expect(parseServerMs('')).toBeNull();
    expect(parseServerMs('not a time')).toBeNull();
  });
});

describe('peerNodeFor — check off (no `reachability` in the payload)', () => {
  it('keeps the rule the wall always had: asked us recently AND seen recently', () => {
    expect(peerNodeFor(legacy(), NOW).reachability).toBe('reachable');
    expect(peerNodeFor(legacy({ reachable: false }), NOW).reachability).toBe('unreachable');
  });

  it('applies the last-seen backstop behind a lost delta', () => {
    // `reachable: true` frozen by a dropped socket, last seen 11 minutes ago.
    expect(peerNodeFor(legacy({ last_seen_at: ago(11) }), NOW).reachability).toBe('unreachable');
    expect(peerNodeFor(legacy({ last_seen_at: ago(9) }), NOW).reachability).toBe('reachable');
    expect(peerNodeFor(legacy({ last_seen_at: null }), NOW).reachability).toBe('unreachable');
  });

  it('is two-valued: it never says unknown, and never recently active', () => {
    const node = peerNodeFor(legacy({ reachable: false, last_seen_at: null }), NOW);
    expect(node.reachability).toBe('unreachable');
    expect(node.recentlyActive).toBe(false);
    expect(node.reason).toBeUndefined();
  });
});

describe('peerNodeFor — check on', () => {
  it('takes the verdict of the signed round trip', () => {
    expect(peerNodeFor(probed(), NOW).reachability).toBe('reachable');
    const down = peerNodeFor(
      probed({ reachability: 'unreachable', reachability_reason: 'pin_mismatch', reachable: false }),
      NOW,
    );
    expect(down.reachability).toBe('unreachable');
    expect(down.reason).toBe('pin_mismatch');
  });

  it('shows unknown as its own state, with its reason', () => {
    const node = peerNodeFor(
      probed({ reachability: 'unknown', reachability_reason: 'unsupported', reachable: false }),
      NOW,
    );
    expect(node.reachability).toBe('unknown');
    expect(node.reason).toBe('unsupported');
  });

  it('does NOT gate a reachable peer on when it last asked us', () => {
    // A ping does not move `last_seen_at`: a reachable instance that has not
    // asked anything for a day must not turn red.
    for (const lastSeen of [null, ago(24 * 60)]) {
      const node = peerNodeFor(probed({ last_seen_at: lastSeen }), NOW);
      expect(node.reachability).toBe('reachable');
    }
  });

  it('degrades a verdict nobody refreshed to unknown, never to unreachable', () => {
    const stale = peerNodeFor(probed({ checked_at: ago(21) }), NOW);
    expect(stale.reachability).toBe('unknown');
    // "Checked, but the result is old" -- not the same as never checked.
    expect(stale.reason).toBe('stale');
    // ... and equally for a red one: an old "unreachable" is no finding either.
    const staleDown = peerNodeFor(
      probed({ reachability: 'unreachable', reachability_reason: 'unreachable', checked_at: ago(21) }),
      NOW,
    );
    expect(staleDown.reachability).toBe('unknown');
    expect(staleDown.reason).toBe('stale');
    // Inside the window the verdict stands.
    expect(peerNodeFor(probed({ checked_at: ago(19) }), NOW).reachability).toBe('reachable');
  });

  it('does not stand behind a verdict without a check time', () => {
    const node = peerNodeFor(probed({ checked_at: null }), NOW);
    expect(node.reachability).toBe('unknown');
    expect(node.reason).toBe('not_checked');
  });

  it('keeps "not checked yet" as it came', () => {
    const node = peerNodeFor(
      probed({ reachability: 'unknown', reachability_reason: 'not_checked', checked_at: null, reachable: false }),
      NOW,
    );
    expect(node.reachability).toBe('unknown');
    expect(node.reason).toBe('not_checked');
  });

  it('reads a state this build does not know as unknown, not as a fault', () => {
    const node = peerNodeFor(probed({ reachability: 'degraded' }), NOW);
    expect(node.reachability).toBe('unknown');
  });

  it('passes recently-active through, and only a real true', () => {
    expect(peerNodeFor(probed({ recently_active: true }), NOW).recentlyActive).toBe(true);
    expect(peerNodeFor(probed({ recently_active: undefined }), NOW).recentlyActive).toBe(false);
  });
});

describe('the age of a check does not depend on the device clock', () => {
  // The SERVER's clock at the push, and a check it made one minute before.
  const SERVER_AT = NOW;
  const checked = ago(1);
  /** A display whose own clock is off by `skewMin`, `sinceMin` after receipt. */
  const view = (skewMin: number, sinceMin: number) => {
    const receivedAt = SERVER_AT + skewMin * 60_000;
    const clock: KioskPeerClock = { receivedAt, serverAt: SERVER_AT };
    return { clock, now: receivedAt + sinceMin * 60_000 };
  };

  it.each([
    ['right', 0],
    ['25 minutes fast', 25],
    ['25 minutes slow', -25],
    ['a day fast', 24 * 60],
  ])('reads the same age on a tablet whose clock is %s', (_label, skew) => {
    const { clock, now } = view(skew, 3);
    // One minute on the server's clock, three on the device's own.
    expect(peerAgeMs(checked, now, clock)).toBe(4 * 60_000);
    expect(peerNodeFor(probed({ checked_at: checked }), now, clock).reachability).toBe('reachable');
  });

  it('does NOT hide a peer behind "unknown" on a tablet running 25 minutes fast', () => {
    // 🛑 The regression: device time minus server time made every check look
    // 26 minutes old, for ever -- and a real outage read "not checked".
    const { clock, now } = view(25, 0);
    const down = peerNodeFor(
      probed({ reachability: 'unreachable', reachability_reason: 'pin_mismatch', checked_at: checked }),
      now, clock,
    );
    expect(down.reachability).toBe('unreachable');
    expect(down.reason).toBe('pin_mismatch');
  });

  it('does NOT keep an old verdict alive on a tablet running 25 minutes slow', () => {
    const { clock, now } = view(-25, 21);
    const node = peerNodeFor(probed({ checked_at: checked }), now, clock);
    expect(node.reachability).toBe('unknown');
    expect(node.reason).toBe('stale');
  });

  it('turns stale by the passage of time on the device alone (a silent socket)', () => {
    expect(peerNodeFor(probed({ checked_at: checked }), view(25, 18).now, view(25, 18).clock)
      .reachability).toBe('reachable');
    expect(peerNodeFor(probed({ checked_at: checked }), view(25, 19).now, view(25, 19).clock)
      .reason).toBe('stale');
  });

  it('counts the age the reading already had on the server', () => {
    const { clock, now } = view(0, 5);
    // Checked 16 minutes before the push, 5 more on the wall: 21.
    expect(peerNodeFor(probed({ checked_at: ago(16) }), now, clock).reason).toBe('stale');
  });

  it('applies the same measure to the last-seen backstop with the check off', () => {
    const fast = view(25, 0);
    expect(peerNodeFor(legacy({ last_seen_at: ago(1) }), fast.now, fast.clock).reachability)
      .toBe('reachable');
    const slow = view(-25, 11);
    expect(peerNodeFor(legacy({ last_seen_at: ago(1) }), slow.now, slow.clock).reachability)
      .toBe('unreachable');
  });

  it('compares the two clocks directly only when the server sent no stamp', () => {
    const clock: KioskPeerClock = { receivedAt: NOW, serverAt: null };
    expect(peerAgeMs(ago(2), NOW, clock)).toBe(2 * 60_000);
    expect(peerAgeMs(ago(2), NOW)).toBe(2 * 60_000);
    expect(peerAgeMs('garbage', NOW, clock)).toBeNaN();
  });
});
