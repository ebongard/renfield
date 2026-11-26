/**
 * PresenceCalibration — tab "Calibration" on /admin/presence (#1551 stage 2 B).
 * The socket hook is a fixture; the routes are MSW. Covers: the receiver
 * table, the guided measurement (start → countdown → evaluate → proposal →
 * save, nothing saved before "Save"), an uncalibrated receiver needing a typed
 * cutoff, coded refusals rendered as translated text, and the two-step remove.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import PresenceCalibration, {
  MEASUREMENT_SECONDS,
  countMeasurementReadings,
} from '../../../../src/frontend/src/components/presence/PresenceCalibration';
import type {
  PresenceReadingsSnapshot,
  PresenceReadingsState,
} from '../../../../src/frontend/src/hooks/usePresenceReadings';
import type { Calibration } from '../../../../src/frontend/src/api/resources/presence';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithProviders } from '../test-utils';

const hookState = vi.hoisted(() => ({
  current: { snapshot: null, status: 'connecting' } as PresenceReadingsState,
}));

vi.mock('../../../../src/frontend/src/hooks/usePresenceReadings', () => ({
  usePresenceReadings: () => hookState.current,
}));

const SINCE = 1_800_000_000;

function snapshot(readings = 12): PresenceReadingsSnapshot {
  return {
    now: SINCE + 60,
    enabled: true,
    settings: {
      rssi_floor_dbm: -80, switch_enter_margin_db: 8, stale_timeout_s: 120,
      rssi_filter_enabled: true, voice_hold_s: 60, voice_release_margin_db: 10,
    },
    satellites: [
      { satellite_id: 'sat-kueche', room_id: 10, room_name: 'Küche' },
      { satellite_id: 'sat-neu', room_id: 20, room_name: 'Büro' },
    ],
    devices: [{
      key: 'irk:0a1b2c3d', user_id: 1, user_name: 'alice',
      current_room_id: null, current_room_name: null, current_confidence: null,
      voice_hold_remaining_s: null, voice_pinned: false, location_uncertain: false,
      filtered_rooms: [],
      sightings: [
        { satellite_id: 'sat-kueche', room_id: 10, room_name: 'Küche', rssi: -70,
          timestamp: SINCE - 5, age_s: 65 },
        ...Array.from({ length: readings }, (_, i) => ({
          satellite_id: 'sat-kueche', room_id: 10, room_name: 'Küche', rssi: -70 + i,
          timestamp: SINCE + 1 + i * 4, age_s: 59 - i * 4,
        })),
      ],
    }],
  };
}

function calibration(overrides: Partial<Calibration> = {}): Calibration {
  return {
    source: 'database',
    settings_ignored: true,
    engine_active: true,
    receivers: [{ satellite_id: 'sat-kueche', offset_db: 0, in_room_dbm: -47.4, cutoff_dbm: -100 }],
    zones: [],
    min_samples: 10,
    in_room_percentile: 10,
    ...overrides,
  };
}

let saved: unknown[] = [];

beforeEach(() => {
  saved = [];
  hookState.current = { snapshot: snapshot(), status: 'live' };
  server.use(
    http.get(`${BASE_URL}/api/presence/calibration`, () => HttpResponse.json(calibration())),
    http.post(`${BASE_URL}/api/presence/calibration/measurement`, () => HttpResponse.json({ since: SINCE })),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

async function chooseAndStart(user: ReturnType<typeof userEvent.setup>, satellite: string) {
  await user.selectOptions(await screen.findByLabelText('Gerät'), 'irk:0a1b2c3d');
  await user.selectOptions(screen.getByLabelText('Empfänger'), satellite);
  await user.click(screen.getByRole('button', { name: 'Messung starten' }));
}

describe('countMeasurementReadings', () => {
  it('counts this receiver’s readings of this device since the start, once per instant', () => {
    expect(countMeasurementReadings(snapshot(), 'irk:0a1b2c3d', 'sat-kueche', SINCE)).toBe(12);
    expect(countMeasurementReadings(snapshot(), 'irk:0a1b2c3d', 'sat-neu', SINCE)).toBe(0);
    expect(countMeasurementReadings(snapshot(), 'irk:other', 'sat-kueche', SINCE)).toBe(0);
    expect(countMeasurementReadings(null, 'irk:0a1b2c3d', 'sat-kueche', SINCE)).toBe(0);
  });
});

describe('PresenceCalibration', () => {
  it('lists the calibrated receivers and says the JSON settings are ignored', async () => {
    renderWithProviders(<PresenceCalibration />);
    const row = await screen.findByTestId('calibrated-sat-kueche');
    expect(row).toHaveTextContent('Küche');
    expect(row).toHaveTextContent('-47,4');
    expect(screen.getByText('Die JSON-Einstellungen sind gesetzt, werden aber ignoriert')).toBeInTheDocument();
  });

  it('runs the guided measurement and saves only on confirmation', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let proposalBody: unknown = null;
    server.use(
      http.post(`${BASE_URL}/api/presence/calibration/proposal`, async ({ request }) => {
        proposalBody = await request.json();
        return HttpResponse.json({
          satellite_id: 'sat-kueche', samples: 12, in_room_dbm: -68.9,
          min_dbm: -70, median_dbm: -64.5, max_dbm: -59, cutoff_dbm: -100, offset_db: 0,
          current: { satellite_id: 'sat-kueche', offset_db: 0, in_room_dbm: -47.4, cutoff_dbm: -100 },
        });
      }),
      http.put(`${BASE_URL}/api/presence/calibration/receivers/sat-kueche`, async ({ request }) => {
        saved.push(await request.json());
        return HttpResponse.json(calibration({
          receivers: [{ satellite_id: 'sat-kueche', offset_db: 0, in_room_dbm: -68.9, cutoff_dbm: -100 }],
        }));
      }),
    );
    renderWithProviders(<PresenceCalibration />);
    await chooseAndStart(user, 'sat-kueche');

    const evaluate = await screen.findByRole('button', { name: 'Auswerten' });
    expect(evaluate).toBeDisabled();
    expect(screen.getByTestId('measurement-progress')).toHaveTextContent('12 Messwerte von Küche');

    await act(async () => { vi.advanceTimersByTime(MEASUREMENT_SECONDS * 1000); });
    expect(evaluate).toBeEnabled();
    await user.click(evaluate);

    const proposal = await screen.findByTestId('proposal');
    expect(proposalBody).toEqual({ device_key: 'irk:0a1b2c3d', satellite_id: 'sat-kueche', since: SINCE });
    expect(proposal).toHaveTextContent('Bisher: Raumgrenze -47,4 dBm');
    expect(within(proposal).getByLabelText('Raumgrenze (dBm)')).toHaveValue('-68.9');
    expect(saved).toEqual([]);

    await user.click(within(proposal).getByRole('button', { name: 'Speichern' }));
    await vi.waitFor(() => expect(saved).toEqual([{ in_room_dbm: -68.9, cutoff_dbm: -100, offset_db: 0 }]));
    expect(await screen.findByTestId('calibrated-sat-kueche')).toHaveTextContent('-68,9');
    expect(screen.queryByTestId('proposal')).not.toBeInTheDocument();
  });

  it('asks for the cutoff of an uncalibrated receiver instead of inventing one', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    server.use(
      http.post(`${BASE_URL}/api/presence/calibration/proposal`, () =>
        HttpResponse.json({
          satellite_id: 'sat-neu', samples: 11, in_room_dbm: -62, min_dbm: -66,
          median_dbm: -60, max_dbm: -55, cutoff_dbm: null, offset_db: 0, current: null,
        })),
    );
    renderWithProviders(<PresenceCalibration />);
    await chooseAndStart(user, 'sat-neu');
    await act(async () => { vi.advanceTimersByTime(MEASUREMENT_SECONDS * 1000); });
    await user.click(await screen.findByRole('button', { name: 'Auswerten' }));

    const proposal = await screen.findByTestId('proposal');
    expect(proposal).toHaveTextContent('Dieser Empfänger ist noch nicht kalibriert.');
    const save = within(proposal).getByRole('button', { name: 'Speichern' });
    expect(within(proposal).getByLabelText('Schnittgrenze (dBm)')).toHaveValue('');
    expect(save).toBeDisabled();
    await user.type(within(proposal).getByLabelText('Schnittgrenze (dBm)'), '-80');
    expect(save).toBeEnabled();
  });

  it('renders a coded refusal as translated text', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    server.use(
      http.post(`${BASE_URL}/api/presence/calibration/proposal`, () =>
        HttpResponse.json({ detail: { code: 'too_few_readings', samples: 3, needed: 10 } }, { status: 422 })),
    );
    renderWithProviders(<PresenceCalibration />);
    await chooseAndStart(user, 'sat-kueche');
    await act(async () => { vi.advanceTimersByTime(MEASUREMENT_SECONDS * 1000); });
    await user.click(await screen.findByRole('button', { name: 'Auswerten' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Nur 3 Messwerte dieses Geräts von diesem Empfänger, mindestens 10 nötig.',
    );
  });

  it('removes a calibration only after the second click', async () => {
    const user = userEvent.setup();
    let deleted = 0;
    server.use(
      http.delete(`${BASE_URL}/api/presence/calibration/receivers/sat-kueche`, () => {
        deleted += 1;
        return HttpResponse.json(calibration({ receivers: [] }));
      }),
    );
    renderWithProviders(<PresenceCalibration />);
    await user.click(await screen.findByRole('button', { name: 'Kalibrierung von sat-kueche entfernen' }));
    expect(deleted).toBe(0);
    await user.click(screen.getByRole('button', { name: 'Wirklich entfernen' }));
    await vi.waitFor(() => expect(deleted).toBe(1));
    expect(await screen.findByText(/Noch kein Empfänger ist kalibriert/)).toBeInTheDocument();
  });
});
