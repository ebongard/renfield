import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import {
  Mic, Loader, XCircle, Upload, FileText, Trash2, Check, X,
} from 'lucide-react';

import PageHeader from '../components/PageHeader';
import { formatDate, formatDateTime } from '../utils/datetime';
import { useFeatureFlags } from '../api/resources/brain';
import StatusBadge from '../components/meetings/StatusBadge';
import ProjectSelect from '../components/meetings/ProjectSelect';
import {
  useMeetingsQuery, useUploadMeeting, useDeleteMeeting, useUpdateMeetingProject,
  type Meeting,
} from '../api/resources/meetings';
import { useProjectsQuery, type Project } from '../api/resources/projects';

/**
 * List card. A completed meeting is a LINK to its dedicated detail page
 * (§2 Track D) — the transcript + minutes deliverable lives there, not inline.
 * Non-completed meetings (pending/processing/failed) render as static rows.
 */
function MeetingCard({
  meeting, minutesEnabled, projects,
}: { meeting: Meeting; minutesEnabled: boolean; projects: Project[] }) {
  const { t } = useTranslation();
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const deleteMeeting = useDeleteMeeting();
  const updateProject = useUpdateMeetingProject();
  const isCompleted = meeting.status === 'completed';
  const detailPath = `/meetings/${meeting.id}`;

  const handleDelete = async () => {
    if (deleteMeeting.isPending) return;
    try {
      await deleteMeeting.mutateAsync(meeting.id);
      // Row disappears when the list refetches; nothing else to do.
    } catch {
      setConfirmingDelete(false); // surfaced via errorMessage below
    }
  };

  const heading = (
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
  );

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-4">
        {/* Completed → whole heading links to the detail page; otherwise static. */}
        {isCompleted ? (
          <Link
            to={detailPath}
            className="min-w-0 flex items-start gap-2 text-left group"
            aria-label={t('meetings.openDetailFor', { title: meeting.title || t('meetings.untitled') })}
          >
            <FileText className="w-4 h-4 mt-1 shrink-0 text-gray-400 group-hover:text-primary-500" aria-hidden="true" />
            {heading}
          </Link>
        ) : (
          <div className="min-w-0 flex items-start gap-2">{heading}</div>
        )}
        <div className="flex items-center gap-2 shrink-0">
          {minutesEnabled && isCompleted && meeting.minutes_status === 'draft' && (
            <Link
              to={detailPath}
              className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/30 hover:bg-amber-100 dark:hover:bg-amber-900/50"
              title={t('meetings.minutes.draftReadyHint')}
            >
              <FileText className="w-3 h-3" aria-hidden="true" />
              {t('meetings.minutes.draftReadyBadge')}
            </Link>
          )}
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

      {/* Project link — change or clear it anytime (Phase 4A). Only shown where
          projects exist. Invalidates project timelines on change. */}
      {projects.length > 0 && (
        <div className="mt-2 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <span className="shrink-0">{t('meetings.projectLabel')}:</span>
          <ProjectSelect
            projects={projects}
            value={meeting.project_id}
            onChange={(id) => updateProject.mutate({ meetingId: meeting.id, projectId: id })}
            disabled={updateProject.isPending}
            ariaLabel={t('meetings.projectForMeeting', { title: meeting.title || t('meetings.untitled') })}
            className="input py-1 text-xs max-w-[16rem]"
          />
          {updateProject.isPending && <Loader className="w-3.5 h-3.5 animate-spin shrink-0" />}
        </div>
      )}

      {meeting.status === 'failed' && meeting.error && (
        <p className="mt-2 text-sm text-red-600 dark:text-red-400">{meeting.error}</p>
      )}
      {deleteMeeting.errorMessage && (
        <p className="mt-2 text-sm text-red-600 dark:text-red-400">{deleteMeeting.errorMessage}</p>
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
  // Projects are optional (Phase 4A) — the picker only appears where projects
  // exist (i.e. projects_enabled instances). Gate the query on the flag so a
  // projects-disabled instance doesn't 404-retry /api/projects on every mount.
  const projects = useProjectsQuery(featureFlags?.projects_enabled ?? false).data ?? [];

  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [date, setDate] = useState('');
  const [consentNote, setConsentNote] = useState('');
  const [consent, setConsent] = useState(false);
  const [projectId, setProjectId] = useState<number | null>(null);
  // Default 'auto' = whisper detects the spoken language — the meeting ASR used
  // to be hardcoded to German, so English recordings came back as German.
  const [language, setLanguage] = useState('auto');

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
        projectId,
        language,
      });
      setFile(null);
      setTitle('');
      setDate('');
      setConsentNote('');
      setConsent(false);
      setProjectId(null);
      setLanguage('auto');
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

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
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
          <select
            className="input"
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            aria-label={t('meetings.languageLabel')}
          >
            <option value="auto">{t('meetings.languageAuto')}</option>
            <option value="de">{t('meetings.languageDe')}</option>
            <option value="en">{t('meetings.languageEn')}</option>
          </select>
        </div>

        {/* Optional project scope — only when projects exist (Phase 4A). */}
        {projects.length > 0 && (
          <ProjectSelect
            projects={projects}
            value={projectId}
            onChange={setProjectId}
            disabled={upload.isPending}
            ariaLabel={t('meetings.projectLabel')}
          />
        )}

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
            <MeetingCard key={m.id} meeting={m} minutesEnabled={minutesEnabled} projects={projects} />
          ))
        )}
      </div>
    </div>
  );
}
