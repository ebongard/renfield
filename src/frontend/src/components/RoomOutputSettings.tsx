import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Volume2, Plus, Trash2, Loader, ChevronDown, ChevronUp,
  Power, PowerOff, Speaker, Radio, Monitor, Wifi,
} from 'lucide-react';
import { extractApiError } from '../utils/axios';
import { useConfirmDialog } from './ConfirmDialog';
import { useAuth } from '../context/AuthContext';
import {
  useOutputDevicesQuery,
  useAvailableOutputsQuery,
  useAddOutputDevice,
  useUpdateOutputDevice,
  useDeleteOutputDevice,
  useReorderOutputDevices,
  type OutputType,
  type OutputDevice,
  type RenfieldOutputDevice,
  type HaOutputDevice,
  type DlnaOutputDevice,
} from '../api/resources/roomOutputs';

type DeviceKind = 'homeassistant' | 'renfield' | 'dlna';
type AvailableDevice = RenfieldOutputDevice | HaOutputDevice | DlnaOutputDevice;

export interface RoomOutputSettingsProps {
  roomId: number;
  roomName: string;
  outputType?: OutputType;
}

export default function RoomOutputSettings({
  roomId,
  roomName,
  outputType = 'audio',
}: RoomOutputSettingsProps) {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();
  const { isFeatureEnabled } = useAuth();
  const showHA = isFeatureEnabled('smart_home');
  const isVisual = outputType === 'visual';

  const [expanded, setExpanded] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const outputsQuery = useOutputDevicesQuery(roomId, expanded);
  const allDevices = outputsQuery.data ?? [];
  const outputDevices = allDevices.filter((d) => d.output_type === outputType);
  const loading = outputsQuery.isLoading;

  const availableQuery = useAvailableOutputsQuery(roomId, showAddModal);
  const availableOutputs = availableQuery.data ?? {
    renfield_devices: [],
    ha_media_players: [],
    dlna_renderers: [],
  };
  const loadingAvailable = availableQuery.isLoading;

  const addMutation = useAddOutputDevice(roomId);
  const updateMutation = useUpdateOutputDevice(roomId);
  const deleteMutation = useDeleteOutputDevice(roomId);
  const reorderMutation = useReorderOutputDevices(roomId);

  const [selectedType, setSelectedType] = useState<DeviceKind>(showHA ? 'homeassistant' : 'renfield');
  const [selectedDevice, setSelectedDevice] = useState('');
  const [allowInterruption, setAllowInterruption] = useState(false);
  const [ttsVolume, setTtsVolume] = useState(50);

  const adding = addMutation.isPending;

  const getDefaultType = (): DeviceKind => (showHA ? 'homeassistant' : 'renfield');

  const openAddModal = () => {
    setSelectedType(getDefaultType());
    setSelectedDevice('');
    setAllowInterruption(false);
    setTtsVolume(50);
    setShowAddModal(true);
  };

  const addOutputDevice = async () => {
    if (!selectedDevice) {
      setError(t('rooms.outputErrorSelectDevice'));
      return;
    }
    try {
      const payload: Record<string, unknown> = {
        output_type: outputType,
        allow_interruption: allowInterruption,
        tts_volume: ttsVolume / 100,
        priority: outputDevices.length + 1,
      };

      if (selectedType === 'renfield') {
        payload.renfield_device_id = selectedDevice;
      } else if (selectedType === 'dlna') {
        payload.dlna_renderer_name = selectedDevice;
      } else {
        payload.ha_entity_id = selectedDevice;
      }

      await addMutation.mutateAsync({ roomId, payload });
      setShowAddModal(false);
    } catch (err) {
      setError(extractApiError(err, t('rooms.outputErrorAddFailed')));
    }
  };

  const updateOutputDevice = async (deviceId: number, updates: Partial<OutputDevice>) => {
    try {
      await updateMutation.mutateAsync({ deviceId, updates });
    } catch {
      setError(t('rooms.outputErrorUpdateFailed'));
    }
  };

  const deleteOutputDevice = async (deviceId: number) => {
    const confirmed = await confirm({
      title: t('rooms.removeOutputDevice'),
      message: t('rooms.removeOutputDeviceConfirm'),
      variant: 'danger',
    });
    if (!confirmed) return;
    try {
      await deleteMutation.mutateAsync(deviceId);
    } catch {
      setError(t('rooms.outputErrorDeleteFailed'));
    }
  };

  const moveDevice = async (index: number, direction: 'up' | 'down') => {
    const newDevices = [...outputDevices];
    const targetIndex = direction === 'up' ? index - 1 : index + 1;
    if (targetIndex < 0 || targetIndex >= newDevices.length) return;
    [newDevices[index], newDevices[targetIndex]] = [newDevices[targetIndex], newDevices[index]];
    const deviceIds = newDevices.map((d) => d.id);
    try {
      await reorderMutation.mutateAsync({ roomId, outputType, deviceIds });
    } catch {
      setError(t('rooms.outputErrorReorderFailed'));
    }
  };

  const getDeviceIcon = (device: OutputDevice) => {
    if (device.dlna_renderer_name) {
      return <Wifi className="w-4 h-4 text-purple-400" />;
    }
    if (device.renfield_device_id) {
      return <Radio className="w-4 h-4 text-green-400" />;
    }
    return <Speaker className="w-4 h-4 text-blue-400" />;
  };

  const getAvailableDevices = (): AvailableDevice[] => {
    const configuredRenfieldIds = new Set(
      outputDevices.map((d) => d.renfield_device_id).filter((v): v is string => Boolean(v)),
    );
    const configuredHAIds = new Set(
      outputDevices.map((d) => d.ha_entity_id).filter((v): v is string => Boolean(v)),
    );
    const configuredDLNANames = new Set(
      outputDevices.map((d) => d.dlna_renderer_name).filter((v): v is string => Boolean(v)),
    );

    if (selectedType === 'renfield') {
      return availableOutputs.renfield_devices.filter((d) => !configuredRenfieldIds.has(d.device_id));
    }
    if (selectedType === 'dlna') {
      return (availableOutputs.dlna_renderers ?? []).filter((d) => !configuredDLNANames.has(d.name));
    }
    if (showHA) {
      return availableOutputs.ha_media_players.filter((d) => !configuredHAIds.has(d.entity_id));
    }
    return [];
  };

  const getDeviceKey = (device: AvailableDevice): string => {
    if ('device_id' in device) return device.device_id;
    if ('entity_id' in device) return device.entity_id;
    return device.name;
  };

  const getDeviceValue = (device: AvailableDevice): string => getDeviceKey(device);

  const getDeviceLabel = (device: AvailableDevice): string => {
    if ('device_id' in device) return device.device_name ?? device.device_id;
    if ('entity_id' in device) return device.friendly_name ?? device.entity_id;
    return device.friendly_name ?? device.name;
  };

  return (
    <div className="mt-4 border-t border-gray-700 pt-4">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between text-left hover:bg-gray-800 rounded-lg p-2 -m-2"
      >
        <div className="flex items-center space-x-2">
          {isVisual ? (
            <Monitor className="w-4 h-4 text-gray-400" />
          ) : (
            <Volume2 className="w-4 h-4 text-gray-400" />
          )}
          <span className="text-sm text-gray-300">
            {isVisual ? t('rooms.outputVisualLabel') : t('rooms.outputAudioLabel')}
          </span>
          {outputDevices.length > 0 && (
            <span className="text-xs text-gray-500">({outputDevices.length})</span>
          )}
        </div>
        {expanded ? (
          <ChevronUp className="w-4 h-4 text-gray-400" />
        ) : (
          <ChevronDown className="w-4 h-4 text-gray-400" />
        )}
      </button>

      {expanded && (
        <div className="mt-4 space-y-3">
          {error && (
            <div className="text-red-400 text-xs bg-red-900/20 p-2 rounded-sm">
              {error}
              <button onClick={() => setError(null)} className="ml-2 underline">
                {t('rooms.outputDismissError')}
              </button>
            </div>
          )}

          {loading ? (
            <div className="text-center py-4">
              <Loader className="w-5 h-5 animate-spin mx-auto text-gray-400" />
            </div>
          ) : outputDevices.length === 0 ? (
            <p className="text-gray-500 text-xs text-center py-2">
              {isVisual ? (
                t('rooms.outputVisualNoneConfigured')
              ) : (
                <>
                  {t('rooms.outputAudioNoneConfigured')}
                  <br />
                  {t('rooms.outputAudioNoneConfiguredDetail')}
                </>
              )}
            </p>
          ) : (
            <div className="space-y-2">
              {outputDevices.map((device, index) => (
                <div
                  key={device.id}
                  className={`flex items-center space-x-2 p-2 rounded-lg ${
                    device.is_enabled ? 'bg-gray-800' : 'bg-gray-800/50 opacity-50'
                  }`}
                >
                  <span className="w-5 h-5 bg-gray-700 rounded-sm text-xs flex items-center justify-center text-gray-400">
                    {index + 1}
                  </span>

                  {getDeviceIcon(device)}

                  <span className="flex-1 text-sm text-gray-300 truncate">
                    {device.device_name || device.dlna_renderer_name || device.ha_entity_id || device.renfield_device_id}
                  </span>

                  {device.tts_volume !== null && (
                    <span className="text-xs text-gray-500">
                      {Math.round(device.tts_volume * 100)}%
                    </span>
                  )}

                  {device.allow_interruption && (
                    <span className="text-xs text-yellow-400" title={t('rooms.outputDeviceInterruptHint')}>
                      INT
                    </span>
                  )}

                  <button
                    onClick={() => updateOutputDevice(device.id, { is_enabled: !device.is_enabled })}
                    className={`p-1 rounded-sm ${device.is_enabled ? 'text-green-400' : 'text-gray-500'}`}
                    title={device.is_enabled ? t('rooms.outputDeviceDisable') : t('rooms.outputDeviceEnable')}
                  >
                    {device.is_enabled ? <Power className="w-3 h-3" /> : <PowerOff className="w-3 h-3" />}
                  </button>

                  <div className="flex flex-col">
                    <button
                      onClick={() => moveDevice(index, 'up')}
                      disabled={index === 0}
                      className="text-gray-500 hover:text-gray-300 disabled:opacity-30"
                    >
                      <ChevronUp className="w-3 h-3" />
                    </button>
                    <button
                      onClick={() => moveDevice(index, 'down')}
                      disabled={index === outputDevices.length - 1}
                      className="text-gray-500 hover:text-gray-300 disabled:opacity-30"
                    >
                      <ChevronDown className="w-3 h-3" />
                    </button>
                  </div>

                  <button
                    onClick={() => deleteOutputDevice(device.id)}
                    className="p-1 text-red-400 hover:text-red-300"
                    title={t('rooms.outputDeviceRemove')}
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}

          <button
            onClick={openAddModal}
            className="w-full flex items-center justify-center space-x-2 py-2 text-sm text-gray-400 hover:text-gray-300 border border-dashed border-gray-700 rounded-lg hover:border-gray-600"
          >
            <Plus className="w-4 h-4" />
            <span>
              {isVisual
                ? t('rooms.outputAddVisualButton')
                : t('rooms.outputAddAudioButton')}
            </span>
          </button>
        </div>
      )}

      {ConfirmDialogComponent}

      {showAddModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card max-w-md w-full">
            <h2 className="text-xl font-bold text-white mb-4">
              {isVisual
                ? t('rooms.outputAddDialogVisualTitle')
                : t('rooms.outputAddDialogAudioTitle')}
            </h2>
            <p className="text-gray-400 text-sm mb-4">{t('rooms.outputDialogRoomLabel', { name: roomName })}</p>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-400 mb-2">{t('rooms.outputDeviceTypeLabel')}</label>
                <div className="flex space-x-2">
                  {showHA && (
                    <button
                      onClick={() => {
                        setSelectedType('homeassistant');
                        setSelectedDevice('');
                      }}
                      className={`flex-1 p-3 rounded-lg border ${
                        selectedType === 'homeassistant'
                          ? 'border-blue-500 bg-blue-500/20'
                          : 'border-gray-700 bg-gray-800'
                      }`}
                    >
                      <Speaker className="w-5 h-5 mx-auto mb-1 text-blue-400" />
                      <span className="text-sm text-gray-300">{t('rooms.outputDeviceTypeHomeAssistant')}</span>
                    </button>
                  )}
                  <button
                    onClick={() => {
                      setSelectedType('renfield');
                      setSelectedDevice('');
                    }}
                    className={`flex-1 p-3 rounded-lg border ${
                      selectedType === 'renfield'
                        ? 'border-green-500 bg-green-500/20'
                        : 'border-gray-700 bg-gray-800'
                    }`}
                  >
                    <Radio className="w-5 h-5 mx-auto mb-1 text-green-400" />
                    <span className="text-sm text-gray-300">{t('rooms.outputDeviceTypeRenfield')}</span>
                  </button>
                  <button
                    onClick={() => {
                      setSelectedType('dlna');
                      setSelectedDevice('');
                    }}
                    className={`flex-1 p-3 rounded-lg border ${
                      selectedType === 'dlna'
                        ? 'border-purple-500 bg-purple-500/20'
                        : 'border-gray-700 bg-gray-800'
                    }`}
                  >
                    <Wifi className="w-5 h-5 mx-auto mb-1 text-purple-400" />
                    <span className="text-sm text-gray-300">{t('rooms.dlnaRenderer')}</span>
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-2">{t('rooms.outputDeviceLabel')}</label>
                {loadingAvailable ? (
                  <div className="text-center py-4">
                    <Loader className="w-5 h-5 animate-spin mx-auto text-gray-400" />
                  </div>
                ) : (
                  <select
                    value={selectedDevice}
                    onChange={(e) => setSelectedDevice(e.target.value)}
                    className="input w-full"
                  >
                    <option value="">{t('rooms.outputDeviceSelectPlaceholder')}</option>
                    {getAvailableDevices().map((device) => (
                      <option
                        key={getDeviceKey(device)}
                        value={getDeviceValue(device)}
                      >
                        {getDeviceLabel(device)}
                      </option>
                    ))}
                  </select>
                )}
                {getAvailableDevices().length === 0 && !loadingAvailable && (
                  <p className="text-yellow-400 text-xs mt-2">
                    {t('rooms.outputNoAvailableDevices')}
                  </p>
                )}
                {selectedType === 'dlna' && !loadingAvailable && (
                  <p className="text-xs text-gray-500 mt-1">
                    {t('rooms.dlnaRendererDesc')}
                  </p>
                )}
              </div>

              {!isVisual && (
                <div>
                  <label className="block text-sm text-gray-400 mb-2">
                    {t('rooms.outputTtsVolumeLabel', { percent: ttsVolume })}
                  </label>
                  <input
                    type="range"
                    min="0"
                    max="100"
                    value={ttsVolume}
                    onChange={(e) => setTtsVolume(parseInt(e.target.value, 10))}
                    className="w-full"
                  />
                  <p className="text-xs text-gray-500 mt-1">
                    {t('rooms.outputTtsVolumeHint')}
                  </p>
                </div>
              )}

              <div className="flex items-center space-x-3">
                <input
                  type="checkbox"
                  id="allowInterruption"
                  checked={allowInterruption}
                  onChange={(e) => setAllowInterruption(e.target.checked)}
                  className="w-4 h-4"
                />
                <label htmlFor="allowInterruption" className="text-sm text-gray-300">
                  {t('rooms.outputAllowInterruption')}
                </label>
              </div>
              <p className="text-xs text-gray-500 -mt-2">
                {t('rooms.outputAllowInterruptionHint')}
              </p>
            </div>

            <div className="flex space-x-3 mt-6">
              <button
                onClick={() => setShowAddModal(false)}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white"
              >
                {t('rooms.outputCancelButton')}
              </button>
              <button
                onClick={addOutputDevice}
                disabled={!selectedDevice || adding}
                className="flex-1 btn btn-primary disabled:opacity-50"
              >
                {adding ? (
                  <Loader className="w-4 h-4 animate-spin mx-auto" />
                ) : (
                  t('rooms.outputAddButton')
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
