import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import SatellitesPage from '../../../../src/frontend/src/pages/SatellitesPage';
import { renderWithProviders } from '../test-utils';
import type { SatelliteData } from '../../../../src/frontend/src/api/resources/satellites';

/**
 * Capability badges must reflect the satellite's REAL reported hardware,
 * not the legacy hardcoded "3 LEDs" for every device. i18n test language
 * is German (test-utils sets 'de'), so assertions use the de.json strings.
 */

function mockSatellites(sat: SatelliteData) {
  server.use(
    http.get(`${BASE_URL}/api/satellites`, () =>
      HttpResponse.json({ satellites: [sat], latest_version: '1.0.0' }),
    ),
  );
}

const base = {
  state: 'idle' as const,
  uptime_seconds: 120,
  heartbeat_ago_seconds: 2,
};

describe('SatellitesPage capability badges', () => {
  it('renders the real hardware of a fully-equipped satellite', async () => {
    mockSatellites({
      ...base,
      satellite_id: 'benszimmer',
      room: 'BensZimmer',
      capabilities: {
        local_wakeword: true,
        speaker: true,
        led_count: 12,
        led_type: 'xvf3800',
        mic_count: 4,
        has_camera: true,
        has_display: true,
        has_enviro: true,
      },
    });

    renderWithProviders(<SatellitesPage />);

    // Cards render collapsed; capability badges live in the expanded body.
    await waitFor(() => {
      expect(screen.getByText('BensZimmer')).toBeInTheDocument();
    });
    await userEvent.click(screen.getByText('BensZimmer'));

    // Data-driven, not the hardcoded 3
    expect(await screen.findByText('12 LEDs')).toBeInTheDocument();
    expect(screen.getByText('4 Mikrofone')).toBeInTheDocument();
    expect(screen.getByText('Kamera')).toBeInTheDocument();
    expect(screen.getByText('Umweltsensor')).toBeInTheDocument();
    expect(screen.getByText('Wake Word')).toBeInTheDocument();
    expect(screen.getByText('Lautsprecher')).toBeInTheDocument();
    expect(screen.queryByText('3 LEDs')).not.toBeInTheDocument();
  });

  it('shows conservative badges for a legacy/minimal satellite', async () => {
    mockSatellites({
      ...base,
      satellite_id: 'old-sat',
      room: 'Wohnzimmer',
      capabilities: {
        local_wakeword: true,
        speaker: true,
        led_count: 3,
        mic_count: 1,
        has_camera: false,
        has_display: false,
        has_enviro: false,
      },
    });

    renderWithProviders(<SatellitesPage />);

    await waitFor(() => {
      expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
    });
    await userEvent.click(screen.getByText('Wohnzimmer'));

    expect(await screen.findByText('3 LEDs')).toBeInTheDocument();
    // Single-mic → no Mics badge; absent optional hardware → no badges
    expect(screen.queryByText('1 Mikrofone')).not.toBeInTheDocument();
    expect(screen.queryByText('Kamera')).not.toBeInTheDocument();
    expect(screen.queryByText('Umweltsensor')).not.toBeInTheDocument();
  });

  it('uses singular form when a satellite reports exactly one LED', async () => {
    // sat-arbeitszimmer-style: whisplay HAT has 1 status RGB LED.
    // i18next picks _one over _other for count=1 → "1 LED", not "1 LEDs".
    mockSatellites({
      ...base,
      satellite_id: 'arbeitszimmer',
      room: 'Arbeitszimmer',
      capabilities: {
        local_wakeword: true,
        speaker: true,
        led_count: 1,
        led_type: 'gpio_rgb',
        mic_count: 2,
        has_camera: true,
        has_display: true,
        has_enviro: false,
      },
    });

    renderWithProviders(<SatellitesPage />);

    await waitFor(() => {
      expect(screen.getByText('Arbeitszimmer')).toBeInTheDocument();
    });
    await userEvent.click(screen.getByText('Arbeitszimmer'));

    expect(await screen.findByText('1 LED')).toBeInTheDocument();
    expect(screen.queryByText('1 LEDs')).not.toBeInTheDocument();
    expect(screen.getByText('2 Mikrofone')).toBeInTheDocument();
    expect(screen.getByText('Kamera')).toBeInTheDocument();
    expect(screen.getByText('Display')).toBeInTheDocument();
  });
});

describe('SatellitesPage corrupt entry (#1279)', () => {
  it('flags an entry the backend could not build and never shows it as fresh', async () => {
    mockSatellites({
      state: 'error',
      satellite_id: 'bad-sat',
      room: 'Keller',
      uptime_seconds: -1,
      heartbeat_ago_seconds: -1,
      corrupt: true,
    });

    renderWithProviders(<SatellitesPage />);

    expect(await screen.findByText('Daten unlesbar')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Keller'));

    // -1 is "unknown": neither "gerade eben" nor "0s".
    expect(await screen.findAllByText('—')).toHaveLength(2);
    expect(screen.queryByText('0s')).not.toBeInTheDocument();
  });

  it('shows no flag on a healthy entry', async () => {
    mockSatellites({ ...base, satellite_id: 'good-sat', room: 'Flur' });

    renderWithProviders(<SatellitesPage />);

    expect(await screen.findByText('Flur')).toBeInTheDocument();
    expect(screen.queryByText('Daten unlesbar')).not.toBeInTheDocument();
  });
});

describe('SatellitesPage deaf satellite (#1211)', () => {
  it('flags a connected satellite whose microphone delivers nothing', async () => {
    mockSatellites({
      ...base,
      satellite_id: 'sat-wohnzimmer',
      room: 'Wohnzimmer',
      capture_stalled_seconds: 7260,
    });

    renderWithProviders(<SatellitesPage />);

    expect(await screen.findByText('Mikrofon stumm')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Wohnzimmer'));
    expect(await screen.findByRole('status')).toHaveTextContent('Pegel seit 2h 1m unverändert');
  });

  it('shows nothing when the backend does not judge it (null or absent)', async () => {
    mockSatellites({ ...base, satellite_id: 'sat-kueche', room: 'Küche', capture_stalled_seconds: null });

    renderWithProviders(<SatellitesPage />);

    expect(await screen.findByText('Küche')).toBeInTheDocument();
    expect(screen.queryByText('Mikrofon stumm')).not.toBeInTheDocument();
  });
});
