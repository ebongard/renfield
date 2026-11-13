/**
 * LiveReadings — the "Readings" tab on /admin/presence. The socket hook is
 * replaced by a fixture; this file covers what the view makes of a snapshot:
 * per-satellite rows and their statistics, connected satellites that did not
 * hear the device, the per-room take-over threshold, and the empty, disabled
 * and reconnecting states.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';

import LiveReadings, {
  isPinnedWithoutReading,
  summarizeRooms,
  summarizeSatellites,
} from '../../../../src/frontend/src/components/presence/LiveReadings';
import type {
  PresenceReadingDevice,
  PresenceReadingsSnapshot,
  PresenceReadingsState,
} from '../../../../src/frontend/src/hooks/usePresenceReadings';
import { formatTime } from '../../../../src/frontend/src/utils/datetime';
import { renderWithProviders } from '../test-utils';

const hookState = vi.hoisted(() => ({
  current: { snapshot: null, status: 'connecting' } as PresenceReadingsState,
}));

vi.mock('../../../../src/frontend/src/hooks/usePresenceReadings', () => ({
  usePresenceReadings: () => hookState.current,
}));

const NOW = 1_800_000_000;

function sighting(satellite: string, roomId: number, room: string, rssi: number, age: number) {
  return {
    satellite_id: satellite, room_id: roomId, room_name: room,
    rssi, timestamp: NOW - age, age_s: age,
  };
}

function device(overrides: Partial<PresenceReadingDevice> = {}): PresenceReadingDevice {
  return {
    key: 'irk:0a1b2c3d',
    user_id: 1,
    user_name: 'alice',
    current_room_id: 10,
    current_room_name: 'Küche',
    current_confidence: 0.714,
    voice_hold_remaining_s: null,
    voice_pinned: false,
    location_uncertain: false,
    filtered_rooms: [
      { room_id: 20, room_name: 'Flur', rssi: -66.5 },
      { room_id: 10, room_name: 'Küche', rssi: -58 },
    ],
    sightings: [
      sighting('sat-kueche', 10, 'Küche', -60, 9),
      sighting('sat-kueche', 10, 'Küche', -50, 6),
      sighting('sat-kueche', 10, 'Küche', -55, 3),
      sighting('sat-flur', 20, 'Flur', -84, 2),
    ],
    ...overrides,
  };
}

function snapshot(overrides: Partial<PresenceReadingsSnapshot> = {}): PresenceReadingsSnapshot {
  return {
    now: NOW,
    enabled: true,
    settings: {
      rssi_floor_dbm: -80,
      switch_enter_margin_db: 8,
      stale_timeout_s: 120,
      rssi_filter_enabled: true,
      voice_hold_s: 60,
      voice_release_margin_db: 10,
    },
    satellites: [
      { satellite_id: 'sat-kueche', room_id: 10, room_name: 'Küche' },
      { satellite_id: 'sat-flur', room_id: 20, room_name: 'Flur' },
      { satellite_id: 'sat-bad', room_id: 30, room_name: 'Bad' },
    ],
    devices: [device()],
    ...overrides,
  };
}

beforeEach(() => {
  hookState.current = { snapshot: snapshot(), status: 'live' };
});

describe('summarizeSatellites', () => {
  it('folds the readings per satellite and appends the silent ones', () => {
    const rows = summarizeSatellites(device(), snapshot().satellites);
    expect(rows.map((r) => r.satelliteId)).toEqual(['sat-kueche', 'sat-flur', 'sat-bad']);
    const kitchen = rows[0];
    expect(kitchen).toMatchObject({
      heard: true, latest: -55, mean: -55, min: -60, max: -50, count: 3, ageS: 3,
    });
    expect(kitchen.series.map((p) => p.rssi)).toEqual([-60, -50, -55]);
    expect(rows[2]).toMatchObject({ heard: false, latest: null, count: 0, roomName: 'Bad' });
  });

  it('keeps a satellite that reported but has left the roster', () => {
    const rows = summarizeSatellites(device(), []);
    expect(rows.map((r) => r.satelliteId)).toEqual(['sat-kueche', 'sat-flur']);
  });
});

describe('summarizeRooms', () => {
  it('marks the current room and computes what a challenger needs', () => {
    const rooms = summarizeRooms(device(), snapshot().settings);
    expect(rooms.map((r) => r.roomId)).toEqual([10, 20]);
    expect(rooms[0]).toMatchObject({ isCurrent: true, needs: null, missing: null, basis: null });
    // current -58 + margin 8 = -50; the hall sits at -66.5 → 16.5 dB short.
    expect(rooms[1]).toMatchObject({
      isCurrent: false, needs: -50, missing: 16.5, basis: 'enter',
    });
  });

  it('uses floor + release margin for a pinned room the device was never heard in', () => {
    const pinned = device({
      current_room_id: 99, current_room_name: 'Büro', voice_pinned: true,
    });
    expect(isPinnedWithoutReading(pinned, snapshot().settings)).toBe(true);
    const rooms = summarizeRooms(pinned, snapshot().settings);
    // floor -80 + release margin 10 = -70, for every challenger.
    expect(rooms.map((r) => [r.roomId, r.needs, r.basis, r.missing])).toEqual([
      [10, -70, 'release', -12],
      [20, -70, 'release', -3.5],
    ]);
  });

  it('keeps the enter margin when the pinned room does have a filtered value', () => {
    const pinned = device({ voice_pinned: true });
    expect(isPinnedWithoutReading(pinned, snapshot().settings)).toBe(false);
    expect(summarizeRooms(pinned, snapshot().settings)[1]).toMatchObject({
      needs: -50, basis: 'enter',
    });
  });

  it('has no release threshold when the release margin is switched off', () => {
    const base = snapshot().settings;
    const rooms = summarizeRooms(
      device({ current_room_id: 99, voice_pinned: true }),
      { ...base, voice_release_margin_db: 0 },
    );
    expect(rooms.every((r) => r.needs === null)).toBe(true);
  });

  it('has no threshold when the current room has no filtered value', () => {
    const rooms = summarizeRooms(device({ current_room_id: 99 }), snapshot().settings);
    expect(rooms.every((r) => !r.isCurrent && r.needs === null)).toBe(true);
  });
});

describe('LiveReadings', () => {
  it('shows who, where and with which confidence', () => {
    renderWithProviders(<LiveReadings />);
    expect(screen.getByRole('heading', { name: 'alice' })).toBeInTheDocument();
    expect(screen.getByText('irk:0a1b2c3d')).toBeInTheDocument();
    expect(screen.getByTestId('placement')).toHaveTextContent('Verortet in Küche · Konfidenz 71 %');
    expect(screen.getByText('Live')).toBeInTheDocument();
  });

  it('shows the local time of the snapshot', () => {
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText(`Stand ${formatTime(NOW * 1000, 'de')}`)).toBeInTheDocument();
  });

  it('renders one row per satellite with its statistics', () => {
    renderWithProviders(<LiveReadings />);
    const row = within(screen.getByTestId('satellite-sat-kueche'));
    const cells = row.getAllByRole('cell').map((c) => c.textContent);
    expect(cells.slice(0, 7)).toEqual([
      'sat-kueche', 'Küche', '-55', '-55', '-60 / -50', '3', 'vor 3 s',
    ]);
    expect(
      row.getByRole('img', { name: 'Signalverlauf an sat-kueche über die letzten 120 s' }),
    ).toBeInTheDocument();
  });

  it('labels a reading below the floor in words, not only in colour', () => {
    renderWithProviders(<LiveReadings />);
    const hall = within(screen.getByTestId('satellite-sat-flur'));
    expect(hall.getByText('unter Schwelle')).toBeInTheDocument();
    const kitchen = within(screen.getByTestId('satellite-sat-kueche'));
    expect(kitchen.queryByText('unter Schwelle')).not.toBeInTheDocument();
  });

  it('lists a connected satellite that did not hear the device', () => {
    renderWithProviders(<LiveReadings />);
    const row = within(screen.getByTestId('satellite-sat-bad'));
    expect(row.getByText('Bad')).toBeInTheDocument();
    expect(row.getByText(/nicht gehört/)).toBeInTheDocument();
    expect(row.queryByRole('img')).not.toBeInTheDocument();
  });

  it('shows the filtered value per room and the take-over threshold', () => {
    renderWithProviders(<LiveReadings />);
    const current = within(screen.getByTestId('room-10'));
    expect(current.getByText('-58')).toBeInTheDocument();
    expect(current.getByText('aktueller Raum')).toBeInTheDocument();
    const challenger = within(screen.getByTestId('room-20'));
    expect(challenger.getByText('-66,5')).toBeInTheDocument();
    expect(
      challenger.getByText('braucht ≥ -50 dBm für die Übernahme (es fehlen 16,5 dB)'),
    ).toBeInTheDocument();
    expect(challenger.queryByText('aktueller Raum')).not.toBeInTheDocument();
  });

  it('explains the three numbers the readings are judged by', () => {
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText(/unter -80 dBm zählen nicht/)).toHaveTextContent(
      /um 8 dB übertrifft.*letzten 120 s/,
    );
  });

  it('announces only the connection status, never the ticking timestamp', () => {
    renderWithProviders(<LiveReadings />);
    const live = document.querySelectorAll('[aria-live]');
    expect(live).toHaveLength(1);
    expect(live[0]).toHaveTextContent(/^Live$/);
    expect(live[0]).toBe(screen.getByRole('status'));
    const stamp = screen.getByText(`Stand ${formatTime(NOW * 1000, 'de')}`);
    expect(stamp.closest('[aria-live]')).toBeNull();
  });

  it('shows no voice badge for a user Bluetooth placed', () => {
    renderWithProviders(<LiveReadings />);
    expect(screen.queryByText(/per Stimme gehalten/)).not.toBeInTheDocument();
    expect(screen.queryByText('gehaltene Vermutung')).not.toBeInTheDocument();
    expect(screen.queryByText('Raum aus Sprachsichtung')).not.toBeInTheDocument();
    expect(screen.queryByTestId('pinned-note')).not.toBeInTheDocument();
  });

  it('shows the running voice hold and does not promise a switch', () => {
    hookState.current = {
      status: 'live',
      snapshot: snapshot({
        devices: [device({
          voice_hold_remaining_s: 41.2,
          voice_pinned: true,
          // The hall has met the margin: -58 + 8 = -50 ≤ -45.
          filtered_rooms: [
            { room_id: 20, room_name: 'Flur', rssi: -45 },
            { room_id: 10, room_name: 'Küche', rssi: -58 },
          ],
        })],
      }),
    };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText('per Stimme gehalten, noch 42 s')).toBeInTheDocument();
    const challenger = within(screen.getByTestId('room-20'));
    expect(
      challenger.getByText(
        'Schwelle von -50 dBm erreicht, doch der Sprach-Halt sperrt den Wechsel noch 42 s',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/übernimmt mit der nächsten Messung/)).not.toBeInTheDocument();
    expect(screen.queryByText('Raum aus Sprachsichtung')).not.toBeInTheDocument();
  });

  it('says "margin met" once no hold is running', () => {
    hookState.current = {
      status: 'live',
      snapshot: snapshot({
        devices: [device({
          filtered_rooms: [
            { room_id: 20, room_name: 'Flur', rssi: -45 },
            { room_id: 10, room_name: 'Küche', rssi: -58 },
          ],
        })],
      }),
    };
    renderWithProviders(<LiveReadings />);
    expect(
      within(screen.getByTestId('room-20')).getByText(
        'Schwelle von -50 dBm erreicht – übernimmt mit der nächsten Messung',
      ),
    ).toBeInTheDocument();
  });

  it('marks a held guess', () => {
    hookState.current = {
      status: 'live',
      snapshot: snapshot({
        devices: [device({ voice_pinned: true, location_uncertain: true })],
      }),
    };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText('gehaltene Vermutung')).toBeInTheDocument();
    expect(screen.queryByText(/per Stimme gehalten/)).not.toBeInTheDocument();
    expect(screen.queryByText('Raum aus Sprachsichtung')).not.toBeInTheDocument();
  });

  it('marks a room that still rests on a voice sighting after the hold', () => {
    hookState.current = {
      status: 'live',
      snapshot: snapshot({ devices: [device({ voice_pinned: true })] }),
    };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText('Raum aus Sprachsichtung')).toBeInTheDocument();
  });

  it('computes the threshold from the release margin for a pinned, unheard room', () => {
    hookState.current = {
      status: 'live',
      snapshot: snapshot({
        devices: [device({
          current_room_id: 99, current_room_name: 'Büro', voice_pinned: true,
          filtered_rooms: [
            { room_id: 20, room_name: 'Flur', rssi: -76.5 },
            { room_id: 10, room_name: 'Küche', rssi: -58 },
          ],
        })],
      }),
    };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByTestId('pinned-note')).toHaveTextContent(
      /Büro stammt aus einer Sprach- oder Anmeldesichtung.*erst ab -70 dBm/,
    );
    expect(
      within(screen.getByTestId('room-20')).getByText(
        'braucht ≥ -70 dBm für die Übernahme (es fehlen 6,5 dB)',
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId('room-10')).getByText(
        'Schwelle von -70 dBm erreicht – übernimmt mit der nächsten Messung',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText('aktueller Raum')).not.toBeInTheDocument();
  });

  it('explains the voice hold and the release threshold in the legend', () => {
    renderWithProviders(<LiveReadings />);
    const legend = screen.getByText(/unter -80 dBm zählen nicht/);
    expect(legend).toHaveTextContent(/bleibt der Raum 60 s lang fest/);
    expect(legend).toHaveTextContent(/mindestens -70 dBm/);
  });

  it('leaves the voice sentences out of the legend when both are switched off', () => {
    const base = snapshot();
    hookState.current = {
      status: 'live',
      snapshot: {
        ...base,
        settings: { ...base.settings, voice_hold_s: 0, voice_release_margin_db: 0 },
      },
    };
    renderWithProviders(<LiveReadings />);
    const legend = screen.getByText(/unter -80 dBm zählen nicht/);
    expect(legend).not.toHaveTextContent(/Sprach/);
  });

  it('shows access denied instead of reconnecting', () => {
    hookState.current = { status: 'denied', snapshot: null };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByRole('status')).toHaveTextContent('Kein Zugriff');
    expect(screen.getByText('Kein Zugriff auf die Messwerte')).toBeInTheDocument();
    expect(screen.getByText(/Administratoren vorbehalten/)).toBeInTheDocument();
    expect(screen.queryByText(/Verbindung wird/)).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('says so when a device is not placed in any room', () => {
    hookState.current = {
      status: 'live',
      snapshot: snapshot({
        devices: [device({
          current_room_id: null, current_room_name: null,
          current_confidence: null, filtered_rooms: [],
        })],
      }),
    };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByTestId('placement')).toHaveTextContent('Derzeit keinem Raum zugeordnet');
    expect(screen.getByText('Noch keine geglätteten Werte.')).toBeInTheDocument();
  });

  it('says so when the RSSI filter is off', () => {
    const base = snapshot();
    hookState.current = {
      status: 'live',
      snapshot: { ...base, settings: { ...base.settings, rssi_filter_enabled: false } },
    };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText(/RSSI-Filter ist abgeschaltet/)).toBeInTheDocument();
  });

  it('has an empty state when no tracked device was heard', () => {
    hookState.current = { status: 'live', snapshot: snapshot({ devices: [] }) };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText('Kein erfasstes Gerät gehört')).toBeInTheDocument();
    expect(screen.getByText(/Keiner der 3 verbundenen Satelliten/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('has a disabled state', () => {
    hookState.current = {
      status: 'live',
      snapshot: snapshot({ enabled: false, devices: [], satellites: [] }),
    };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText(/PRESENCE_ENABLED=false/)).toBeInTheDocument();
    expect(screen.queryByText(/zählen nicht/)).not.toBeInTheDocument();
  });

  it('shows connecting before the first snapshot', () => {
    hookState.current = { status: 'connecting', snapshot: null };
    renderWithProviders(<LiveReadings />);
    expect(screen.getAllByText('Verbindung wird aufgebaut').length).toBeGreaterThan(0);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('keeps the last readings while reconnecting and marks them as stale', () => {
    hookState.current = { status: 'reconnecting', snapshot: snapshot() };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText('Verbindung wird wiederhergestellt')).toBeInTheDocument();
    expect(screen.getByText(/letzte bekannte Stand/)).toBeInTheDocument();
    expect(screen.getByTestId('satellite-sat-kueche')).toBeInTheDocument();
    expect(screen.queryByText('Live')).not.toBeInTheDocument();
  });

  it('shows reconnecting when no snapshot ever arrived', () => {
    hookState.current = { status: 'reconnecting', snapshot: null };
    renderWithProviders(<LiveReadings />);
    expect(screen.getByText(/Server ist gerade nicht erreichbar/)).toBeInTheDocument();
  });
});
