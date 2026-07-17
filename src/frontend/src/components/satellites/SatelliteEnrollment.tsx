/**
 * Satellite enrollment admin UI (security review H1, PR-C).
 *
 * Mints a per-satellite enrollment PSK so a satellite can prove its identity on
 * the /ws/satellite register frame (instead of merely claiming a satellite_id).
 * The token is shown EXACTLY ONCE on enroll/rotate — provision it to the
 * satellite (Ansible host_var `satellite_enrollment_token` or a k8s Secret).
 *
 * Patterned on components/presence/IrkPairing.tsx.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '../../utils/datetime';

import {
  useSatelliteEnrollmentsQuery,
  useEnrollmentStatusQuery,
  useEnrollSatellite,
  useRevokeSatelliteEnrollment,
  type EnrollResult,
} from '../../api/resources/satelliteEnrollment';
import { useSatellitesQuery } from '../../api/resources/satellites';

export default function SatelliteEnrollment() {
  const { t } = useTranslation();
  const enrollmentsQuery = useSatelliteEnrollmentsQuery();
  const statusQuery = useEnrollmentStatusQuery();
  const satellitesQuery = useSatellitesQuery(false);
  const enroll = useEnrollSatellite();
  const revoke = useRevokeSatelliteEnrollment();

  const enrollments = enrollmentsQuery.data ?? [];
  const status = statusQuery.data;
  const connected = satellitesQuery.data?.satellites ?? [];

  const [satelliteId, setSatelliteId] = useState('');
  const [room, setRoom] = useState('');
  const [minted, setMinted] = useState<EnrollResult | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const enrolledIds = new Set(enrollments.map((e) => e.satellite_id));
  const canEnroll = satelliteId.trim().length > 0 && !enroll.isPending;

  // `override` lets the per-row Rotate action pass the row's id/room directly —
  // state setters only queue a re-render, so reading `satelliteId`/`room` from
  // the closure in the same tick would send STALE form values (wrong satellite
  // or a phantom enroll). The form-driven Enroll path passes no override.
  const doEnroll = async (
    rotate: boolean,
    override?: { satelliteId: string; room: string },
  ) => {
    setError(null);
    setMinted(null);
    setCopied(false);
    setCopyFailed(false);
    const sid = (override?.satelliteId ?? satelliteId).trim();
    const rm = (override?.room ?? room).trim();
    try {
      const result = await enroll.mutateAsync({
        satellite_id: sid,
        room: rm || null,
        rotate,
      });
      setMinted(result);
      if (!rotate) {
        setSatelliteId('');
        setRoom('');
      }
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      setError(
        // 409 = already enrolled → point the admin at Rotate. 422 = malformed
        // satellite_id → tell them it's an input-format problem, not a server error.
        status === 409
          ? t('satellites.enrollment.alreadyEnrolled')
          : status === 422
            ? t('satellites.enrollment.invalidIdFormat')
            : t('satellites.enrollment.enrollFailed'),
      );
    }
  };

  const copyToken = async () => {
    if (!minted) return;
    try {
      await navigator.clipboard.writeText(minted.token);
      setCopied(true);
      setCopyFailed(false);
    } catch {
      // The token is shown ONCE — a silently-failed copy could lose it. Tell
      // the user to select it manually instead of leaving the button inert.
      setCopied(false);
      setCopyFailed(true);
    }
  };

  const fmt = (iso: string | null) =>
    iso ? formatDateTime(iso) : t('satellites.enrollment.never');

  return (
    <section className="card mt-6">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
        {t('satellites.enrollment.title')}
      </h2>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
        {t('satellites.enrollment.description')}
      </p>

      {/* Fleet enforcement status */}
      {status && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span
            className={`rounded-full px-2 py-0.5 font-medium ${
              status.enabled
                ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
            }`}
          >
            {status.enabled
              ? t('satellites.enrollment.statusEnabled')
              : t('satellites.enrollment.statusDisabled')}
          </span>
          {status.enabled && (
            <span
              className={`rounded-full px-2 py-0.5 font-medium ${
                status.enforcing
                  ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300'
                  : 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300'
              }`}
            >
              {status.enforcing
                ? t('satellites.enrollment.statusEnforcing')
                : t('satellites.enrollment.statusPermissive')}
            </span>
          )}
          {status.pending_first_auth > 0 && (
            <span className="text-gray-500 dark:text-gray-400">
              {t('satellites.enrollment.pendingCount', { count: status.pending_first_auth })}
            </span>
          )}
        </div>
      )}

      {/* Enroll form */}
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <input
          className="input"
          type="text"
          list="enrollment-connected-sats"
          value={satelliteId}
          placeholder={t('satellites.enrollment.satelliteIdPlaceholder')}
          onChange={(e) => setSatelliteId(e.target.value)}
          aria-label={t('satellites.enrollment.satelliteId')}
        />
        <datalist id="enrollment-connected-sats">
          {/* Suggest connected, not-yet-enrolled satellite IDs. The room rides
              on the `label` attribute (not text content) so it doesn't collide
              with the satellite cards' room text in the DOM. */}
          {connected
            .filter((s) => !enrolledIds.has(s.satellite_id))
            .map((s) => (
              <option key={s.satellite_id} value={s.satellite_id} label={s.room || undefined} />
            ))}
        </datalist>

        <input
          className="input"
          type="text"
          value={room}
          placeholder={t('satellites.enrollment.roomPlaceholder')}
          onChange={(e) => setRoom(e.target.value)}
          aria-label={t('satellites.enrollment.room')}
        />

        <button className="btn-primary" disabled={!canEnroll} onClick={() => doEnroll(false)}>
          {enroll.isPending
            ? t('satellites.enrollment.enrolling')
            : t('satellites.enrollment.enroll')}
        </button>
      </div>

      {error && <p className="mt-2 text-sm text-red-600 dark:text-red-400">{error}</p>}

      {/* One-time token reveal */}
      {minted && (
        <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-700 dark:bg-amber-900/20">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
            {minted.rotated
              ? t('satellites.enrollment.tokenRotated', { id: minted.satellite_id })
              : t('satellites.enrollment.tokenMinted', { id: minted.satellite_id })}
          </p>
          <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
            {t('satellites.enrollment.tokenOnceWarning')}
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="flex-1 break-all rounded-sm bg-white px-2 py-1 font-mono text-xs text-gray-800 dark:bg-gray-800 dark:text-gray-100">
              {minted.token}
            </code>
            <button className="btn-secondary text-sm" onClick={copyToken}>
              {copied ? t('satellites.enrollment.copied') : t('satellites.enrollment.copy')}
            </button>
          </div>
          {copyFailed && (
            <p className="mt-2 text-xs text-red-600 dark:text-red-400">
              {t('satellites.enrollment.copyFailed')}
            </p>
          )}
        </div>
      )}

      {/* Enrolled satellites */}
      {enrollments.length > 0 && (
        <ul className="mt-5 divide-y divide-gray-200 dark:divide-gray-700">
          {enrollments.map((e) => (
            <li key={e.id} className="flex items-center justify-between py-2">
              <span className="text-sm text-gray-800 dark:text-gray-200">
                <span
                  className={`mr-2 inline-block h-2 w-2 rounded-full align-middle ${
                    e.connected ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'
                  }`}
                  title={
                    e.connected
                      ? t('satellites.enrollment.connected')
                      : t('satellites.enrollment.offline')
                  }
                />
                {e.satellite_id}
                {e.room && <span className="text-gray-400"> · {e.room}</span>}
                {!e.is_enabled && (
                  <span className="ml-2 text-xs text-red-500">
                    ({t('satellites.enrollment.revoked')})
                  </span>
                )}
                <span className="ml-2 text-xs text-gray-400">
                  {t('satellites.enrollment.lastAuth')}: {fmt(e.last_authenticated_at)}
                </span>
              </span>
              <div className="flex items-center gap-2">
                <button
                  className="btn-secondary text-sm"
                  onClick={() =>
                    void doEnroll(true, { satelliteId: e.satellite_id, room: e.room ?? '' })
                  }
                  disabled={enroll.isPending}
                >
                  {t('satellites.enrollment.rotate')}
                </button>
                {e.is_enabled && (
                  <button
                    className="btn-secondary text-sm"
                    onClick={() => revoke.mutate(e.satellite_id)}
                    disabled={revoke.isPending}
                  >
                    {t('satellites.enrollment.revoke')}
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
