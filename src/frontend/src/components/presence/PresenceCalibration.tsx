/**
 * Placement v2 calibration — tab "Calibration" on /admin/presence.
 *
 * The numbers placement v2 decides with live in tables (backend
 * `ha_glue/services/presence_calibration.py`). This view shows them, lets an
 * admin edit or remove a receiver, edit the open-plan zones, and run the
 * GUIDED measurement: pick the device and the receiver of the room you are in,
 * leave the phone where it usually is in that room, start; after
 * MEASUREMENT_SECONDS the backend proposes the in-room bound from the readings
 * that receiver took of that device. Nothing is saved before "Save".
 *
 * Live levels come by push (usePresenceReadings); the only timer here is the
 * measurement countdown, which fetches nothing.
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Play, Save, Trash2, X } from 'lucide-react';

import Badge from '../Badge';
import { usePresenceReadings, type PresenceReadingsSnapshot } from '../../hooks/usePresenceReadings';
import {
  calibrationErrorDetail,
  deleteCalibrationReceiver,
  requestCalibrationProposal,
  saveCalibrationReceiver,
  saveCalibrationZones,
  startCalibrationMeasurement,
  useCalibrationCache,
  usePresenceCalibrationQuery,
  type Calibration,
  type CalibrationProposal,
  type CalibrationZone,
} from '../../api/resources/presence';

/** Seconds of a guided measurement. Below the backend's reading window (120 s). */
export const MEASUREMENT_SECONDS = 60;

const MUTED = 'text-gray-500 dark:text-gray-400';
const TH = 'py-2 px-3 font-medium text-gray-600 dark:text-gray-400';
const TD = 'py-2 px-3 tabular-nums text-gray-900 dark:text-white';

/** Readings the chosen receiver took of the chosen device since `since`. */
export function countMeasurementReadings(
  snapshot: PresenceReadingsSnapshot | null,
  deviceKey: string,
  satelliteId: string,
  since: number,
): number {
  const device = snapshot?.devices.find((d) => d.key === deviceKey);
  if (!device) return 0;
  return new Set(
    device.sightings
      .filter((s) => s.satellite_id === satelliteId && s.timestamp >= since)
      .map((s) => s.timestamp),
  ).size;
}

/** A finite number from an input, or null. */
function parseNumber(value: string): number | null {
  if (value.trim() === '') return null;
  const n = Number(value.replace(',', '.'));
  return Number.isFinite(n) ? n : null;
}

function useErrorText() {
  const { t } = useTranslation();
  return (error: unknown) => {
    const detail = calibrationErrorDetail(error);
    return detail
      ? t(`presence.calibration.errors.${detail.code}`, { ...detail })
      : t('presence.calibration.errors.generic');
  };
}

// ---- guided measurement ------------------------------------------------------

interface ProposalFormProps {
  proposal: CalibrationProposal;
  formatDbm: (value: number) => string;
  onSaved: (calibration: Calibration) => void;
  onCancel: () => void;
}

