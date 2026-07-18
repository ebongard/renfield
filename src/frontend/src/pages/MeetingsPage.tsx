import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import {
  Mic, Loader, Clock, CheckCircle, AlertCircle, XCircle,
  Upload, ChevronDown, ChevronRight, FileText, Users, Check, Trash2, X,
  ClipboardList, Sparkles, Plus, RefreshCw, Pencil,
} from 'lucide-react';

import PageHeader from '../components/PageHeader';
import { formatDate, formatDateTime } from '../utils/datetime';
import { useFeatureFlags } from '../api/resources/brain';
import {
  useMeetingsQuery, useUploadMeeting, useMeetingSegments, useRelabelSpeaker, useDeleteMeeting,
  useMinutes, useGenerateMinutes, useUpdateMinutes, useConfirmMinutes, useDeleteMinutes,
  type Meeting, type MeetingSegment, type MinutesBody,
} from '../api/resources/meetings';

function formatClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const mm = Math.floor(s / 60).toString().padStart(2, '0');
  const ss = (s % 60).toString().padStart(2, '0');
  return `${mm}:${ss}`;
}

function StatusBadge({ status }: { status: Meeting['status'] }) {
  const { t } = useTranslation();
  const meta = {
    pending: { Icon: Clock, cls: 'text-gray-500 dark:text-gray-400', spin: false },
    processing: { Icon: Loader, cls: 'text-primary-500', spin: true },
    completed: { Icon: CheckCircle, cls: 'text-green-600 dark:text-green-400', spin: false },
    failed: { Icon: AlertCircle, cls: 'text-red-500', spin: false },
  }[status];
  const { Icon, cls, spin } = meta;
  return (
    <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${cls}`}>
      <Icon className={`w-4 h-4 ${spin ? 'animate-spin' : ''}`} aria-hidden="true" />
      {t(`meetings.status.${status}`)}
    </span>
  );
}

/** Distinct speaker clusters in appearance order — each gets a relabel field. */
function SpeakerLabels({ meetingId, segments }: { meetingId: number; segments: MeetingSegment[] }) {
  const { t } = useTranslation();
  const relabel = useRelabelSpeaker();

  const speakers = useMemo(() => {
    const seen = new Map<string, string>();
    for (const s of segments) {
      if (!seen.has(s.speaker_key)) seen.set(s.speaker_key, s.speaker);
    }
    return Array.from(seen, ([key, name]) => ({ key, name }));
  }, [segments]);

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savedKey, setSavedKey] = useState<string | null>(null);

  const submit = async (speakerKey: string) => {
    const label = (drafts[speakerKey] ?? '').trim();
    if (!label || relabel.isPending) return;
    try {
      await relabel.mutateAsync({ meetingId, speakerKey, label });
      setDrafts((d) => ({ ...d, [speakerKey]: '' }));
      setSavedKey(speakerKey);
    } catch {
      // Error surfaced via relabel.errorMessage; keep the draft.
    }
  };

  return (
    <div className="space-y-2">
      <h4 className="flex items-center gap-1.5 text-sm font-semibold text-gray-900 dark:text-white">
        <Users className="w-4 h-4" aria-hidden="true" />
        {t('meetings.speakers')}
      </h4>
      <p className="text-xs text-gray-500 dark:text-gray-400">{t('meetings.relabelHint')}</p>
      {speakers.map((sp) => (
        <div key={sp.key} className="flex items-center gap-2">
          <span className="w-32 shrink-0 truncate text-sm text-gray-700 dark:text-gray-300" title={sp.name}>
            {sp.name}
          </span>
          <input
            className="input flex-1"
            type="text"
            value={drafts[sp.key] ?? ''}
            onChange={(e) => {
              setDrafts((d) => ({ ...d, [sp.key]: e.target.value }));
              if (savedKey === sp.key) setSavedKey(null);
            }}
            placeholder={t('meetings.relabelPlaceholder')}
            aria-label={t('meetings.relabelAria', { speaker: sp.name })}
            maxLength={100}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(sp.key); } }}
          />
          <button
            type="button"
            className="btn-secondary inline-flex items-center gap-1 whitespace-nowrap"
            disabled={!(drafts[sp.key] ?? '').trim() || relabel.isPending}
            onClick={() => submit(sp.key)}
          >
            {savedKey === sp.key && !relabel.isPending ? (
              <Check className="w-4 h-4 text-green-600 dark:text-green-400" />
            ) : (
              t('meetings.relabelSave')
            )}
          </button>
        </div>
      ))}
      {relabel.errorMessage && (
        <p className="text-sm text-red-600 dark:text-red-400">{relabel.errorMessage}</p>
      )}
    </div>
  );
}

function TranscriptView({ meetingId }: { meetingId: number }) {
  const { t } = useTranslation();
  const segmentsQuery = useMeetingSegments(meetingId, true);
  const segments = segmentsQuery.data ?? [];

  if (segmentsQuery.isLoading) {
    return (
      <div className="py-6 text-center">
        <Loader className="w-6 h-6 animate-spin mx-auto text-gray-400" />
      </div>
    );
  }
  if (segmentsQuery.errorMessage) {
    return <p className="text-sm text-red-600 dark:text-red-400 py-2">{segmentsQuery.errorMessage}</p>;
  }
  if (segments.length === 0) {
    return <p className="text-sm text-gray-500 dark:text-gray-400 py-2">{t('meetings.noSegments')}</p>;
  }

  return (
    <div className="space-y-4">
      <SpeakerLabels meetingId={meetingId} segments={segments} />
      <div className="space-y-2 border-t border-gray-200 dark:border-gray-700 pt-3">
        {segments.map((s, i) => (
          <div key={i} className="text-sm">
            <span className="font-semibold text-gray-900 dark:text-white">{s.speaker}</span>
            <span className="ml-2 text-xs text-gray-400 tabular-nums">{formatClock(s.start_s)}</span>
            <p className="text-gray-700 dark:text-gray-300">{s.text}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function emptyMinutes(): MinutesBody {
  return { summary: '', decisions: [], action_items: [] };
}

/** §2 Phase 3 minutes: generate DRAFT → edit → confirm (renders into the
 *  transcript document). Gated by the caller on meeting_minutes_enabled. */
function MinutesPanel({ meetingId }: { meetingId: number }) {
  const { t } = useTranslation();
  const minutesQuery = useMinutes(meetingId, true);
  const generate = useGenerateMinutes();
  const update = useUpdateMinutes();
  const confirm = useConfirmMinutes();
  const discard = useDeleteMinutes();

  const status = minutesQuery.data?.minutes_status ?? 'none';
  const serverBody = minutesQuery.data?.minutes ?? null;

  // Local editable draft, seeded from the server body. A `draft` meeting is
  // always editing; a `confirmed` one is read-only until the user taps Edit.
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState<MinutesBody>(emptyMinutes());

  // Reseed whenever the server body CONTENT changes (generate / save / confirm /
  // reload). We key on the serialized value, not the object reference, so a
  // referentially-new-but-equal query result doesn't clobber in-progress edits.
  const serverKey = JSON.stringify(serverBody);
  useEffect(() => {
    setBody(serverBody ?? emptyMinutes());
    setEditing(false);
    // serverBody is intentionally read through serverKey (content identity).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);

  const isBusy =
    generate.isPending || update.isPending || confirm.isPending || discard.isPending;
  const errorMessage =
    generate.errorMessage || update.errorMessage || confirm.errorMessage ||
    discard.errorMessage || minutesQuery.errorMessage;

  const patch = (next: Partial<MinutesBody>) => setBody((b) => ({ ...b, ...next }));

  // Dirty = the on-screen body diverges from the last server-saved body. Key
  // order is stable (backend + emptyMinutes + patch all preserve it), so a
  // serialized compare is reliable; a rare false-positive only costs one extra PUT.
  const dirty = JSON.stringify(body) !== serverKey;

  const onGenerate = () => { if (!isBusy) generate.mutate(meetingId); };
  const onSave = () => { if (!isBusy) update.mutate({ meetingId, body }); };
  // Confirm must persist live edits FIRST — otherwise the backend confirms the
  // last-saved draft and the reseed effect silently discards the user's edits
  // (the "generate → tweak → Confirm" data-loss path). PUT reverts to draft, so
  // the immediately-following confirm (which requires draft) still succeeds.
  const onConfirm = async () => {
    if (isBusy) return;
    try {
      if (dirty) await update.mutateAsync({ meetingId, body });
      await confirm.mutateAsync(meetingId);
    } catch {
      // Surfaced via update/confirm.errorMessage; state stays editable.
    }
  };
  const onDiscard = () => { if (!isBusy) discard.mutate(meetingId); };

  const header = (
    <h4 className="flex items-center gap-1.5 text-sm font-semibold text-gray-900 dark:text-white">
      <ClipboardList className="w-4 h-4" aria-hidden="true" />
      {t('meetings.minutes.title')}
    </h4>
  );

  if (minutesQuery.isLoading) {
    return (
      <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
        {header}
        <div className="py-3 text-center">
          <Loader className="w-5 h-5 animate-spin mx-auto text-gray-400" />
        </div>
      </div>
    );
  }

  const showForm = status === 'draft' || editing;

  return (
    <div className="border-t border-gray-200 dark:border-gray-700 pt-3 space-y-3">
      <div className="flex items-center justify-between gap-2">
        {header}
        {status === 'confirmed' && !editing && (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-green-600 dark:text-green-400">
            <CheckCircle className="w-3.5 h-3.5" aria-hidden="true" />
            {t('meetings.minutes.confirmedBadge')}
          </span>
        )}
      </div>

      {status === 'none' ? (
        <div className="space-y-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">{t('meetings.minutes.noneHint')}</p>
          <button
            type="button"
            className="btn-primary inline-flex items-center gap-2"
            onClick={onGenerate}
            disabled={isBusy}
          >
            {generate.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
            {t('meetings.minutes.generate')}
          </button>
        </div>
      ) : showForm ? (
        <MinutesForm body={body} onChange={patch} disabled={isBusy} />
      ) : (
        <MinutesReadonly body={body} />
      )}

      {status !== 'none' && (
        <div className="flex flex-wrap items-center gap-2">
          {showForm ? (
            <>
              <button
                type="button"
                className="btn-secondary inline-flex items-center gap-1.5"
                onClick={onSave}
                disabled={isBusy}
              >
                {update.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                {t('meetings.minutes.save')}
              </button>
              {status === 'draft' && (
                <button
                  type="button"
                  className="btn-primary inline-flex items-center gap-1.5"
                  onClick={onConfirm}
                  disabled={isBusy}
                >
                  {confirm.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
                  {t('meetings.minutes.confirm')}
                </button>
              )}
              <button
                type="button"
                className="btn-secondary inline-flex items-center gap-1.5"
                onClick={onGenerate}
                disabled={isBusy}
                title={t('meetings.minutes.regenerateHint')}
              >
                <RefreshCw className="w-4 h-4" />
                {t('meetings.minutes.regenerate')}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="btn-secondary inline-flex items-center gap-1.5"
              onClick={() => setEditing(true)}
              disabled={isBusy}
            >
              <Pencil className="w-4 h-4" />
              {t('meetings.minutes.edit')}
            </button>
          )}
          <button
            type="button"
            className="ml-auto p-1.5 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700"
            onClick={onDiscard}
            disabled={isBusy}
            aria-label={t('meetings.minutes.discard')}
            title={t('meetings.minutes.discard')}
          >
            {discard.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
          </button>
        </div>
      )}

      {status === 'confirmed' && !editing && (
        <p className="text-xs text-gray-500 dark:text-gray-400">{t('meetings.minutes.confirmedHint')}</p>
      )}
      {errorMessage && <p className="text-sm text-red-600 dark:text-red-400">{errorMessage}</p>}
    </div>
  );
}

/** Read-only rendering of confirmed minutes. */
function MinutesReadonly({ body }: { body: MinutesBody }) {
  const { t } = useTranslation();
  const empty = !body.summary && body.decisions.length === 0 && body.action_items.length === 0;
  if (empty) {
    return <p className="text-sm text-gray-500 dark:text-gray-400">{t('meetings.minutes.emptyBody')}</p>;
  }
  return (
    <div className="space-y-3 text-sm">
      {body.summary && (
        <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{body.summary}</p>
      )}
      {body.decisions.length > 0 && (
        <div>
          <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
            {t('meetings.minutes.decisions')}
          </h5>
          <ul className="list-disc list-inside space-y-0.5 text-gray-700 dark:text-gray-300">
            {body.decisions.map((d, i) => (
              <li key={i}>
                {d.text}
                {d.made_by && <span className="text-gray-500 dark:text-gray-400"> — {d.made_by}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {body.action_items.length > 0 && (
        <div>
          <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
            {t('meetings.minutes.actionItems')}
          </h5>
          <ul className="list-disc list-inside space-y-0.5 text-gray-700 dark:text-gray-300">
            {body.action_items.map((a, i) => (
              <li key={i}>
                {a.text}
                {a.owner && <span className="text-gray-500 dark:text-gray-400"> — {a.owner}</span>}
                {a.due_hint && <span className="text-gray-500 dark:text-gray-400"> ({a.due_hint})</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** Editable draft form for the minutes body. */
function MinutesForm({
  body, onChange, disabled,
}: {
  body: MinutesBody;
  onChange: (next: Partial<MinutesBody>) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();

  const setDecision = (i: number, field: 'text' | 'made_by', value: string) => {
    const decisions = body.decisions.map((d, j) => (j === i ? { ...d, [field]: value } : d));
    onChange({ decisions });
  };
  const addDecision = () => onChange({ decisions: [...body.decisions, { text: '', made_by: '' }] });
  const removeDecision = (i: number) => onChange({ decisions: body.decisions.filter((_, j) => j !== i) });

  const setAction = (i: number, field: 'text' | 'owner' | 'due_hint', value: string) => {
    const action_items = body.action_items.map((a, j) => (j === i ? { ...a, [field]: value } : a));
    onChange({ action_items });
  };
  const addAction = () =>
    onChange({ action_items: [...body.action_items, { text: '', owner: '', due_hint: '' }] });
  const removeAction = (i: number) =>
    onChange({ action_items: body.action_items.filter((_, j) => j !== i) });

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
          {t('meetings.minutes.summary')}
        </label>
        <textarea
          className="input"
          value={body.summary}
          onChange={(e) => onChange({ summary: e.target.value })}
          placeholder={t('meetings.minutes.summaryPlaceholder')}
          aria-label={t('meetings.minutes.summary')}
          rows={3}
          maxLength={4000}
          disabled={disabled}
        />
      </div>

      <div className="space-y-2">
        <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
          {t('meetings.minutes.decisions')}
        </h5>
        {body.decisions.map((d, i) => (
          <div key={i} className="flex items-start gap-2">
            <input
              className="input flex-1"
              type="text"
              value={d.text}
              onChange={(e) => setDecision(i, 'text', e.target.value)}
              placeholder={t('meetings.minutes.decisionPlaceholder')}
              aria-label={t('meetings.minutes.decisionAria', { n: i + 1 })}
              maxLength={1000}
              disabled={disabled}
            />
            <input
              className="input w-32 shrink-0"
              type="text"
              value={d.made_by}
              onChange={(e) => setDecision(i, 'made_by', e.target.value)}
              placeholder={t('meetings.minutes.byPlaceholder')}
              aria-label={t('meetings.minutes.madeByAria', { n: i + 1 })}
              maxLength={200}
              disabled={disabled}
            />
            <button
              type="button"
              className="p-2 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700 shrink-0"
              onClick={() => removeDecision(i)}
              disabled={disabled}
              aria-label={t('meetings.minutes.removeDecision', { n: i + 1 })}
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        ))}
        <button
          type="button"
          className="inline-flex items-center gap-1 text-sm text-primary-600 dark:text-primary-400 hover:underline"
          onClick={addDecision}
          disabled={disabled}
        >
          <Plus className="w-3.5 h-3.5" />
          {t('meetings.minutes.addDecision')}
        </button>
      </div>

      <div className="space-y-2">
        <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
          {t('meetings.minutes.actionItems')}
        </h5>
        {body.action_items.map((a, i) => (
          <div key={i} className="flex items-start gap-2">
            <input
              className="input flex-1"
              type="text"
              value={a.text}
              onChange={(e) => setAction(i, 'text', e.target.value)}
              placeholder={t('meetings.minutes.actionPlaceholder')}
              aria-label={t('meetings.minutes.actionAria', { n: i + 1 })}
              maxLength={1000}
              disabled={disabled}
            />
            <input
              className="input w-28 shrink-0"
              type="text"
              value={a.owner}
              onChange={(e) => setAction(i, 'owner', e.target.value)}
              placeholder={t('meetings.minutes.ownerPlaceholder')}
              aria-label={t('meetings.minutes.ownerAria', { n: i + 1 })}
              maxLength={200}
              disabled={disabled}
            />
            <input
              className="input w-28 shrink-0"
              type="text"
              value={a.due_hint}
              onChange={(e) => setAction(i, 'due_hint', e.target.value)}
              placeholder={t('meetings.minutes.duePlaceholder')}
              aria-label={t('meetings.minutes.dueAria', { n: i + 1 })}
              maxLength={200}
              disabled={disabled}
            />
            <button
              type="button"
              className="p-2 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700 shrink-0"
              onClick={() => removeAction(i)}
              disabled={disabled}
              aria-label={t('meetings.minutes.removeAction', { n: i + 1 })}
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        ))}
        <button
          type="button"
          className="inline-flex items-center gap-1 text-sm text-primary-600 dark:text-primary-400 hover:underline"
          onClick={addAction}
          disabled={disabled}
        >
          <Plus className="w-3.5 h-3.5" />
          {t('meetings.minutes.addAction')}
        </button>
      </div>
    </div>
  );
}

function MeetingCard({ meeting, minutesEnabled }: { meeting: Meeting; minutesEnabled: boolean }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const deleteMeeting = useDeleteMeeting();
  const canExpand = meeting.status === 'completed';

  const handleDelete = async () => {
    if (deleteMeeting.isPending) return;
    try {
      await deleteMeeting.mutateAsync(meeting.id);
      // Row disappears when the list refetches; nothing else to do.
    } catch {
      setConfirmingDelete(false); // surfaced via errorMessage below
    }
  };

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-4">
        <button
          type="button"
          className="min-w-0 flex items-start gap-2 text-left"
          onClick={() => canExpand && setExpanded((v) => !v)}
          disabled={!canExpand}
          aria-expanded={canExpand ? expanded : undefined}
        >
          {canExpand && (
            expanded
              ? <ChevronDown className="w-4 h-4 mt-1 shrink-0 text-gray-400" />
              : <ChevronRight className="w-4 h-4 mt-1 shrink-0 text-gray-400" />
          )}
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-gray-900 dark:text-white truncate">
              {meeting.title || t('meetings.untitled')}
            </h3>
            <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              {meeting.date && <span>{formatDate(meeting.date)}</span>}
              {meeting.created_at && (
                <span>{t('meetings.uploaded')}: {formatDateTime(meeting.created_at)}</span>
              )}
            </div>
          </div>
        </button>
        <div className="flex items-center gap-2 shrink-0">
          <StatusBadge status={meeting.status} />
          {confirmingDelete ? (
            <span className="inline-flex items-center gap-1">
              <button
                type="button"
                className="p-1 rounded text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30"
                onClick={handleDelete}
                disabled={deleteMeeting.isPending}
                aria-label={t('meetings.confirmDelete')}
                title={t('meetings.confirmDelete')}
              >
                {deleteMeeting.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              </button>
              <button
                type="button"
                className="p-1 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
                onClick={() => setConfirmingDelete(false)}
                aria-label={t('meetings.cancelDelete')}
                title={t('meetings.cancelDelete')}
              >
                <X className="w-4 h-4" />
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="p-1 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700"
              onClick={() => setConfirmingDelete(true)}
              aria-label={t('meetings.delete')}
              title={t('meetings.delete')}
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {meeting.status === 'failed' && meeting.error && (
        <p className="mt-2 text-sm text-red-600 dark:text-red-400">{meeting.error}</p>
      )}
      {deleteMeeting.errorMessage && (
        <p className="mt-2 text-sm text-red-600 dark:text-red-400">{deleteMeeting.errorMessage}</p>
      )}

      {canExpand && meeting.transcript_document_id != null && (
        <Link
          to={`/knowledge?doc=${meeting.transcript_document_id}`}
          className="mt-2 inline-flex items-center gap-1 text-sm text-primary-600 dark:text-primary-400 hover:underline"
        >
          <FileText className="w-3.5 h-3.5" />
          {t('meetings.openTranscript')}
        </Link>
      )}

      {canExpand && expanded && (
        <div className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-4 space-y-4">
          <TranscriptView meetingId={meeting.id} />
          {minutesEnabled && <MinutesPanel meetingId={meeting.id} />}
        </div>
      )}
    </div>
  );
}

export default function MeetingsPage() {
  const { t } = useTranslation();
  const meetingsQuery = useMeetingsQuery();
  const upload = useUploadMeeting();
  const { data: featureFlags } = useFeatureFlags();
  const minutesEnabled = featureFlags?.meeting_minutes_enabled ?? false;
  const meetings = meetingsQuery.data ?? [];

  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [date, setDate] = useState('');
  const [consentNote, setConsentNote] = useState('');
  const [consent, setConsent] = useState(false);

  const canSubmit = file != null && consent && !upload.isPending;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit || !file) return;
    try {
      await upload.mutateAsync({
        audio: file,
        consentConfirmed: consent,
        title: title.trim() || undefined,
        date: date || undefined,
        consentNote: consentNote.trim() || undefined,
      });
      setFile(null);
      setTitle('');
      setDate('');
      setConsentNote('');
      setConsent(false);
    } catch {
      // Error surfaced via upload.errorMessage; keep the form filled.
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader icon={Mic} title={t('meetings.title')} subtitle={t('meetings.subtitle')} />

      {/* Upload form */}
      <form onSubmit={handleSubmit} className="card space-y-3">
        <h2 className="text-base font-semibold text-gray-900 dark:text-white">
          {t('meetings.uploadTitle')}
        </h2>

        <input
          className="input"
          type="file"
          accept="audio/*,video/mp4,.m4a,.opus,.webm"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          aria-label={t('meetings.audioLabel')}
        />

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <input
            className="input"
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t('meetings.titlePlaceholder')}
            aria-label={t('meetings.titlePlaceholder')}
            maxLength={255}
          />
          <input
            className="input"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            aria-label={t('meetings.datePlaceholder')}
          />
        </div>

        <textarea
          className="input"
          value={consentNote}
          onChange={(e) => setConsentNote(e.target.value)}
          placeholder={t('meetings.consentNotePlaceholder')}
          aria-label={t('meetings.consentNotePlaceholder')}
          rows={2}
        />

        {/* Consent is mandatory (DE workplace recording) — the backend 422s without it. */}
        <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={consent}
            onChange={(e) => setConsent(e.target.checked)}
          />
          <span>{t('meetings.consentLabel')}</span>
        </label>

        {upload.errorMessage && (
          <p className="text-sm text-red-600 dark:text-red-400">{upload.errorMessage}</p>
        )}

        <button
          type="submit"
          className="btn-primary inline-flex items-center gap-2"
          disabled={!canSubmit}
        >
          {upload.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
          {upload.isPending ? t('meetings.uploading') : t('meetings.upload')}
        </button>
      </form>

      {/* List */}
      <div className="space-y-4">
        {meetingsQuery.isLoading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('meetings.loading')}</p>
          </div>
        ) : meetingsQuery.errorMessage ? (
          <div className="card text-center py-12">
            <XCircle className="w-12 h-12 mx-auto text-red-500 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{meetingsQuery.errorMessage}</p>
          </div>
        ) : meetings.length === 0 ? (
          <div className="card text-center py-12">
            <Mic className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{t('meetings.empty')}</p>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('meetings.emptyDesc')}</p>
          </div>
        ) : (
          meetings.map((m) => (
            <MeetingCard key={m.id} meeting={m} minutesEnabled={minutesEnabled} />
          ))
        )}
      </div>
    </div>
  );
}
