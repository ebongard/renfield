/**
 * The arc node of a paired instance: its label, its three-valued reachability
 * and the id a tool call lights it with. `peerNodeFor` is the pure rule; the
 * rendering is covered in pages/KioskPage.test.tsx.
 */
import { describe, expect, it } from 'vitest';

import { peerNodeFor } from '../../../../src/frontend/src/components/kiosk/useKioskModel';
import type { KioskPeer } from '../../../../src/frontend/src/components/kiosk/useKioskSocket';

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

describe('peerNodeFor — the pulse id', () => {
  it('is the subsystem id a turn_activity names for this peer', () => {
    // The backend sends the row id as a JSON number.
    expect(peerNodeFor(legacy({ id: 3 }), NOW).pulseId).toBe('peer_3');
    expect(peerNodeFor(legacy({ id: '12' }), NOW).pulseId).toBe('peer_12');
    expect(peerNodeFor(legacy({ id: 3 }), NOW).id).toBe('3');
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
    expect(stale.reason).toBe('not_checked');
    // ... and equally for a red one: an old "unreachable" is no finding either.
    const staleDown = peerNodeFor(
      probed({ reachability: 'unreachable', reachability_reason: 'unreachable', checked_at: ago(21) }),
      NOW,
    );
    expect(staleDown.reachability).toBe('unknown');
    expect(staleDown.reason).toBe('not_checked');
    // Inside the window the verdict stands.
    expect(peerNodeFor(probed({ checked_at: ago(19) }), NOW).reachability).toBe('reachable');
  });

  it('does not stand behind a verdict without a check time', () => {
    expect(peerNodeFor(probed({ checked_at: null }), NOW).reachability).toBe('unknown');
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
