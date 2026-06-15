import { useTranslation } from 'react-i18next';
import { CornerDownLeft } from 'lucide-react';
import { useChatContext } from '../../pages/ChatPage/context/ChatContext';

/**
 * Follow-up suggestion chips under the last assistant turn (chat-ui roadmap
 * item 2). Tapping a chip fills the composer with that text — the user reviews
 * and sends (no auto-send: safer for a shared household, no accidental sends).
 *
 * Ephemeral by design — only the live last assistant turn carries these, so they
 * don't reappear on history reload. Empty/undefined → renders nothing.
 */
export default function FollowupChips({ followups }: { followups?: string[] }) {
  const { t } = useTranslation();
  const { setInput } = useChatContext();

  if (!followups || followups.length === 0) return null;

  return (
    <div className="mt-2 flex flex-wrap gap-1.5" aria-label={t('chat.followups.label')}>
      {followups.map((text, i) => (
        <button
          key={`${i}-${text}`}
          type="button"
          onClick={() => setInput?.(text)}
          title={text}
          className="inline-flex items-center gap-1 min-h-[44px] sm:min-h-0 px-3 py-1.5 rounded-full text-sm bg-primary-50 text-primary-700 hover:bg-primary-100 dark:bg-primary-900/30 dark:text-primary-200 dark:hover:bg-primary-900/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
        >
          <CornerDownLeft className="w-3.5 h-3.5 flex-shrink-0 opacity-70" aria-hidden="true" />
          <span className="truncate max-w-[260px]">{text}</span>
        </button>
      ))}
    </div>
  );
}
