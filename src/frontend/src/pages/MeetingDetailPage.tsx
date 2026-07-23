import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate, useParams } from 'react-router';
import {
  ArrowLeft, Loader, XCircle, FileText, Trash2, Check, X, ChevronDown, ChevronRight, AlertCircle,
} from 'lucide-react';

import { formatDate, formatDateTime } from '../utils/datetime';
import { useFeatureFlags } from '../api/resources/brain';
import StatusBadge from '../components/meetings/StatusBadge';
import TranscriptView from '../components/meetings/TranscriptView';
import MinutesPanel from '../components/meetings/MinutesPanel';
import ProjectSelect from '../components/meetings/ProjectSelect';
import {
  useMeeting, useDeleteMeeting, useUpdateMeetingProject,
} from '../api/resources/meetings';
import { useProjectsQuery } from '../api/resources/projects';

/**
 * Dedicated meeting detail page (§2 Track D). The deliverable — the minutes
 * (summary / decisions / action items) — is the DEFAULT view at the top; the
 * raw transcript is secondary, collapsed below. A draft-confirm nudge banner
 * keeps generated minutes from rotting unconfirmed.
 */
export default function MeetingDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const params = useParams<{ id: string }>();
  const meetingId = Number(params.id);
  const validId = Number.isInteger(meetingId) && meetingId > 0;

  const meetingQuery = useMeeting(validId ? meetingId : null);
  const meeting = meetingQuery.data;

  const { data: featureFlags } = useFeatureFlags();
  const minutesEnabled = featureFlags?.meeting_minutes_enabled ?? false;
  const projects = useProjectsQuery(featureFlags?.projects_enabled ?? false).data ?? [];

  const deleteMeeting = useDeleteMeeting();
  const updateProject = useUpdateMeetingProject();

  const [confirmingDelete, setConfirmingDelete] = useState(false);
  // Minutes-first: the transcript is the raw material, collapsed by default when
  // minutes are enabled. Without minutes it is the only content → shown open.
  const [showTranscript, setShowTranscript] = useState(!minutesEnabled);

  const backLink = (
    <Link
      to="/meetings"
      className="inline-flex items-center gap-1 text-sm text-primary-600 dark:text-primary-400 hover:underline"
    >
      <ArrowLeft className="w-4 h-4" aria-hidden="true" />
      {t('meetings.backToList')}
    </Link>
  );

  if (!validId || (meetingQuery.errorMessage && !meeting)) {
    return (
      <div className="space-y-6">
        {backLink}
        <div className="card text-center py-12">
          <XCircle className="w-12 h-12 mx-auto text-red-500 mb-3" />
          <p className="font-medium text-gray-700 dark:text-gray-300">
            {validId ? meetingQuery.errorMessage : t('meetings.notFound')}
          </p>
        </div>
      </div>
    );
  }

  if (meetingQuery.isLoading || !meeting) {
    return (
      <div className="space-y-6">
        {backLink}
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400" />
        </div>
      </div>
    );
  }

  const handleDelete = async () => {
    if (deleteMeeting.isPending) return;
    try {
      await deleteMeeting.mutateAsync(meeting.id);
      navigate('/meetings');
    } catch {
      setConfirmingDelete(false); // surfaced via errorMessage below
    }
  };

  const isCompleted = meeting.status === 'completed';
  const showDraftNudge = isCompleted && minutesEnabled && meeting.minutes_status === 'draft';

  return (
    <div className="space-y-6">
      {backLink}

      {/* Header */}
      <div className="card space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold text-gray-900 dark:text-white break-words">
              {meeting.title || t('meetings.untitled')}
            </h1>
            <div className="flex flex-wrap items-center gap-3 text-xs text-gray-500 dark:text-gray-400 mt-1">
              {meeting.date && <span>{formatDate(meeting.date)}</span>}
              {meeting.created_at && (
                <span>{t('meetings.uploaded')}: {formatDateTime(meeting.created_at)}</span>
              )}
            </div>
          </div>
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

        {/* Project link — change or clear it anytime (Phase 4A). */}
        {projects.length > 0 && (
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <span className="shrink-0">{t('meetings.projectLabel')}:</span>
            <ProjectSelect
              projects={projects}
              value={meeting.project_id}
              onChange={(id) => updateProject.mutate({ meetingId: meeting.id, projectId: id })}
              disabled={updateProject.isPending}
              ariaLabel={t('meetings.projectForMeeting', { title: meeting.title || t('meetings.untitled') })}
              className="input py-1 text-sm max-w-[16rem]"
            />
            {updateProject.isPending && <Loader className="w-3.5 h-3.5 animate-spin shrink-0" />}
          </div>
        )}

        {meeting.status === 'failed' && meeting.error && (
          <p className="text-sm text-red-600 dark:text-red-400">{meeting.error}</p>
        )}
        {deleteMeeting.errorMessage && (
          <p className="text-sm text-red-600 dark:text-red-400">{deleteMeeting.errorMessage}</p>
        )}

        {isCompleted && meeting.transcript_document_id != null && (
          <Link
            to={`/knowledge?doc=${meeting.transcript_document_id}`}
            className="inline-flex items-center gap-1 text-sm text-primary-600 dark:text-primary-400 hover:underline"
          >
            <FileText className="w-3.5 h-3.5" />
            {t('meetings.openTranscript')}
          </Link>
        )}
      </div>

      {/* Still transcribing / failed → no deliverable yet. */}
      {!isCompleted && meeting.status !== 'failed' && (
        <div className="card text-center py-10 text-gray-500 dark:text-gray-400">
          <Loader className="w-6 h-6 animate-spin mx-auto mb-2" />
          <p>{t('meetings.stillProcessing')}</p>
        </div>
      )}

      {isCompleted && (
        <>
          {/* Draft-confirm nudge — the whole point of Track D: don't let a
              generated draft rot unseen. */}
          {showDraftNudge && (
            <div className="card border-l-4 border-amber-400 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-500">
              <div className="flex items-start gap-2">
                <AlertCircle className="w-5 h-5 shrink-0 text-amber-500 dark:text-amber-400 mt-0.5" aria-hidden="true" />
                <div>
                  <p className="font-medium text-amber-800 dark:text-amber-200">
                    {t('meetings.draftNudgeTitle')}
                  </p>
                  <p className="text-sm text-amber-700 dark:text-amber-300 mt-0.5">
                    {t('meetings.draftNudgeBody')}
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* Deliverable first: minutes at the top. */}
          {minutesEnabled && (
            <div className="card">
              <MinutesPanel meetingId={meeting.id} />
            </div>
          )}

          {/* Raw transcript — secondary. Collapsed when minutes lead the page. */}
          <div className="card">
            {minutesEnabled ? (
              <>
                <button
                  type="button"
                  onClick={() => setShowTranscript((v) => !v)}
                  className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
                  aria-expanded={showTranscript}
                >
                  {showTranscript
                    ? <ChevronDown className="w-4 h-4" aria-hidden="true" />
                    : <ChevronRight className="w-4 h-4" aria-hidden="true" />}
                  {t('meetings.transcriptSection')}
                </button>
                {showTranscript && (
                  <div className="mt-3">
                    <TranscriptView meetingId={meeting.id} />
                  </div>
                )}
              </>
            ) : (
              <TranscriptView meetingId={meeting.id} />
            )}
          </div>
        </>
      )}
    </div>
  );
}
