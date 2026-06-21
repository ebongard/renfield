/**
 * artifactSchema (zod, authoritative shape) + the ChatContext streaming-merge
 * helper (same-id keyed append, idempotent re-delivery, out-of-order).
 */
import { describe, it, expect } from 'vitest';
import {
  parseArtifact,
  type ChatArtifact,
} from '../../../../src/frontend/src/components/chat/artifacts/artifactSchema';
import { mergeArtifactFrame } from '../../../../src/frontend/src/pages/ChatPage/context/ChatContext';
import type { ChatArtifactPayload } from '../../../../src/frontend/src/types/chat';

describe('artifactSchema — authoritative shape validation', () => {
  it('accepts a well-formed table/list/keyvalue/chart', () => {
    expect(parseArtifact({ id: 't', kind: 'table', data: { columns: ['A'], rows: [['1']] } })).not.toBeNull();
    expect(parseArtifact({ id: 'l', kind: 'list', data: { items: ['a'] } })).not.toBeNull();
    expect(parseArtifact({ id: 'k', kind: 'keyvalue', data: { pairs: [{ key: 'k', value: 'v' }] } })).not.toBeNull();
    expect(parseArtifact({ id: 'c', kind: 'chart', data: { chartType: 'bar', series: [{ label: 's', points: [{ x: 0, y: 1 }] }] } })).not.toBeNull();
  });

  it('accepts a well-formed weather widget (minimal + full)', () => {
    expect(parseArtifact({
      id: 'w', kind: 'weather',
      data: { location: 'Berlin', current: { temp: 18, unit: '°C', code: 3, condition: 'Bedeckt' } },
    })).not.toBeNull();
    expect(parseArtifact({
      id: 'w2', kind: 'weather', title: 'Berlin',
      data: {
        location: 'Berlin',
        current: { temp: 18, unit: '°C', code: 61, condition: 'Regen', feelsLike: 16, humidity: 80, windSpeed: 12, high: 20, low: 11 },
        forecast: [{ date: '2026-06-21', code: 1, high: 22, low: 12, condition: 'Heiter', precipChance: 10 }],
      },
    })).not.toBeNull();
  });

  it('accepts a device_control widget (incl. brightness + climate fields)', () => {
    expect(parseArtifact({
      id: 'dc', kind: 'device_control',
      data: { devices: [
        { entity_id: 'light.wz', domain: 'light', name: 'Licht', state: 'on', room: 'WZ', brightness: 70 },
        { entity_id: 'scene.abend', domain: 'scene', name: 'Abend', state: 'x' },
        { entity_id: 'climate.wz', domain: 'climate', name: 'Heizung', state: 'heat', currentTemp: 19, targetTemp: 21, minTemp: 5, maxTemp: 30, tempStep: 0.5 },
      ] },
    })).not.toBeNull();
    // devices must be an array of the device shape.
    expect(parseArtifact({ id: 'dc', kind: 'device_control', data: { devices: 'no' } })).toBeNull();
  });

  it('accepts a presence_map widget', () => {
    expect(parseArtifact({
      id: 'pm', kind: 'presence_map',
      data: { rooms: [{ room: 'WZ', users: ['Eduard'] }, { room: 'Küche', users: [] }] },
    })).not.toBeNull();
    expect(parseArtifact({ id: 'pm', kind: 'presence_map', data: { rooms: 'no' } })).toBeNull();
  });

  it('rejects a weather widget missing current or with a non-finite temp', () => {
    expect(parseArtifact({ id: 'w', kind: 'weather', data: { location: 'Berlin' } })).toBeNull();
    expect(parseArtifact({
      id: 'w', kind: 'weather',
      data: { location: 'Berlin', current: { temp: NaN, unit: '°C', code: 0, condition: 'x' } },
    })).toBeNull();
  });

  it('rejects unknown kind, missing id, wrong data shape', () => {
    expect(parseArtifact({ id: 'x', kind: 'html', data: {} })).toBeNull();
    expect(parseArtifact({ kind: 'list', data: { items: [] } })).toBeNull();
    expect(parseArtifact({ id: 'x', kind: 'table', data: { columns: 'no', rows: 1 } })).toBeNull();
    expect(parseArtifact(null)).toBeNull();
    expect(parseArtifact('a string')).toBeNull();
  });

  it('rejects NaN / Infinity chart coordinates (viewBox DoS guard)', () => {
    expect(parseArtifact({ id: 'c', kind: 'chart', data: { chartType: 'line', series: [{ label: 's', points: [{ x: 0, y: NaN }] }] } })).toBeNull();
    expect(parseArtifact({ id: 'c', kind: 'chart', data: { chartType: 'line', series: [{ label: 's', points: [{ x: Infinity, y: 1 }] }] } })).toBeNull();
  });

  it('rejects non-string cells (no implicit coercion on the client)', () => {
    // The backend coerces numbers→strings before emit; the client schema is
    // strict, so a number cell that slipped through → fallback.
    expect(parseArtifact({ id: 't', kind: 'table', data: { columns: ['n'], rows: [[42]] } })).toBeNull();
  });
});

