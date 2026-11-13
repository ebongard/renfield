import { describe, expect, it } from 'vitest';

import { toolServerLabel } from '../../../../src/frontend/src/components/kiosk/useKioskModel';

const peers = [
  { id: 3, name: 'Office' },
  { id: 12, name: '  Workshop  ' },
  { id: 20, name: '   ' },
];

describe('toolServerLabel', () => {
  it("labels a peer's tool server with the peer's display name", () => {
    expect(toolServerLabel('peer_3', peers)).toBe('Office');
  });

  it('accepts the id as the number the backend sends or as a string', () => {
    expect(toolServerLabel('peer_3', [{ id: '3', name: 'Office' }])).toBe('Office');
  });

  it('matches the whole id, not a prefix of it', () => {
    // peer_1 must not pick up peer 12, and peer_12 must not pick up peer 1.
    expect(toolServerLabel('peer_1', peers)).toBe('Peer 1');
    expect(toolServerLabel('peer_12', peers)).toBe('Workshop');
  });

  it('falls back to the server name for a peer the snapshot does not list', () => {
    expect(toolServerLabel('peer_7', peers)).toBe('Peer 7');
    expect(toolServerLabel('peer_3', [])).toBe('Peer 3');
  });

  it('falls back when the peer has a blank display name', () => {
    expect(toolServerLabel('peer_20', peers)).toBe('Peer 20');
  });

  it('leaves every other server name alone', () => {
    expect(toolServerLabel('searxng', peers)).toBe('SearXNG');
    expect(toolServerLabel('home_assistant', peers)).toBe('Home Assistant');
    // Only the exact `peer_<digits>` shape is a peer's tool server.
    expect(toolServerLabel('peer_x', peers)).toBe('Peer X');
    expect(toolServerLabel('peer_3_backup', peers)).toBe('Peer 3 Backup');
    expect(toolServerLabel('my_peer_3', peers)).toBe('My Peer 3');
  });
});
