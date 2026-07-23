import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader, Users, Check } from 'lucide-react';

import { useMeetingSegments, useRelabelSpeaker, type MeetingSegment } from '../../api/resources/meetings';

export function formatClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const mm = Math.floor(s / 60).toString().padStart(2, '0');
  const ss = (s % 60).toString().padStart(2, '0');
  return `${mm}:${ss}`;
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
  // §2 Track A merge-on-enroll: how many OTHER meetings the last relabel renamed.
  const [crossApplied, setCrossApplied] = useState(0);

  const submit = async (speakerKey: string) => {
    const label = (drafts[speakerKey] ?? '').trim();
    if (!label || relabel.isPending) return;
    try {
      const res = await relabel.mutateAsync({ meetingId, speakerKey, label });
      setDrafts((d) => ({ ...d, [speakerKey]: '' }));
      setSavedKey(speakerKey);
      setCrossApplied(res.cross_meeting_applied ?? 0);
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
              if (savedKey === sp.key) { setSavedKey(null); setCrossApplied(0); }
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
      {savedKey && crossApplied > 0 && !relabel.isPending && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {t('meetings.crossMeetingApplied', { count: crossApplied })}
        </p>
      )}
    </div>
  );
}

/** Speaker-attributed transcript with per-cluster relabel fields. The raw
 *  material behind the minutes (Track D) — secondary to the deliverable. */
export default function TranscriptView({ meetingId }: { meetingId: number }) {
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