function ProposalForm({ proposal, formatDbm, onSaved, onCancel }: ProposalFormProps) {
  const { t } = useTranslation();
  const errorText = useErrorText();
  const [bound, setBound] = useState(String(proposal.in_room_dbm));
  const [cutoff, setCutoff] = useState(proposal.cutoff_dbm === null ? '' : String(proposal.cutoff_dbm));
  const [offset, setOffset] = useState(String(proposal.offset_db));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const values = { bound: parseNumber(bound), cutoff: parseNumber(cutoff), offset: parseNumber(offset) };
  const complete = values.bound !== null && values.cutoff !== null && values.offset !== null;

  const save = async () => {
    if (values.bound === null || values.cutoff === null || values.offset === null) return;
    setSaving(true);
    setError(null);
    try {
      onSaved(
        await saveCalibrationReceiver({
          satellite_id: proposal.satellite_id,
          in_room_dbm: values.bound,
          cutoff_dbm: values.cutoff,
          offset_db: values.offset,
        }),
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-6 border-t border-gray-200 dark:border-gray-700 pt-6" data-testid="proposal">
      <h4 className="text-sm font-medium text-gray-900 dark:text-white mb-2">
        {t('presence.calibration.proposalTitle', { satellite: proposal.satellite_id })}
      </h4>
      <p className={`text-sm tabular-nums ${MUTED}`}>
        {t('presence.calibration.proposalStats', {
          samples: proposal.samples,
          min: formatDbm(proposal.min_dbm),
          median: formatDbm(proposal.median_dbm),
          max: formatDbm(proposal.max_dbm),
        })}
      </p>
      <p className={`mt-1 text-sm tabular-nums ${MUTED}`}>
        {proposal.current
          ? t('presence.calibration.proposalCurrent', {
              bound: formatDbm(proposal.current.in_room_dbm),
              cutoff: formatDbm(proposal.current.cutoff_dbm),
            })
          : t('presence.calibration.proposalNew')}
      </p>

      <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-4">
        <label className="block text-sm text-gray-700 dark:text-gray-300">
          {t('presence.calibration.fieldBound')}
          <input className="input mt-1" inputMode="decimal" value={bound}
                 onChange={(e) => setBound(e.target.value)} />
        </label>
        <label className="block text-sm text-gray-700 dark:text-gray-300">
          {t('presence.calibration.fieldCutoff')}
          <input className="input mt-1" inputMode="decimal" value={cutoff}
                 aria-describedby={proposal.cutoff_dbm === null ? 'cutoff-hint' : undefined}
                 onChange={(e) => setCutoff(e.target.value)} />
        </label>
        <label className="block text-sm text-gray-700 dark:text-gray-300">
          {t('presence.calibration.fieldOffset')}
          <input className="input mt-1" inputMode="decimal" value={offset}
                 onChange={(e) => setOffset(e.target.value)} />
        </label>
      </div>
      {proposal.cutoff_dbm === null && (
        <p id="cutoff-hint" className={`mt-2 text-sm ${MUTED}`}>{t('presence.calibration.cutoffHint')}</p>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-primary-700 dark:text-primary-400">{error}</p>
      )}
      <div className="mt-4 flex flex-wrap gap-3">
        <button type="button" className="btn-primary inline-flex items-center gap-2"
                disabled={!complete || saving} onClick={save}>
          <Save className="w-4 h-4" aria-hidden="true" />
          {t('presence.calibration.save')}
        </button>
        <button type="button" className="btn-secondary inline-flex items-center gap-2" onClick={onCancel}>
          <X className="w-4 h-4" aria-hidden="true" />
          {t('presence.calibration.discard')}
        </button>
      </div>
    </div>
  );
}

interface MeasurementProps {
  snapshot: PresenceReadingsSnapshot | null;
  minSamples: number;
  formatDbm: (value: number) => string;
  onSaved: (calibration: Calibration) => void;
}

function GuidedMeasurement({ snapshot, minSamples, formatDbm, onSaved }: MeasurementProps) {
  const { t } = useTranslation();
  const errorText = useErrorText();
  const devices = snapshot?.devices ?? [];
  const satellites = snapshot?.satellites ?? [];
  const [deviceKey, setDeviceKey] = useState('');
  const [satelliteId, setSatelliteId] = useState('');
  const [since, setSince] = useState<number | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null); // browser clock, countdown only
  const [remaining, setRemaining] = useState(0);
  const [proposal, setProposal] = useState<CalibrationProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The countdown: a display timer, it fetches nothing.
  useEffect(() => {
    if (startedAt === null) return undefined;
    const tick = () =>
      setRemaining(Math.max(0, MEASUREMENT_SECONDS - Math.floor((Date.now() - startedAt) / 1000)));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [startedAt]);

  const count = since === null ? 0 : countMeasurementReadings(snapshot, deviceKey, satelliteId, since);
  const running = since !== null && proposal === null;
  const finished = running && remaining === 0;

  const reset = () => {
    setSince(null);
    setStartedAt(null);
    setProposal(null);
    setError(null);
  };

  const start = async () => {
    reset();
    setBusy(true);
    try {
      const serverSince = await startCalibrationMeasurement();
      setRemaining(MEASUREMENT_SECONDS);
      setSince(serverSince);
      setStartedAt(Date.now());
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const evaluate = async () => {
    if (since === null) return;
    setBusy(true);
    setError(null);
    try {
      setProposal(await requestCalibrationProposal({ deviceKey, satelliteId, since }));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const roomOf = (id: string) => satellites.find((s) => s.satellite_id === id)?.room_name ?? null;

  return (
    <section className="card" aria-labelledby="calibration-measure-title">
      <h3 id="calibration-measure-title" className="text-base font-medium text-gray-900 dark:text-white mb-2">
        {t('presence.calibration.measureTitle')}
      </h3>
      <p className={`text-sm ${MUTED} mb-4`}>
        {t('presence.calibration.measureText', { seconds: MEASUREMENT_SECONDS })}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <label className="block text-sm text-gray-700 dark:text-gray-300">
          {t('presence.calibration.device')}
          <select className="input mt-1" value={deviceKey} disabled={running}
                  onChange={(e) => { setDeviceKey(e.target.value); reset(); }}>
            <option value="">{t('presence.calibration.choose')}</option>
            {devices.map((d) => (
              <option key={d.key} value={d.key}>
                {(d.user_name ?? t('presence.readings.unknownUser')) + ' · ' + d.key}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-gray-700 dark:text-gray-300">
          {t('presence.calibration.receiver')}
          <select className="input mt-1" value={satelliteId} disabled={running}
                  onChange={(e) => { setSatelliteId(e.target.value); reset(); }}>
            <option value="">{t('presence.calibration.choose')}</option>
            {satellites.map((s) => (
              <option key={s.satellite_id} value={s.satellite_id}>
                {s.satellite_id + (s.room_name ? ' · ' + s.room_name : '')}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {!running ? (
          <button type="button" className="btn-primary inline-flex items-center gap-2"
                  disabled={!deviceKey || !satelliteId || busy} onClick={start}>
            <Play className="w-4 h-4" aria-hidden="true" />
            {t('presence.calibration.start')}
          </button>
        ) : (
          <>
            <button type="button" className="btn-primary" disabled={!finished || busy} onClick={evaluate}>
              {t('presence.calibration.evaluate')}
            </button>
            <button type="button" className="btn-secondary" onClick={reset}>
              {t('presence.calibration.cancel')}
            </button>
          </>
        )}
        {running && (
          <span className={`text-sm tabular-nums ${MUTED}`} data-testid="measurement-progress">
            {remaining > 0
              ? t('presence.calibration.running', {
                  seconds: remaining,
                  count,
                  room: roomOf(satelliteId) ?? satelliteId,
                })
              : t('presence.calibration.done', { count })}
            {count < minSamples && (
              <Badge color="amber" className="ml-2">
                {t('presence.calibration.fewReadings', { needed: minSamples })}
              </Badge>
            )}
          </span>
        )}
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-primary-700 dark:text-primary-400">{error}</p>}

      {proposal && (
        <ProposalForm
          key={`${proposal.satellite_id}:${since}`}
          proposal={proposal}
          formatDbm={formatDbm}
          onSaved={(calibration) => { onSaved(calibration); reset(); }}
          onCancel={reset}
        />
      )}
    </section>
  );
}

// ---- receivers + zones -------------------------------------------------------

interface ReceiversProps {
  calibration: Calibration;
  roomOf: (satelliteId: string) => string | null;
  formatDbm: (value: number) => string;
  onChanged: (calibration: Calibration) => void;
}

function ReceiverTable({ calibration, roomOf, formatDbm, onChanged }: ReceiversProps) {
  const { t } = useTranslation();
  const errorText = useErrorText();
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const remove = async (satelliteId: string) => {
    setError(null);
    try {
      onChanged(await deleteCalibrationReceiver(satelliteId));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setConfirming(null);
    }
  };

  return (
    <section className="card" aria-labelledby="calibration-receivers-title">
      <h3 id="calibration-receivers-title" className="text-base font-medium text-gray-900 dark:text-white mb-2">
        {t('presence.calibration.receiversTitle')}
      </h3>
      {calibration.receivers.length === 0 ? (
        <p className={`text-sm ${MUTED}`}>{t('presence.calibration.noReceivers')}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 dark:border-gray-700 text-left">
                <th scope="col" className={TH}>{t('presence.readings.colSatellite')}</th>
                <th scope="col" className={TH}>{t('presence.readings.colRoom')}</th>
                <th scope="col" className={`${TH} text-right`}>{t('presence.calibration.fieldBound')}</th>
                <th scope="col" className={`${TH} text-right`}>{t('presence.calibration.fieldCutoff')}</th>
                <th scope="col" className={`${TH} text-right`}>{t('presence.calibration.fieldOffset')}</th>
                <th scope="col" className={TH}><span className="sr-only">{t('presence.calibration.actions')}</span></th>
              </tr>
            </thead>
            <tbody>
              {calibration.receivers.map((r) => (
                <tr key={r.satellite_id} data-testid={`calibrated-${r.satellite_id}`}
                    className="border-b border-gray-100 dark:border-gray-800 last:border-0">
                  <td className="py-2 px-3 font-mono text-xs text-gray-900 dark:text-white">{r.satellite_id}</td>
                  <td className={`py-2 px-3 ${MUTED}`}>{roomOf(r.satellite_id) ?? t('presence.readings.unknownRoom')}</td>
                  <td className={`${TD} text-right`}>{formatDbm(r.in_room_dbm)}</td>
                  <td className={`${TD} text-right`}>{formatDbm(r.cutoff_dbm)}</td>
                  <td className={`${TD} text-right`}>{formatDbm(r.offset_db)}</td>
                  <td className="py-2 px-3 text-right whitespace-nowrap">
                    {confirming === r.satellite_id ? (
                      <span className="inline-flex gap-2">
                        <button type="button" className="btn-secondary text-sm" onClick={() => remove(r.satellite_id)}>
                          {t('presence.calibration.confirmRemove')}
                        </button>
                        <button type="button" className="btn-secondary text-sm" onClick={() => setConfirming(null)}>
                          {t('presence.calibration.cancel')}
                        </button>
                      </span>
                    ) : (
                      <button type="button"
                              className="p-2 min-w-11 min-h-11 inline-flex items-center justify-center rounded-lg text-gray-500 hover:text-primary-700 dark:text-gray-400 dark:hover:text-primary-400"
                              aria-label={t('presence.calibration.remove', { satellite: r.satellite_id })}
                              onClick={() => setConfirming(r.satellite_id)}>
                        <Trash2 className="w-4 h-4" aria-hidden="true" />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className={`mt-3 text-sm ${MUTED}`}>{t('presence.calibration.removeHint')}</p>
      {error && <p role="alert" className="mt-3 text-sm text-primary-700 dark:text-primary-400">{error}</p>}
    </section>
  );
}

interface ZonesProps {
  zones: CalibrationZone[];
  rooms: Array<{ id: number; name: string }>;
  onChanged: (calibration: Calibration) => void;
}

function ZoneEditor({ zones, rooms, onChanged }: ZonesProps) {
  const { t } = useTranslation();
  const errorText = useErrorText();
  const [draft, setDraft] = useState<CalibrationZone[]>(zones);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(zones), [zones]);

  const dirty = JSON.stringify(draft) !== JSON.stringify(zones);
  const setZone = (index: number, zone: CalibrationZone) =>
    setDraft(draft.map((z, i) => (i === index ? zone : z)));
  const roomTaken = (roomId: number, index: number) =>
    draft.some((z, i) => i !== index && z.room_ids.includes(roomId));

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      onChanged(await saveCalibrationZones(draft.map((z) => ({ ...z, name: z.name.trim() }))));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="card" aria-labelledby="calibration-zones-title">
      <h3 id="calibration-zones-title" className="text-base font-medium text-gray-900 dark:text-white mb-2">
        {t('presence.calibration.zonesTitle')}
      </h3>
      <p className={`text-sm ${MUTED} mb-4`}>{t('presence.calibration.zonesText')}</p>
      <div className="space-y-4">
        {draft.map((zone, index) => (
          <fieldset key={index} className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            <legend className="sr-only">{t('presence.calibration.zoneLegend', { index: index + 1 })}</legend>
            <div className="flex flex-wrap items-end gap-3">
              <label className="block text-sm text-gray-700 dark:text-gray-300 grow">
                {t('presence.calibration.zoneName')}
                <input className="input mt-1" value={zone.name}
                       onChange={(e) => setZone(index, { ...zone, name: e.target.value })} />
              </label>
              <button type="button" className="btn-secondary"
                      onClick={() => setDraft(draft.filter((_, i) => i !== index))}>
                {t('presence.calibration.zoneRemove')}
              </button>
            </div>
            <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2">
              {rooms.map((room) => (
                <label key={room.id} className="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 min-h-11">
                  <input type="checkbox"
                         checked={zone.room_ids.includes(room.id)}
                         disabled={roomTaken(room.id, index)}
                         onChange={(e) =>
                           setZone(index, {
                             ...zone,
                             room_ids: e.target.checked
                               ? [...zone.room_ids, room.id].sort((a, b) => a - b)
                               : zone.room_ids.filter((r) => r !== room.id),
                           })
                         } />
                  {room.name}
                </label>
              ))}
            </div>
          </fieldset>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap gap-3">
        <button type="button" className="btn-secondary"
                onClick={() => setDraft([...draft, { name: '', room_ids: [] }])}>
          {t('presence.calibration.zoneAdd')}
        </button>
        <button type="button" className="btn-primary" disabled={!dirty || saving} onClick={save}>
          {t('presence.calibration.zonesSave')}
        </button>
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-primary-700 dark:text-primary-400">{error}</p>}
    </section>
  );
}

// ---- tab ---------------------------------------------------------------------

export default function PresenceCalibration() {
  const { t, i18n } = useTranslation();
  const { data: calibration, isLoading, errorMessage } = usePresenceCalibrationQuery();
  const { snapshot } = usePresenceReadings();
  const putInCache = useCalibrationCache();

  const dbmFormat = new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 1 });
  const formatDbm = (value: number) => dbmFormat.format(value);

  const satellites = useMemo(() => snapshot?.satellites ?? [], [snapshot]);
  const roomOf = (satelliteId: string) =>
    satellites.find((s) => s.satellite_id === satelliteId)?.room_name ?? null;
  const rooms = useMemo(() => {
    const byId = new Map<number, string>();
    for (const s of satellites) {
      if (s.room_id !== null) byId.set(s.room_id, s.room_name ?? String(s.room_id));
    }
    for (const zone of calibration?.zones ?? []) {
      for (const id of zone.room_ids) if (!byId.has(id)) byId.set(id, String(id));
    }
    return [...byId.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
  }, [satellites, calibration?.zones]);

  if (isLoading) return <p className={MUTED}>{t('common.loading')}</p>;
  if (!calibration) return <p role="alert" className="text-primary-700 dark:text-primary-400">{errorMessage}</p>;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2" data-testid="calibration-status">
        <Badge color={calibration.engine_active ? 'accent' : 'gray'}>
          {calibration.engine_active ? t('presence.calibration.engineOn') : t('presence.calibration.engineOff')}
        </Badge>
        {calibration.settings_ignored && (
          <Badge color="amber">{t('presence.calibration.settingsIgnored')}</Badge>
        )}
      </div>
      <GuidedMeasurement snapshot={snapshot} minSamples={calibration.min_samples}
                         formatDbm={formatDbm} onSaved={putInCache} />
      <ReceiverTable calibration={calibration} roomOf={roomOf} formatDbm={formatDbm} onChanged={putInCache} />
      <ZoneEditor zones={calibration.zones} rooms={rooms} onChanged={putInCache} />
    </div>
  );
}
