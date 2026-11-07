import { useTranslation } from 'react-i18next';

/**
 * Placeholder for the message list while a conversation's history loads (#125).
 *
 * Mimics the thread's own layout — alternating user (right) and assistant
 * (left) bubbles at the same max width and radius — so the swap to the real
 * messages does not jump. Neutral grays on both sides, like every other
 * skeleton in the app: the crimson user bubble — and its pale tints, which
 * DESIGN.md reserves for error surfaces — must not appear on a placeholder.
 *
 * The bubbles are decorative (`aria-hidden`); the one announcement is the
 * status line. `animate-pulse` is switched off under `prefers-reduced-motion`
 * by `index.css` and by `motion-reduce:` here.
 */
const BUBBLES: { side: 'user' | 'assistant'; lines: string[] }[] = [
  { side: 'user', lines: ['w-40'] },
  { side: 'assistant', lines: ['w-64', 'w-56', 'w-32'] },
  { side: 'user', lines: ['w-52', 'w-24'] },
  { side: 'assistant', lines: ['w-60', 'w-44'] },
];

export default function ChatSkeleton() {
  const { t } = useTranslation();
  return (
    <div role="status" data-testid="chat-skeleton" className="space-y-4">
      <span className="sr-only">{t('chat.loadingConversation')}</span>
      {BUBBLES.map((bubble, i) => (
        <div
          key={i}
          aria-hidden="true"
          data-side={bubble.side}
          className={`flex ${bubble.side === 'user' ? 'justify-end' : 'justify-start'}`}
        >
          <div className="max-w-[70%] px-4 py-3 rounded-lg space-y-2 bg-gray-100 dark:bg-gray-700 animate-pulse motion-reduce:animate-none">
            {bubble.lines.map((width, j) => (
              <div key={j} className={`h-3 max-w-full rounded bg-gray-200 dark:bg-gray-600 ${width}`} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
