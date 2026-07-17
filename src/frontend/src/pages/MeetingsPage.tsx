import { useMemo, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import {
  Mic, Loader, Clock, CheckCircle, AlertCircle, XCircle,
  Upload, ChevronDown, ChevronRight, FileText, Users, Check,
} from 'lucide-react';

import PageHeader from '../components/PageHeader';
import {
  useMeetingsQuery, useUploadMeeting, useMeetingSegments, useRelabelSpeaker,
  type Meeting, type MeetingSegment,
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
            onChange={(e) => setDrafts((d) => ({ ...d, [sp.key]: e.target.value }))}
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

function MeetingCard({ meeting }: { meeting: Meeting }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const canExpand = meeting.status === 'completed';

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
              {meeting.date && <span>{new Date(meeting.date).toLocaleDateString()}</span>}
              <span>{t('meetings.uploaded')}: {new Date(meeting.created_at).toLocaleString()}</span>
            </div>
          </div>
        </button>
        <StatusBadge status={meeting.status} />
      </div>

      {meeting.status === 'failed' && meeting.error && (
        <p className="mt-2 text-sm text-red-600 dark:text-red-400">{meeting.error}</p>
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
        <div className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-4">
          <TranscriptView meetingId={meeting.id} />
        </div>
      )}
    </div>
  );
}

export default function MeetingsPage() {
  const { t } = useTranslation();
  const meetingsQuery = useMeetingsQuery();
  const upload = useUploadMeeting();
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
          meetings.map((m) => <MeetingCard key={m.id} meeting={m} />)
        )}
      </div>
    </div>
  );
}
