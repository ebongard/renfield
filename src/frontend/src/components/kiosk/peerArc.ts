// Geometry and line language of the PEERS arc on the kiosk -- kept apart from
// the component so placement can be tested as numbers, without rendering.
//
// A peer's label is text the FAR instance chose (up to 64 characters), drawn
// on a wall with nothing to scroll: it is clipped here, and the nodes are
// spaced so that the clipped labels of up to six peers do not run into each
// other or off the screen (from seven on a side gets too crowded for that).
import type { PeerReachability } from './types';

/** The kiosk's fixed viewBox and the arc's place in it. */
export const KIOSK_VIEW = { width: 1920, height: 1080 } as const;
export const PEER_ARC = {
  cx: KIOSK_VIEW.width / 2,
  cy: KIOSK_VIEW.height / 2,
  radius: 520,
  /** Half the arc's opening, in degrees from the top. */
  spread: 50,
} as const;

export const PEER_LABEL_FONT = { primary: 16, secondary: 13 } as const;

/** Longest label drawn, in characters, the ellipsis included. */
export const PEER_LABEL_MAX_CHARS = 22;

/** Clip a label for the wall. Counts code points, not UTF-16 units, so a name
 *  in a script outside the BMP is not cut through the middle of a character. */
export function clipPeerLabel(text: string, max: number = PEER_LABEL_MAX_CHARS): string {
  const chars = Array.from(text);
  if (chars.length <= max) return text;
  return `${chars.slice(0, max - 1).join('').trimEnd()}…`;
}

/** The ring's line per state -- the second channel next to colour (WCAG
 *  1.4.1). Chosen to be told apart from across a room: no gaps, long dashes,
 *  or dots (`unknown` is drawn with round caps, so a 1-long dash is a dot). */
export const PEER_RING_DASH: Record<PeerReachability, string | undefined> = {
  reachable: undefined,
  unreachable: '8 5',
  unknown: '1 6',
};

/** Below this angle from the top a label cannot stand above its node (it
 *  would leave the screen) and stands beside it. The first node that carries
 *  its label above must start beyond `CLEAR_OF_TOP`, or that label runs into
 *  the beside-label of the node at the top. */
const INNER_PAIR = 12;
const CLEAR_OF_TOP = 34;

/** Angles (degrees, 0 = top, negative = left) for `n` peers, left to right.
 *
 *  Up to three: evenly over the arc. From four on, the top of the arc is
 *  given room: one node (odd) or a pair (even) sits there with its label
 *  beside it, and the others share the outer part of each side. */
export function peerAngles(n: number): number[] {
  const { spread } = PEER_ARC;
  if (n <= 0) return [];
  if (n === 1) return [0];
  if (n <= 3) return Array.from({ length: n }, (_, i) => -spread + ((2 * spread) / (n - 1)) * i);
  const inner = n % 2 === 1 ? [0] : [-INNER_PAIR, INNER_PAIR];
  const perSide = (n - inner.length) / 2;
  const side = Array.from({ length: perSide }, (_, j) => (
    perSide === 1 ? spread : CLEAR_OF_TOP + ((spread - CLEAR_OF_TOP) / (perSide - 1)) * j
  ));
  return [...side.map((a) => -a).reverse(), ...inner, ...side];
}

export function peerPoint(deg: number): [number, number] {
  const a = ((deg - 90) * Math.PI) / 180;
  return [PEER_ARC.cx + PEER_ARC.radius * Math.cos(a), PEER_ARC.cy + PEER_ARC.radius * Math.sin(a)];
}

export interface PeerLabelPlace {
  x: number;
  anchor: 'start' | 'middle' | 'end';
  primaryY: number;
  secondaryY: number;
}

/** Where a peer's label goes: above the node, instance over person -- unless
 *  that would leave the viewBox. A node at the very top of the arc
 *  (y = cy - radius = 20) has no room above; there the label stands beside the
 *  node, on the outer side. */
export function peerLabelAt(x: number, y: number, lines: 1 | 2): PeerLabelPlace {
  const secondaryY = y - 18;
  const primaryY = lines === 2 ? secondaryY - 18 : secondaryY;
  // The primary line's glyphs reach its font size above the baseline.
  if (primaryY - PEER_LABEL_FONT.primary >= 0) return { x, anchor: 'middle', primaryY, secondaryY };
  const left = x < PEER_ARC.cx - 4;
  return {
    x: left ? x - 20 : x + 20,
    anchor: left ? 'end' : 'start',
    primaryY: lines === 2 ? y - 3 : y + 5,
    secondaryY: y + 15,
  };
}

export interface PeerPlacement {
  x: number;
  y: number;
  /** The clipped texts, as drawn. */
  primary: string;
  secondary?: string;
  /** True when either line was clipped: the view then offers the full text. */
  clipped: boolean;
  label: PeerLabelPlace;
}

/** Node position and label placement for every peer on the arc. */
export function layoutPeerArc(peers: readonly { label: string; sublabel?: string }[]): PeerPlacement[] {
  const angles = peerAngles(peers.length);
  return peers.map((peer, i) => {
    const [x, y] = peerPoint(angles[i]);
    const primary = clipPeerLabel(peer.label);
    const secondary = peer.sublabel ? clipPeerLabel(peer.sublabel) : undefined;
    return {
      x,
      y,
      primary,
      secondary,
      clipped: primary !== peer.label || (secondary ?? '') !== (peer.sublabel ?? ''),
      label: peerLabelAt(x, y, secondary ? 2 : 1),
    };
  });
}
