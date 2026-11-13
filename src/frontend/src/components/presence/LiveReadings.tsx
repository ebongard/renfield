/**
 * Live Bluetooth readings — the raw numbers behind "which room is this person
 * in". One card per tracked device: every satellite's readings in the window
 * (including the connected satellites that did NOT hear it), and per room the
 * smoothed value the room decision compares, with what a challenger room would
 * need to take over.
 *
 * Data arrives by push (usePresenceReadings → /ws/presence-debug); this
 * component never fetches and never ticks. Ages are the server's own
 * (`age_s`, relative to the snapshot), so a skewed browser clock cannot
 * distort them.
 */
import { useTranslation } from 'react-i18next';
import { Lock, Radio, RefreshCw, Wifi } from 'lucide-react';

import Badge from '../Badge';
import { formatTime } from '../../utils/datetime';
import {
  usePresenceReadings,
  type PresenceReadingDevice,
  type PresenceReadingSatellite,
  type PresenceReadingSettings,
  type PresenceReadingsSnapshot,
  type PresenceReadingsStatus,
} from '../../hooks/usePresenceReadings';

// ---- derivation (pure, exported for tests) ---------------------------------

export interface SatelliteReadingRow {
  satelliteId: string;
  roomName: string | null;
  heard: boolean;
  /** Newest reading; null when not heard. */
  latest: number | null;
  mean: number | null;
  min: number | null;
  max: number | null;
  count: number;
  /** Age of the newest reading in seconds; null when not heard. */
  ageS: number | null;
  /** Oldest → newest, for the trend line. */
  series: Array<{ timestamp: number; rssi: number }>;
}

export interface RoomDecisionRow {
  roomId: number;
  roomName: string | null;
  filtered: number;
  isCurrent: boolean;
  /** Filtered value this room must reach to take over; null for the current
   *  room or when nothing has to be beaten. */
  needs: number | null;
  /** Which rule `needs` comes from: `enter` = current room's filtered value +
   *  enter margin; `release` = RSSI floor + voice release margin (the user is
   *  pinned by a voice/auth sighting to a room the device was never heard in). */
  basis: 'enter' | 'release' | null;
  /** dB still missing to `needs` (≤ 0 = the margin is met). */
  missing: number | null;
}

/** One row per satellite: those that heard the device (strongest newest
 *  reading first), then the connected ones that did not. */
export function summarizeSatellites(
  device: PresenceReadingDevice,
  roster: PresenceReadingSatellite[],
): SatelliteReadingRow[] {
  const bySatellite = new Map<string, SatelliteReadingRow>();
  for (const s of device.sightings) {
    let row = bySatellite.get(s.satellite_id);
    if (!row) {
      row = {
        satelliteId: s.satellite_id,
        roomName: s.room_name,
        heard: true,
        latest: null,
        mean: null,
        min: null,
        max: null,
        count: 0,
        ageS: null,
        series: [],
      };
      bySatellite.set(s.satellite_id, row);
    }
    row.series.push({ timestamp: s.timestamp, rssi: s.rssi });
    if (row.ageS === null || s.age_s <= row.ageS) {
      row.ageS = s.age_s;
      row.latest = s.rssi;
      row.roomName = s.room_name ?? row.roomName;
    }
  }
  const heard = [...bySatellite.values()].map((row) => {
    const values = row.series.map((p) => p.rssi);
    row.series.sort((a, b) => a.timestamp - b.timestamp);
    return {
      ...row,
      count: values.length,
      mean: values.reduce((sum, v) => sum + v, 0) / values.length,
      min: Math.min(...values),
      max: Math.max(...values),
    };
  });
  heard.sort((a, b) => (b.latest ?? -Infinity) - (a.latest ?? -Infinity));

  const notHeard = roster
    .filter((sat) => !bySatellite.has(sat.satellite_id))
    .map<SatelliteReadingRow>((sat) => ({
      satelliteId: sat.satellite_id,
      roomName: sat.room_name,
      heard: false,
      latest: null,
      mean: null,
      min: null,
      max: null,
      count: 0,
      ageS: null,
      series: [],
    }))
    .sort((a, b) => a.satelliteId.localeCompare(b.satelliteId));

  return [...heard, ...notHeard];
}

/** True when the user sits in a voice/auth room the device was never heard in
 *  and the backend therefore applies the release margin instead of the enter
 *  margin (mirrors `PresenceService._should_switch_filtered`). */
export function isPinnedWithoutReading(
  device: PresenceReadingDevice,
  settings: PresenceReadingSettings,
): boolean {
  return (
    device.current_room_id !== null &&
    device.voice_pinned &&
    settings.voice_release_margin_db > 0 &&
    !device.filtered_rooms.some((r) => r.room_id === device.current_room_id)
  );
}

