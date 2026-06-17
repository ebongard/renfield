import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Volume2 } from 'lucide-react';
import { useFeatureFlags } from '../../api/resources/brain';
import type { MediaHandoffMessage } from '../../types/device';

/**
 * Room-handoff affordance (chat-ui roadmap item 8).
 *
 * Renders a quiet, inline meta line in the chat thread when Media Follow moves
 * the user's OWN playback to the room they just entered. The signal arrives
 * out-of-band over the Device WebSocket as a `media_handoff` frame, which
 * `useDeviceConnection` re-dispatches as a `renfield-media-handoff` window
 * CustomEvent (same fan-out pattern as proactive notifications). This component
 * is the only listener, so the chat page need not own a device connection.
 *
 * Design decisions (the modernization doc requires these):
 *  - TRANSIENT, not persisted. A handoff is an ambient system event, not a chat
 *    message — it never enters conversation history and auto-fades after a short
 *    TTL. Persisting it would pollute history with non-conversational noise and
 *    re-show stale "followed" lines on reload.
 *  - UNKNOWN ROOM: if the room name is empty/missing, the room suffix is dropped
 *    and a generic "Playback is following you" label is shown instead.
 *  - UNREACHABLE / STALE: the backend emits this frame ONLY after a SUCCESSFUL
 *    resume in the new room, so a failed/stale follow produces nothing here (we
 *    never claim a handoff that did not actually happen). Any line that does
 *    show expires client-side via the TTL, so a long-lived tab never accrues a
 *    backlog of handoff lines.
 *
 * Visual language: a subtle left-aligned meta line (NOT a chat bubble, NOT
 * centered, NOT an icon-in-a-colored-circle). The icon is paired with text so
 * it is never color-only (WCAG 1.4.1); the row is display-only with an
 * aria-label and `role="status"`/`aria-live="polite"` so it is announced once.
 */

const HANDOFF_TTL_MS = 12_000;
// Cap concurrent lines so a rapid burst (e.g. moving through several rooms)
// can't grow the list unbounded between TTL sweeps.
const MAX_LINES = 3;

interface HandoffLine {
  id: number;
  kind: MediaHandoffMessage['kind'];
  room: string | null;
}

let _seq = 0;

interface MediaHandoffIndicatorProps {
  /** Per-line auto-fade TTL in ms. Defaults to HANDOFF_TTL_MS; overridable so
   *  tests can keep the transient-fade assertion fast. */
  ttlMs?: number;
}

export default function MediaHandoffIndicator({ ttlMs = HANDOFF_TTL_MS }: MediaHandoffIndicatorProps = {}) {
  const { t } = useTranslation();
  const { data: features } = useFeatureFlags();
  const enabled = features?.room_handoff_enabled ?? false;

  const [lines, setLines] = useState<HandoffLine[]>([]);
  // Track per-line expiry timers so we can clear them on unmount.
  const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    if (!enabled) return;

    const onHandoff = (event: Event) => {
      const detail = (event as CustomEvent<MediaHandoffMessage>).detail;
      if (!detail || detail.type !== 'media_handoff') return;

      const id = ++_seq;
      const room = typeof detail.room === 'string' && detail.room.trim() ? detail.room.trim() : null;
      const kind: MediaHandoffMessage['kind'] = detail.kind === 'continued' ? 'continued' : 'media_followed';

      setLines((prev) => [...prev, { id, kind, room }].slice(-MAX_LINES));

      const timer = setTimeout(() => {
        setLines((prev) => prev.filter((l) => l.id !== id));
        timersRef.current.delete(id);
      }, ttlMs);
      timersRef.current.set(id, timer);
    };

    window.addEventListener('renfield-media-handoff', onHandoff as EventListener);
    return () => {
      window.removeEventListener('renfield-media-handoff', onHandoff as EventListener);
    };
  }, [enabled, ttlMs]);

  // Clear any outstanding timers on unmount.
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach((timer) => clearTimeout(timer));
      timers.clear();
    };
  }, []);

  if (!enabled || lines.length === 0) return null;

  const labelFor = (line: HandoffLine): string => {
    if (line.kind === 'continued') {
      return line.room
        ? t('chat.mediaHandoff.continued', { room: line.room })
        : t('chat.mediaHandoff.continuedUnknownRoom');
    }
    return line.room
      ? t('chat.mediaHandoff.followed', { room: line.room })
      : t('chat.mediaHandoff.followedUnknownRoom');
  };

  return (
    <div className="space-y-1" role="status" aria-live="polite">
      {lines.map((line) => {
        const label = labelFor(line);
        return (
          <div
            key={line.id}
            className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400"
            aria-label={t('chat.mediaHandoff.ariaLabel')}
          >
            <Volume2 className="w-3.5 h-3.5 flex-shrink-0 opacity-70" aria-hidden="true" />
            <span>{label}</span>
          </div>
        );
      })}
    </div>
  );
}
