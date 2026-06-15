import { useTranslation } from 'react-i18next';
import { Bot, Pin } from 'lucide-react';
import { useChatContext } from '../../pages/ChatPage/context/ChatContext';

/**
 * Agent-role badge (chat-UI roadmap item 6). Shows WHICH agent role produced an
 * answer and lets the user pin that role for the next turn — making the router's
 * choice visible and correctable. The pin reuses the existing `role_hint` path
 * (`setRoleHint` → `pendingRoleHint`); the ChatInput pinned-role indicator then
 * shows it's active. Gated by `role_surfacing_enabled` at the call site.
 *
 * a11y: the role name is always rendered as text (not color-only); the button's
 * aria-label/title explain the pin action.
 */

// Known agent roles (config/agent_roles.yaml). Unknown roles fall back to the raw
// id so a newly-added role still renders (just unlocalized) instead of breaking.
const KNOWN_ROLES = new Set([
  'smart_home', 'media', 'documents', 'research', 'presence',
  'workflow', 'general', 'conversation', 'knowledge', 'routine',
]);

/** Localized display name for an agent role id (raw id if unknown). */
export function roleLabel(t: (key: string) => string, role: string): string {
  return KNOWN_ROLES.has(role) ? t(`chat.roles.${role}`) : role;
}

export default function AgentRoleBadge({ role }: { role?: string }) {
  const { t } = useTranslation();
  const { pendingRoleHint, setRoleHint } = useChatContext();

  if (!role) return null;

  const label = roleLabel(t, role);
  const pinned = pendingRoleHint === role;

  return (
    <button
      type="button"
      onClick={() => setRoleHint(role)}
      aria-label={t(pinned ? 'chat.roleBadge.pinned' : 'chat.roleBadge.pin', { role: label })}
      title={t(pinned ? 'chat.roleBadge.pinned' : 'chat.roleBadge.pin', { role: label })}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 ${
        pinned
          ? 'bg-primary-100 text-primary-800 dark:bg-primary-900/40 dark:text-primary-100'
          : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600'
      }`}
    >
      <Bot className="w-3 h-3 flex-shrink-0 opacity-70" aria-hidden="true" />
      <span>{label}</span>
      {pinned && <Pin className="w-3 h-3 flex-shrink-0" aria-hidden="true" />}
    </button>
  );
}