/** Rooms by filtered value, strongest first, with the take-over threshold. */
export function summarizeRooms(
  device: PresenceReadingDevice,
  settings: PresenceReadingSettings,
): RoomDecisionRow[] {
  const current = device.filtered_rooms.find((r) => r.room_id === device.current_room_id);
  const release = isPinnedWithoutReading(device, settings);
  return [...device.filtered_rooms]
    .sort((a, b) => b.rssi - a.rssi)
    .map((room) => {
      const isCurrent = current !== undefined && room.room_id === current.room_id;
      let needs: number | null = null;
      let basis: RoomDecisionRow['basis'] = null;
      if (current !== undefined && !isCurrent) {
        needs = current.rssi + settings.switch_enter_margin_db;
        basis = 'enter';
      } else if (release) {
        needs = settings.rssi_floor_dbm + settings.voice_release_margin_db;
        basis = 'release';
      }
      return {
        roomId: room.room_id,
        roomName: room.room_name,
        filtered: room.rssi,
        isCurrent,
        needs,
        basis,
        missing: needs === null ? null : needs - room.rssi,
      };
    });
}

// ---- presentation ----------------------------------------------------------

const SPARK_WIDTH = 96;
const SPARK_HEIGHT = 24;
/** Fixed dBm scale so lines are comparable across satellites and devices. */
const SPARK_MIN_DBM = -100;
const SPARK_MAX_DBM = -30;

function sparkY(rssi: number): number {
  const clamped = Math.min(SPARK_MAX_DBM, Math.max(SPARK_MIN_DBM, rssi));
  const ratio = (clamped - SPARK_MIN_DBM) / (SPARK_MAX_DBM - SPARK_MIN_DBM);
  return Math.round((SPARK_HEIGHT - 2 - ratio * (SPARK_HEIGHT - 4)) * 10) / 10;
}

interface SparklineProps {
  series: SatelliteReadingRow['series'];
  now: number;
  settings: PresenceReadingSettings;
  label: string;
}

function Sparkline({ series, now, settings, label }: SparklineProps) {
  const windowS = Math.max(1, settings.stale_timeout_s);
  const start = now - windowS;
  const x = (timestamp: number) => {
    const ratio = Math.min(1, Math.max(0, (timestamp - start) / windowS));
    return Math.round(ratio * (SPARK_WIDTH - 4) * 10) / 10 + 2;
  };
  const points = series.map((p) => `${x(p.timestamp)},${sparkY(p.rssi)}`).join(' ');
  const floorY = sparkY(settings.rssi_floor_dbm);
  const last = series[series.length - 1];

  return (
    <svg
      role="img"
      aria-label={label}
      width={SPARK_WIDTH}
      height={SPARK_HEIGHT}
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      className="text-primary-600 dark:text-primary-400"
    >
      <line
        x1={0}
        x2={SPARK_WIDTH}
        y1={floorY}
        y2={floorY}
        strokeDasharray="2 3"
        strokeWidth={1}
        className="stroke-gray-300 dark:stroke-gray-600"
      />
      {series.length > 1 && (
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      )}
      {last && <circle cx={x(last.timestamp)} cy={sparkY(last.rssi)} r={2} fill="currentColor" />}
    </svg>
  );
}

function StatusPill({ status }: { status: PresenceReadingsStatus }) {
  const { t } = useTranslation();
  if (status === 'live') {
    return <Badge color="accent" icon={Radio}>{t('presence.readings.statusLive')}</Badge>;
  }
  if (status === 'denied') {
    return <Badge color="gray" icon={Lock}>{t('presence.readings.statusDenied')}</Badge>;
  }
  return (
    <Badge color={status === 'reconnecting' ? 'amber' : 'gray'} icon={RefreshCw}>
      {status === 'reconnecting'
        ? t('presence.readings.statusReconnecting')
        : t('presence.readings.statusConnecting')}
    </Badge>
  );
}

interface DeviceCardProps {
  device: PresenceReadingDevice;
  snapshot: PresenceReadingsSnapshot;
  formatDbm: (value: number) => string;
}

const TH = 'py-2 px-3 font-medium text-gray-600 dark:text-gray-400';
const TD = 'py-2 px-3 tabular-nums text-gray-900 dark:text-white';
const MUTED = 'text-gray-500 dark:text-gray-400';

