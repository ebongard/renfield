import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import {
  FolderKanban, FileText, Mic, Gavel, MessageSquare, NotebookPen, Loader, XCircle, ArrowLeft,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import PageHeader from '../components/PageHeader';
import TierBadge from '../components/TierBadge';
import { formatDateTime } from '../utils/datetime';
import {
  useProjectQuery, useProjectTimeline, type TimelineEvent,
} from '../api/resources/projects';

/** Per-kind icon + accent for a timeline event. */
const KIND_META: Record<TimelineEvent['kind'], { Icon: LucideIcon; cls: string }> = {
  document: { Icon: FileText, cls: 'text-blue-600 dark:text-blue-400' },
  meeting: { Icon: Mic, cls: 'text-primary-600 dark:text-primary-400' },
  decision: { Icon: Gavel, cls: 'text-amber-600 dark:text-amber-400' },
  chat: { Icon: MessageSquare, cls: 'text-green-600 dark:text-green-400' },
  note: { Icon: NotebookPen, cls: 'text-pink-600 dark:text-pink-400' },
};

/** A single timeline row; deep-links to the underlying artifact where one exists. */
function TimelineRow({ event }: { event: TimelineEvent }) {
  const { t } = useTranslation();
  const { Icon, cls } = KIND_META[event.kind];

  // meeting_id wins over document_id: a meeting/decision event carries the
  // transcript's document_id too, but the deliverable-first MEETING DETAIL page
  // (minutes/decisions/action-items) is the destination — not the raw transcript
  // doc in the KB (that "buried" view is exactly what the Meetings-UX track fixes).
  const href =
    event.meeting_id != null
      ? `/meetings/${event.meeting_id}`
      : event.document_id != null
        ? `/knowledge?doc=${event.document_id}`
        : null;

  const body = (
    <div className="flex items-start gap-3">
      <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${cls}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-sm text-gray-900 dark:text-white break-words">{event.title}</p>
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mt-0.5">
          <span className="uppercase tracking-wide">{t(`projects.timeline.kind.${event.kind}`)}</span>
          {event.subtitle && <span>· {event.subtitle}</span>}
          {event.ts && <span>· {formatDateTime(event.ts)}</span>}
        </div>
      </div>
    </div>
  );

  return href ? (
    <Link to={href} className="block card hover:border-primary-300 dark:hover:border-primary-700 transition-colors">
      {body}
    </Link>
  ) : (
    <div className="card">{body}</div>
  );
}

export default function ProjectDetailPage() {
  const { t } = useTranslation();
  const params = useParams();
  const projectId = params.id ? Number(params.id) : null;

  const projectQuery = useProjectQuery(projectId);
  const timelineQuery = useProjectTimeline(projectId);
  const project = projectQuery.data;
  const events = timelineQuery.data ?? [];

  return (
    <div className="space-y-6">
      <Link
        to="/projects"
        className="inline-flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400 hover:text-primary-600 dark:hover:text-primary-400"
      >
        <ArrowLeft className="w-4 h-4" />
        {t('projects.backToList')}
      </Link>

      {projectQuery.isLoading ? (
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400" />
        </div>
      ) : projectQuery.errorMessage || !project ? (
        <div className="card text-center py-12">
          <XCircle className="w-12 h-12 mx-auto text-red-500 mb-3" />
          <p className="font-medium text-gray-700 dark:text-gray-300">
            {projectQuery.errorMessage || t('projects.notFound')}
          </p>
        </div>
      ) : (
        <>
          <PageHeader
            icon={FolderKanban}
            title={project.name}
            subtitle={project.description || t('projects.timeline.subtitle')}
          >
            <TierBadge tier={project.circle_tier} />
          </PageHeader>

          <div>
            <h2 className="text-base font-semibold text-gray-900 dark:text-white mb-3">
              {t('projects.timeline.title')}
            </h2>
            {timelineQuery.isLoading ? (
              <div className="card text-center py-10">
                <Loader className="w-6 h-6 animate-spin mx-auto text-gray-400" />
              </div>
            ) : timelineQuery.errorMessage ? (
              <div className="card text-center py-10">
                <p className="text-sm text-red-600 dark:text-red-400">{timelineQuery.errorMessage}</p>
              </div>
            ) : events.length === 0 ? (
              <div className="card text-center py-10">
                <p className="font-medium text-gray-700 dark:text-gray-300">{t('projects.timeline.empty')}</p>
                <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('projects.timeline.emptyDesc')}</p>
              </div>
            ) : (
              <div className="space-y-2">
                {events.map((e) => <TimelineRow key={e.id} event={e} />)}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
