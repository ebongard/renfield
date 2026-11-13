/**
 * Where the peers and their labels stand on the kiosk's outer arc -- as
 * numbers, without rendering. A peer's name is chosen by the FAR instance (up
 * to 64 characters) and the wall cannot scroll: the label is clipped, and the
 * nodes are placed so the clipped labels neither collide nor leave the screen.
 *
 * jsdom does not lay out SVG text, so a label's width is ESTIMATED here, on
 * the generous side (0.62 em per character). The estimate is the test's own;
 * the code under test knows nothing of it.
 */
import { describe, expect, it } from 'vitest';

import {
  KIOSK_VIEW,
  PEER_ARC,
  PEER_LABEL_FONT,
  PEER_LABEL_MAX_CHARS,
  PEER_RING_DASH,
  clipPeerLabel,
  layoutPeerArc,
  peerAngles,
  type PeerPlacement,
} from '../../../../../src/frontend/src/components/kiosk/peerArc';

const LONG_INSTANCE = 'Instance-With-A-Very-Long-Name-That-Its-Owner-Chose-0123456789-ab'; // 64
const LONG_PERSON = 'A Person With A Rather Long Display Name';

interface Box { left: number; right: number; top: number; bottom: number }

/** The rectangle a placed label covers: both lines, estimated width. */
function labelBox(place: PeerPlacement): Box {
  const width = (text: string, font: number) => Array.from(text).length * font * 0.62;
  const lines = [
    { text: place.primary, font: PEER_LABEL_FONT.primary, baseline: place.label.primaryY },
    ...(place.secondary
      ? [{ text: place.secondary, font: PEER_LABEL_FONT.secondary, baseline: place.label.secondaryY }]
      : []),
  ];
  const w = Math.max(...lines.map((l) => width(l.text, l.font)));
  const left = place.label.anchor === 'middle' ? place.label.x - w / 2
    : place.label.anchor === 'end' ? place.label.x - w : place.label.x;
  return {
    left,
    right: left + w,
    top: Math.min(...lines.map((l) => l.baseline - l.font)),
    bottom: Math.max(...lines.map((l) => l.baseline + l.font * 0.3)),
  };
}

/** The node's own ring, pulse included (r = 9, stroke 2.5). */
const nodeBox = (place: PeerPlacement): Box => ({
  left: place.x - 11, right: place.x + 11, top: place.y - 11, bottom: place.y + 11,
});

const overlap = (a: Box, b: Box): boolean =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

const peersOf = (n: number) =>
  Array.from({ length: n }, () => ({ label: LONG_INSTANCE, sublabel: LONG_PERSON }));

describe('clipPeerLabel', () => {
  it('leaves a short name alone', () => {
    expect(clipPeerLabel('büro-süd')).toBe('büro-süd');
    expect(clipPeerLabel('x'.repeat(PEER_LABEL_MAX_CHARS))).toBe('x'.repeat(PEER_LABEL_MAX_CHARS));
  });

  it('clips a long one to the limit, the ellipsis included', () => {
    const clipped = clipPeerLabel(LONG_INSTANCE);
    expect(Array.from(clipped)).toHaveLength(PEER_LABEL_MAX_CHARS);
    expect(clipped.endsWith('…')).toBe(true);
    expect(LONG_INSTANCE.startsWith(clipped.slice(0, -1))).toBe(true);
  });

  it('counts characters, not UTF-16 units, and leaves no space before the ellipsis', () => {
    const astral = '𝔘'.repeat(40); // each is two UTF-16 units
    const clipped = clipPeerLabel(astral);
    expect(Array.from(clipped)).toHaveLength(PEER_LABEL_MAX_CHARS);
    expect(clipped).not.toMatch(/[\uD800-\uDBFF]…$/); // no half character
    expect(clipPeerLabel(`${'a'.repeat(PEER_LABEL_MAX_CHARS - 2)} bcdef`)).toBe(
      `${'a'.repeat(PEER_LABEL_MAX_CHARS - 2)}…`,
    );
  });
});