function DeviceCard({ device, snapshot, formatDbm }: DeviceCardProps) {
  const { t } = useTranslation();
  const { settings } = snapshot;
  const satellites = summarizeSatellites(device, snapshot.satellites);
  const rooms = summarizeRooms(device, settings);
  const userName = device.user_name ?? t('presence.readings.unknownUser');
  const placed = device.current_room_id !== null;
  const holdRemaining = device.voice_hold_remaining_s ?? 0;
  const held = placed && holdRemaining > 0;
  const pinnedWithoutReading = isPinnedWithoutReading(device, settings);
  const roomLabel = (name: string | null) => name ?? t('presence.readings.unknownRoom');

  return (
    <section className="card" aria-label={userName}>
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 mb-4">
        <div>
          <h3 className="text-base font-medium text-gray-900 dark:text-white">{userName}</h3>
          <p className={`text-xs font-mono ${MUTED}`}>{device.key}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm text-gray-900 dark:text-white" data-testid="placement">
            {placed
              ? t('presence.readings.placedIn', {
                  room: roomLabel(device.current_room_name),
                  confidence: Math.round((device.current_confidence ?? 0) * 100),
                })
              : t('presence.readings.notPlaced')}
          </p>
          {held && (
            <Badge color="accent">
              {t('presence.readings.heldByVoice', { seconds: Math.ceil(holdRemaining) })}
            </Badge>
          )}
          {placed && device.location_uncertain && (
            <Badge color="amber">{t('presence.readings.heldGuess')}</Badge>
          )}
          {placed && device.voice_pinned && !held && !device.location_uncertain && (
            <Badge color="gray">{t('presence.readings.voicePinned')}</Badge>
          )}
        </div>
      </header>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700 text-left">
              <th scope="col" className={TH}>{t('presence.readings.colSatellite')}</th>
              <th scope="col" className={TH}>{t('presence.readings.colRoom')}</th>
              <th scope="col" className={`${TH} text-right`}>{t('presence.readings.colLatest')}</th>
              <th scope="col" className={`${TH} text-right`}>{t('presence.readings.colMean')}</th>
              <th scope="col" className={`${TH} text-right`}>{t('presence.readings.colRange')}</th>
              <th scope="col" className={`${TH} text-right`}>{t('presence.readings.colCount')}</th>
              <th scope="col" className={`${TH} text-right`}>{t('presence.readings.colAge')}</th>
              <th scope="col" className={TH}>{t('presence.readings.colTrend')}</th>
            </tr>
          </thead>
          <tbody>
            {satellites.map((row) => (
              <tr
                key={row.satelliteId}
                data-testid={`satellite-${row.satelliteId}`}
                className="border-b border-gray-100 dark:border-gray-800 last:border-0"
              >
                <td className="py-2 px-3 font-mono text-xs text-gray-900 dark:text-white">
                  {row.satelliteId}
                </td>
                <td className={`py-2 px-3 ${MUTED}`}>{roomLabel(row.roomName)}</td>
                {row.heard && row.latest !== null && row.mean !== null
                  && row.min !== null && row.max !== null && row.ageS !== null ? (
                  <>
                    <td className={`${TD} text-right`}>
                      {formatDbm(row.latest)}
                      {row.latest < settings.rssi_floor_dbm && (
                        <Badge color="amber" className="ml-2">
                          {t('presence.readings.belowFloor')}
                        </Badge>
                      )}
                    </td>
                    <td className={`${TD} text-right`}>{formatDbm(row.mean)}</td>
                    <td className={`${TD} text-right`}>
                      {t('presence.readings.range', {
                        min: formatDbm(row.min),
                        max: formatDbm(row.max),
                      })}
                    </td>
                    <td className={`${TD} text-right`}>{row.count}</td>
                    <td className={`${TD} text-right`}>
                      {t('presence.readings.age', { seconds: Math.round(row.ageS) })}
                    </td>
                    <td className="py-2 px-3">
                      <Sparkline
                        series={row.series}
                        now={snapshot.now}
                        settings={settings}
                        label={t('presence.readings.trendLabel', {
                          satellite: row.satelliteId,
                          seconds: settings.stale_timeout_s,
                        })}
                      />
                    </td>
                  </>
                ) : (
                  <td colSpan={6} className={`py-2 px-3 ${MUTED}`}>
                    {t('presence.readings.notHeard')}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h4 className="mt-6 mb-2 text-sm font-medium text-gray-900 dark:text-white">
        {t('presence.readings.roomsTitle')}
      </h4>
      {settings.rssi_filter_enabled && pinnedWithoutReading && (
        <p className={`mb-2 text-sm ${MUTED}`} data-testid="pinned-note">
          {t('presence.readings.pinnedNoReading', {
            room: roomLabel(device.current_room_name),
            value: formatDbm(settings.rssi_floor_dbm + settings.voice_release_margin_db),
          })}
        </p>
      )}
      {!settings.rssi_filter_enabled ? (
        <p className={`text-sm ${MUTED}`}>{t('presence.readings.filterDisabled')}</p>
      ) : rooms.length === 0 ? (
        <p className={`text-sm ${MUTED}`}>{t('presence.readings.noRooms')}</p>
      ) : (
        <ul className="space-y-2">
          {rooms.map((room) => (
            <li
              key={room.roomId}
              data-testid={`room-${room.roomId}`}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
            >
              <span className="text-gray-900 dark:text-white">{roomLabel(room.roomName)}</span>
              <span className="tabular-nums text-gray-900 dark:text-white">
                {formatDbm(room.filtered)}
              </span>
              {room.isCurrent && (
                <Badge color="accent">{t('presence.readings.currentRoom')}</Badge>
              )}
              {room.needs !== null && room.missing !== null && (
                <span className={`tabular-nums ${MUTED}`}>
                  {room.missing > 0
                    ? t('presence.readings.needs', {
                        value: formatDbm(room.needs),
                        missing: formatDbm(room.missing),
                      })
                    : held
                      ? t('presence.readings.holdBlocks', {
                          value: formatDbm(room.needs),
                          seconds: Math.ceil(holdRemaining),
                        })
                      : t('presence.readings.marginMet', { value: formatDbm(room.needs) })}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function EmptyState({ title, text }: { title: string; text: string }) {
  return (
    <div className="card p-12">
      <Wifi className="w-10 h-10 text-gray-400 mb-4" aria-hidden="true" />
      <h3 className="text-base font-medium text-gray-900 dark:text-white mb-2">{title}</h3>
      <p className={MUTED}>{text}</p>
    </div>
  );
}

export default function LiveReadings() {
  const { t, i18n } = useTranslation();
  const { snapshot, status } = usePresenceReadings();

  const dbmFormat = new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 1 });
  const formatDbm = (value: number) => dbmFormat.format(value);

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 mb-4">
        {/* The live region is the connection status ONLY: it changes on
            connect / disconnect. The timestamp next to it changes every
            second and must stay outside, or a screen reader reads it out
            continuously (DESIGN.md, Accessibility). */}
        <span role="status" aria-live="polite">
          <StatusPill status={status} />
        </span>
        {snapshot && (
          <span className={`text-sm tabular-nums ${MUTED}`}>
            {t('presence.readings.lastUpdate', {
              time: formatTime(snapshot.now * 1000, i18n.language),
            })}
          </span>
        )}
      </div>

      {status === 'reconnecting' && snapshot && (
        <p className="mb-4 text-sm text-amber-600 dark:text-amber-400">
          {t('presence.readings.staleHint')}
        </p>
      )}

      {status === 'denied' ? (
        <EmptyState
          title={t('presence.readings.deniedTitle')}
          text={t('presence.readings.deniedText')}
        />
      ) : snapshot === null ? (
        <EmptyState
          title={
            status === 'reconnecting'
              ? t('presence.readings.statusReconnecting')
              : t('presence.readings.statusConnecting')
          }
          text={
            status === 'reconnecting'
              ? t('presence.readings.reconnectingText')
              : t('presence.readings.connectingText')
          }
        />
      ) : !snapshot.enabled ? (
        <EmptyState title={t('presence.noData')} text={t('presence.presenceDisabled')} />
      ) : (
        <>
          <p className={`mb-6 text-sm ${MUTED}`}>
            {t('presence.readings.legend', {
              floor: formatDbm(snapshot.settings.rssi_floor_dbm),
              margin: formatDbm(snapshot.settings.switch_enter_margin_db),
              window: snapshot.settings.stale_timeout_s,
            })}
            {snapshot.settings.voice_hold_s > 0 && (
              <>
                {' '}
                {t('presence.readings.legendHold', {
                  hold: formatDbm(snapshot.settings.voice_hold_s),
                })}
              </>
            )}
            {snapshot.settings.rssi_filter_enabled
              && snapshot.settings.voice_release_margin_db > 0 && (
              <>
                {' '}
                {t('presence.readings.legendRelease', {
                  value: formatDbm(
                    snapshot.settings.rssi_floor_dbm + snapshot.settings.voice_release_margin_db,
                  ),
                })}
              </>
            )}
          </p>
          {snapshot.devices.length === 0 ? (
            <EmptyState
              title={t('presence.readings.noDevicesTitle')}
              text={t('presence.readings.noDevicesText', {
                satellites: snapshot.satellites.length,
              })}
            />
          ) : (
            <div className="space-y-6">
              {snapshot.devices.map((device) => (
                <DeviceCard
                  key={device.key}
                  device={device}
                  snapshot={snapshot}
                  formatDbm={formatDbm}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