const table = (id: string, rows: string[][]): ChatArtifactPayload => ({
  id, kind: 'table', data: { columns: ['A', 'B'], rows },
});

describe('mergeArtifactFrame — streaming (decision 1)', () => {
  it('a new id appends to the array', () => {
    const out = mergeArtifactFrame(undefined, table('t', [['1', '2']]));
    expect(out.map((a) => a.id)).toEqual(['t']);
  });

  it('a turn can carry multiple artifacts (table + chart) keyed by id', () => {
    let arr = mergeArtifactFrame(undefined, table('t', [['1', '2']]));
    arr = mergeArtifactFrame(arr, { id: 'c', kind: 'chart', data: { chartType: 'bar', series: [] } });
    expect(arr.map((a) => a.id)).toEqual(['t', 'c']);
  });

  it('same-id table frames APPEND rows (not replace)', () => {
    let arr = mergeArtifactFrame(undefined, table('t', [['1', '2']]));
    arr = mergeArtifactFrame(arr, table('t', [['3', '4']]));
    const rows = (arr[0].data as { rows: string[][] }).rows;
    expect(rows).toEqual([['1', '2'], ['3', '4']]);
  });

  it('same-id list frames APPEND items', () => {
    let arr = mergeArtifactFrame(undefined, { id: 'l', kind: 'list', data: { items: ['a'] } });
    arr = mergeArtifactFrame(arr, { id: 'l', kind: 'list', data: { items: ['b', 'c'] } });
    expect((arr[0].data as { items: string[] }).items).toEqual(['a', 'b', 'c']);
  });

  it('idempotent re-delivery of the same trailing chunk does NOT double-append', () => {
    let arr = mergeArtifactFrame(undefined, table('t', [['1', '2']]));
    arr = mergeArtifactFrame(arr, table('t', [['3', '4']]));
    // re-deliver the exact same second chunk
    arr = mergeArtifactFrame(arr, table('t', [['3', '4']]));
    const rows = (arr[0].data as { rows: string[][] }).rows;
    expect(rows).toEqual([['1', '2'], ['3', '4']]);
  });

  it('keyvalue / chart same-id frames REPLACE (only table/list append)', () => {
    let arr = mergeArtifactFrame(undefined, { id: 'c', kind: 'chart', data: { chartType: 'bar', series: [{ label: 'a', points: [] }] } });
    arr = mergeArtifactFrame(arr, { id: 'c', kind: 'chart', data: { chartType: 'line', series: [{ label: 'b', points: [] }] } });
    expect((arr[0].data as { chartType: string }).chartType).toBe('line');
    expect(arr.length).toBe(1);
  });

  it('the finalize/partial flag rides on the latest frame so the renderer can resolve it', () => {
    let arr = mergeArtifactFrame(undefined, { ...table('t', [['1', '2']]), partial: true });
    arr = mergeArtifactFrame(arr, { ...table('t', [['3', '4']]), partial: false });
    const final = arr[0] as ChatArtifactPayload & { partial?: boolean };
    expect(final.partial).toBe(false);
    expect((final.data as { rows: string[][] }).rows).toEqual([['1', '2'], ['3', '4']]);
  });
});

describe('parseArtifact return type', () => {
  it('narrows kind for the dispatcher', () => {
    const a = parseArtifact({ id: 'l', kind: 'list', data: { items: ['x'] } }) as ChatArtifact;
    expect(a.kind).toBe('list');
  });
});
