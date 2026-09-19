/**
 * RoomOutputSettings — Phase 4 generic mode (output_providers_enabled).
 *
 * When the backend returns the unified `output_targets` union, the Add dialog
 * drops the hardcoded renfield/HA/dlna type-buttons for ONE picker over every
 * provider (incl. samsung), shows capability badges, marks unreachable targets
 * disabled, and submits the (output_provider, output_target_id) pair.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders, userEvent } from '../test-utils';
import RoomOutputSettings from '../../../../src/frontend/src/components/RoomOutputSettings';
import {
  useOutputDevicesQuery,
  useAvailableOutputsQuery,
  useAddOutputDevice,
  useUpdateOutputDevice,
  useDeleteOutputDevice,
  useReorderOutputDevices,
} from '../../../../src/frontend/src/api/resources/roomOutputs';

vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn(() => ({ isFeatureEnabled: () => true })),
}));

vi.mock('../../../../src/frontend/src/api/resources/roomOutputs', async (orig) => ({
  ...(await orig<typeof import('../../../../src/frontend/src/api/resources/roomOutputs')>()),
  useOutputDevicesQuery: vi.fn(),
  useAvailableOutputsQuery: vi.fn(),
  useAddOutputDevice: vi.fn(),
  useUpdateOutputDevice: vi.fn(),
  useDeleteOutputDevice: vi.fn(),
  useReorderOutputDevices: vi.fn(),
}));

const addSpy = vi.fn().mockResolvedValue(undefined);
const updateSpy = vi.fn().mockResolvedValue(undefined);
const noopMut = { mutateAsync: vi.fn(), isPending: false };

function setAvailable(output_targets: unknown) {
  vi.mocked(useAvailableOutputsQuery).mockReturnValue({
    data: { renfield_devices: [], ha_media_players: [], dlna_renderers: [], output_targets },
    isLoading: false,
  } as unknown as ReturnType<typeof useAvailableOutputsQuery>);
}

beforeEach(() => {
  addSpy.mockClear();
  updateSpy.mockClear();
  vi.mocked(useOutputDevicesQuery).mockReturnValue(
    { data: [], isLoading: false } as unknown as ReturnType<typeof useOutputDevicesQuery>,
  );
  vi.mocked(useAddOutputDevice).mockReturnValue(
    { mutateAsync: addSpy, isPending: false } as unknown as ReturnType<typeof useAddOutputDevice>,
  );
  vi.mocked(useUpdateOutputDevice).mockReturnValue(noopMut as unknown as ReturnType<typeof useUpdateOutputDevice>);
  vi.mocked(useDeleteOutputDevice).mockReturnValue(noopMut as unknown as ReturnType<typeof useDeleteOutputDevice>);
  vi.mocked(useReorderOutputDevices).mockReturnValue(noopMut as unknown as ReturnType<typeof useReorderOutputDevices>);
});

const TARGETS = [
  { provider: 'samsung', target_id: '192.168.1.47', name: 'Living Room TV', capabilities: ['video', 'audio', 'power'], reachable: true },
  { provider: 'dlna', target_id: 'Wohnzimmer', name: 'Wohnzimmer Renderer', capabilities: ['audio', 'video'], reachable: true },
  { provider: 'samsung', target_id: '192.168.1.99', name: 'samsung (unreachable)', capabilities: ['video', 'power'], reachable: false },
];

async function openModal() {
  const user = userEvent.setup();
  renderWithProviders(<RoomOutputSettings roomId={1} roomName="Wohnzimmer" outputType="audio" />);
  await user.click(screen.getByText('Audio-Ausgabe'));            // expand section
  await user.click(screen.getByText('Audio-Ausgabegerät hinzufügen')); // open add modal
  return user;
}

describe('RoomOutputSettings generic mode', () => {
  it('shows the unified picker (no type-buttons) with all providers', async () => {
    setAvailable(TARGETS);
    await openModal();
    // type-selector is hidden in generic mode
    expect(screen.queryByText('Gerätetyp:')).toBeNull();
    // unified dropdown lists samsung + dlna targets
    expect(screen.getByRole('option', { name: /Living Room TV · samsung/ })).toBeTruthy();
    expect(screen.getByRole('option', { name: /Wohnzimmer Renderer · dlna/ })).toBeTruthy();
  });

  it('marks unreachable targets disabled', async () => {
    setAvailable(TARGETS);
    await openModal();
    const opt = screen.getByRole('option', { name: /unreachable/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(true);
  });

  it('submits the (output_provider, output_target_id) pair', async () => {
    setAvailable(TARGETS);
    const user = await openModal();
    await user.selectOptions(screen.getByLabelText('Gerät:'), 'samsung::192.168.1.47');
    // capability badges render for the selection
    expect(screen.getByText('power')).toBeTruthy();
    await user.click(screen.getByText('Hinzufügen'));
    expect(addSpy).toHaveBeenCalledTimes(1);
    const payload = addSpy.mock.calls[0][0].payload;
    expect(payload.output_provider).toBe('samsung');
    expect(payload.output_target_id).toBe('192.168.1.47');
    expect(payload.renfield_device_id).toBeUndefined();
  });

  it('falls back to legacy type-buttons when output_targets is absent', async () => {
    setAvailable(undefined);  // flag off → legacy shape
    await openModal();
    expect(screen.getByText('Gerätetyp:')).toBeTruthy();
  });
});

// TTS sound profile (tts_eq_profile): a NAMED equaliser profile for spoken
// answers on hi-fi outputs. null = off; the only known profile is hifi_speech.
describe('RoomOutputSettings TTS sound profile', () => {
  const DEVICE = {
    id: 7,
    output_type: 'audio' as const,
    is_enabled: true,
    allow_interruption: false,
    tts_volume: 0.5,
    tts_eq_profile: null as string | null,
    priority: 1,
    device_name: 'Anlage',
    output_provider: 'dlna',
    output_target_id: 'Wohnzimmer',
  };

  function setDevices(devices: Array<typeof DEVICE>) {
    vi.mocked(useOutputDevicesQuery).mockReturnValue(
      { data: devices, isLoading: false } as unknown as ReturnType<typeof useOutputDevicesQuery>,
    );
  }

  async function expand() {
    const user = userEvent.setup();
    renderWithProviders(<RoomOutputSettings roomId={1} roomName="Wohnzimmer" outputType="audio" />);
    await user.click(screen.getByText('Audio-Ausgabe'));
    return user;
  }

  it('renders the profile select with both options and the helper text', async () => {
    setAvailable(TARGETS);
    await openModal();
    const select = screen.getByLabelText('Klangprofil') as HTMLSelectElement;
    expect(select.value).toBe('');
    const options = Array.from(select.options).map((o) => [o.value, o.textContent]);
    expect(options).toEqual([['', 'Keins'], ['hifi_speech', 'HiFi-Sprache']]);
    expect(screen.getByText(/Entzerrt Sprachantworten/)).toBeTruthy();
  });

  it('sends tts_eq_profile "hifi_speech" when the profile is chosen', async () => {
    setAvailable(TARGETS);
    const user = await openModal();
    await user.selectOptions(screen.getByLabelText('Gerät:'), 'dlna::Wohnzimmer');
    await user.selectOptions(screen.getByLabelText('Klangprofil'), 'hifi_speech');
    await user.click(screen.getByText('Hinzufügen'));
    expect(addSpy).toHaveBeenCalledTimes(1);
    expect(addSpy.mock.calls[0][0].payload.tts_eq_profile).toBe('hifi_speech');
  });

  it('sends tts_eq_profile null when left on "Keins"', async () => {
    setAvailable(TARGETS);
    const user = await openModal();
    await user.selectOptions(screen.getByLabelText('Gerät:'), 'dlna::Wohnzimmer');
    await user.click(screen.getByText('Hinzufügen'));
    expect(addSpy).toHaveBeenCalledTimes(1);
    const payload = addSpy.mock.calls[0][0].payload;
    expect('tts_eq_profile' in payload).toBe(true);
    expect(payload.tts_eq_profile).toBeNull();
  });

  it('offers no profile select for a visual output and sends null', async () => {
    setAvailable(TARGETS);
    const user = userEvent.setup();
    renderWithProviders(<RoomOutputSettings roomId={1} roomName="Wohnzimmer" outputType="visual" />);
    await user.click(screen.getByText('Visuelle Ausgabe'));
    await user.click(screen.getByText('Visuelles Ausgabegerät hinzufügen'));
    expect(screen.queryByLabelText('Klangprofil')).toBeNull();
    await user.selectOptions(screen.getByLabelText('Gerät:'), 'samsung::192.168.1.47');
    await user.click(screen.getByText('Hinzufügen'));
    expect(addSpy.mock.calls[0][0].payload.tts_eq_profile).toBeNull();
  });

  it('shows the profile label on a device that has one', async () => {
    setDevices([{ ...DEVICE, tts_eq_profile: 'hifi_speech' }]);
    await expand();
    expect(screen.getByText('HiFi-Sprache')).toBeTruthy();
  });

  it('shows no profile label on a device without one', async () => {
    setDevices([DEVICE]);
    await expand();
    expect(screen.queryByText('HiFi-Sprache')).toBeNull();
  });

  it('shows an unknown profile name verbatim', async () => {
    setDevices([{ ...DEVICE, tts_eq_profile: 'studio_monitor' }]);
    await expand();
    expect(screen.getByText('studio_monitor')).toBeTruthy();
  });

  it('updates an existing device through the inline editor (set and clear)', async () => {
    vi.mocked(useUpdateOutputDevice).mockReturnValue(
      { mutateAsync: updateSpy, isPending: false } as unknown as ReturnType<typeof useUpdateOutputDevice>,
    );
    setDevices([{ ...DEVICE, tts_eq_profile: 'hifi_speech' }]);
    const user = await expand();
    expect(screen.queryByLabelText('Klangprofil')).toBeNull();     // closed by default
    await user.click(screen.getByRole('button', { name: 'Klangprofil ändern' }));
    const select = screen.getByLabelText('Klangprofil') as HTMLSelectElement;
    expect(select.value).toBe('hifi_speech');
    await user.selectOptions(select, '');
    expect(updateSpy).toHaveBeenLastCalledWith({ deviceId: 7, updates: { tts_eq_profile: null } });
    await user.selectOptions(select, 'hifi_speech');
    expect(updateSpy).toHaveBeenLastCalledWith({ deviceId: 7, updates: { tts_eq_profile: 'hifi_speech' } });
  });
});
