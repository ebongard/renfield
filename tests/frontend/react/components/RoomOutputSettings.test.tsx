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
const noopMut = { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof useUpdateOutputDevice>;

function setAvailable(output_targets: unknown) {
  vi.mocked(useAvailableOutputsQuery).mockReturnValue({
    data: { renfield_devices: [], ha_media_players: [], dlna_renderers: [], output_targets },
    isLoading: false,
  } as unknown as ReturnType<typeof useAvailableOutputsQuery>);
}

beforeEach(() => {
  addSpy.mockClear();
  vi.mocked(useOutputDevicesQuery).mockReturnValue(
    { data: [], isLoading: false } as unknown as ReturnType<typeof useOutputDevicesQuery>,
  );
  vi.mocked(useAddOutputDevice).mockReturnValue(
    { mutateAsync: addSpy, isPending: false } as unknown as ReturnType<typeof useAddOutputDevice>,
  );
  vi.mocked(useUpdateOutputDevice).mockReturnValue(noopMut);
  vi.mocked(useDeleteOutputDevice).mockReturnValue(noopMut);
  vi.mocked(useReorderOutputDevices).mockReturnValue(noopMut);
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
    await user.selectOptions(screen.getByRole('combobox'), 'samsung::192.168.1.47');
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