describe('peerAngles', () => {
  it('is symmetric, ordered left to right, and stays inside the arc', () => {
    for (let n = 1; n <= 7; n += 1) {
      const angles = peerAngles(n);
      expect(angles).toHaveLength(n);
      expect([...angles].sort((a, b) => a - b)).toEqual(angles);
      expect(Math.max(...angles.map(Math.abs))).toBeLessThanOrEqual(PEER_ARC.spread);
      angles.forEach((a, i) => expect(a).toBeCloseTo(-angles[n - 1 - i], 9));
    }
    expect(peerAngles(0)).toEqual([]);
  });

  it('keeps the positions one, two and three peers have always had', () => {
    expect(peerAngles(1)).toEqual([0]);
    expect(peerAngles(2)).toEqual([-50, 50]);
    expect(peerAngles(3)).toEqual([-50, 0, 50]);
  });
});

describe('layoutPeerArc — nothing collides, nothing leaves the screen', () => {
  // Six is what the arc holds with full-length labels; from seven on the outer
  // nodes of a side stand closer than a label is wide.
  it.each([1, 2, 3, 4, 5, 6])('%i peer(s) with the longest names', (n) => {
    const places = layoutPeerArc(peersOf(n));
    const labels = places.map(labelBox);

    labels.forEach((box) => {
      expect(box.left).toBeGreaterThanOrEqual(0);
      expect(box.right).toBeLessThanOrEqual(KIOSK_VIEW.width);
      expect(box.top).toBeGreaterThanOrEqual(0);
      expect(box.bottom).toBeLessThanOrEqual(KIOSK_VIEW.height);
    });
    for (let i = 0; i < n; i += 1) {
      for (let j = 0; j < n; j += 1) {
        if (i === j) continue;
        if (j > i) expect(overlap(labels[i], labels[j]), `labels ${i} and ${j}`).toBe(false);
        expect(overlap(labels[i], nodeBox(places[j])), `label ${i} over node ${j}`).toBe(false);
      }
    }
  });

  it('puts the label of a single peer beside the node, not above the screen edge', () => {
    const [only] = layoutPeerArc([{ label: 'Instance B', sublabel: 'alex' }]);
    expect(only.y).toBeCloseTo(PEER_ARC.cy - PEER_ARC.radius, 6);
    expect(only.label.anchor).toBe('start');
    expect(only.label.x).toBeGreaterThan(only.x);
    expect(only.label.primaryY - PEER_LABEL_FONT.primary).toBeGreaterThanOrEqual(0);
  });

  it('puts the labels of two peers above their nodes', () => {
    const places = layoutPeerArc(peersOf(2));
    places.forEach((place) => {
      expect(place.label.anchor).toBe('middle');
      expect(place.label.secondaryY).toBeLessThan(place.y);
    });
  });

  it('gives the top node of five room: its neighbours start clear of its label', () => {
    // 🛑 The regression: at an even 25° the neighbour's centred label ran
    // into the beside-label of the node at the top.
    const places = layoutPeerArc(peersOf(5));
    const top = places[2];
    expect(top.label.anchor).toBe('start');
    expect(overlap(labelBox(top), labelBox(places[3]))).toBe(false);
    expect(labelBox(places[3]).top).toBeGreaterThan(labelBox(top).bottom);
  });

  it('reports what it clipped, and draws the full text nowhere', () => {
    const [long, short] = layoutPeerArc([
      { label: LONG_INSTANCE, sublabel: 'alex' },
      { label: 'Instance B', sublabel: 'alex' },
    ]);
    expect(long.clipped).toBe(true);
    expect(long.primary).not.toBe(LONG_INSTANCE);
    expect(long.secondary).toBe('alex');
    expect(short.clipped).toBe(false);
    expect(short.primary).toBe('Instance B');
    // A long PERSON alone is clipped as well.
    expect(layoutPeerArc([{ label: 'Instance B', sublabel: LONG_PERSON }])[0].clipped).toBe(true);
    expect(layoutPeerArc([{ label: 'alex' }])[0].secondary).toBeUndefined();
  });
});

describe('PEER_RING_DASH — the second channel next to colour', () => {
  it('gives each state a line of its own', () => {
    expect(PEER_RING_DASH.reachable).toBeUndefined();
    const dash = (state: 'unreachable' | 'unknown') =>
      (PEER_RING_DASH[state] ?? '').split(' ').map(Number);
    const [downOn, downOff] = dash('unreachable');
    const [unknownOn, unknownOff] = dash('unknown');
    // Long dashes against dots: the drawn part differs by a wide margin, and
    // a dot's gap is several times its length.
    expect(downOn).toBeGreaterThanOrEqual(4 * unknownOn);
    expect(unknownOff).toBeGreaterThanOrEqual(4 * unknownOn);
    expect(downOn).toBeGreaterThan(downOff);
  });
});
