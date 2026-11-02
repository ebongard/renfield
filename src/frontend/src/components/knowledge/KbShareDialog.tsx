/**
 * Share dialog of a knowledge base: who has which level, add a share, change
 * a level, revoke a share — and the owner's separate delete switch.
 *
 * The backend decides every action; the dialog mirrors its rules so nothing
 * is offered that would be refused:
 *  - the delete switch is the KB owner's (and kb.all's) alone — `kb.can_delete_kb`
 *    carries exactly that rule. An admin grantee sees the switch read-only;
 *  - a share that carries the switch is the owner's to change or revoke, so an
 *    admin grantee cannot downgrade or revoke it (the controls are disabled).
 */
import { useState } from 'react';
import type { FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Trash2, UserPlus } from 'lucide-react';

import Modal from '../Modal';
import Alert from '../Alert';
import { extractApiError } from '../../utils/axios';
import { useUsersQuery } from '../../api/resources/users';
import {
  useKbSharesQuery,
  useShareKnowledgeBase,
  useRevokeKbShare,
  type KbShare,
  type KbShareLevel,
  type KnowledgeBaseRow,
} from '../../api/resources/knowledge';

const LEVELS: KbShareLevel[] = ['read', 'write', 'admin'];

function isLevel(value: string): value is KbShareLevel {
  return (LEVELS as string[]).includes(value);
}

interface KbShareDialogProps {
  kb: KnowledgeBaseRow;
  isOpen: boolean;
  onClose: () => void;
}

export default function KbShareDialog({ kb, isOpen, onClose }: KbShareDialogProps) {
  const { t } = useTranslation();
  const mayManageSwitch = kb.can_delete_kb === true;

  const sharesQuery = useKbSharesQuery(kb.id, isOpen);
  const usersQuery = useUsersQuery();
  const shareMutation = useShareKnowledgeBase();
  const revokeMutation = useRevokeKbShare();

  const [error, setError] = useState<string | null>(null);
  const [addUserId, setAddUserId] = useState('');
  const [addLevel, setAddLevel] = useState<KbShareLevel>('read');
  const [addCanDelete, setAddCanDelete] = useState(false);

  const shares = sharesQuery.data ?? [];
  const sharedIds = new Set(shares.map((s) => s.user_id));
  // The user list needs USERS_VIEW; without it the picker falls back to an id field.
  const userOptions = (usersQuery.data ?? []).filter(
    (u) => u.id !== kb.owner_id && !sharedIds.has(u.id),
  );
  const pickerBlocked = usersQuery.isError;

  const run = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(extractApiError(err, t('common.error')));
    }
  };

  const changeLevel = (share: KbShare, level: KbShareLevel) =>
    run(() => shareMutation.mutateAsync({ kbId: kb.id, userId: share.user_id, permission: level }));

  const toggleSwitch = (share: KbShare) =>
    run(() => shareMutation.mutateAsync({
      kbId: kb.id, userId: share.user_id, permission: share.permission, canDelete: !share.can_delete,
    }));

  const revoke = (share: KbShare) =>
    run(() => revokeMutation.mutateAsync({ kbId: kb.id, userId: share.user_id }));

  const handleAdd = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const userId = parseInt(addUserId, 10);
    if (!Number.isFinite(userId)) return;
    await run(async () => {
      await shareMutation.mutateAsync({
        kbId: kb.id,
        userId,
        permission: addLevel,
        ...(mayManageSwitch ? { canDelete: addCanDelete } : {}),
      });
      setAddUserId('');
      setAddLevel('read');
      setAddCanDelete(false);
    });
  };

  const busy = shareMutation.isPending || revokeMutation.isPending;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={t('knowledge.shares.title', { name: kb.name })}
      maxWidth="max-w-lg"
    >
      <div className="space-y-4">
        {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}
        {sharesQuery.errorMessage && <Alert variant="error">{sharesQuery.errorMessage}</Alert>}

        {shares.length === 0 && !sharesQuery.isLoading ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">{t('knowledge.shares.empty')}</p>
        ) : (
          <ul className="divide-y divide-gray-200 dark:divide-gray-700" aria-label={t('knowledge.shares.list')}>
            {shares.map((share) => {
              // A switched share is the owner's to change or revoke.
              const locked = share.can_delete && !mayManageSwitch;
              return (
                <li key={share.user_id} className="py-2 flex flex-wrap items-center gap-2">
                  <span className="grow text-sm font-medium text-gray-900 dark:text-white">
                    {share.username || `#${share.user_id}`}
                  </span>
                  <select
                    aria-label={t('knowledge.shares.levelFor', { name: share.username })}
                    value={share.permission}
                    disabled={busy || locked}
                    onChange={(e) => {
                      if (isLevel(e.target.value)) changeLevel(share, e.target.value);
                    }}
                    className="input w-auto py-1 text-sm"
                  >
                    {LEVELS.map((level) => (
                      <option key={level} value={level}>{t(`knowledge.shares.level.${level}`)}</option>
                    ))}
                  </select>
                  {mayManageSwitch ? (
                    <label className="flex items-center gap-1 text-sm text-gray-700 dark:text-gray-300">
                      <input
                        type="checkbox"
                        checked={share.can_delete}
                        disabled={busy}
                        onChange={() => toggleSwitch(share)}
                      />
                      {t('knowledge.shares.mayDelete')}
                    </label>
                  ) : (
                    share.can_delete && (
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        {t('knowledge.shares.mayDelete')}
                      </span>
                    )
                  )}
                  <button
                    type="button"
                    onClick={() => revoke(share)}
                    disabled={busy || locked}
                    aria-label={t('knowledge.shares.revokeFor', { name: share.username })}
                    title={locked ? t('knowledge.shares.lockedHint') : t('knowledge.shares.revoke')}
                    className="p-2 rounded-lg text-gray-500 hover:text-red-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-red-400 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    <Trash2 className="w-4 h-4" aria-hidden="true" />
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        <form onSubmit={handleAdd} className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
          <h3 className="text-sm font-semibold text-gray-900 dark:text-white">{t('knowledge.shares.add')}</h3>
          <div>
            <label htmlFor="kb-share-user" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('knowledge.shares.user')}
            </label>
            {pickerBlocked ? (
              <>
                <input
                  id="kb-share-user"
                  type="number"
                  min="1"
                  value={addUserId}
                  onChange={(e) => setAddUserId(e.target.value)}
                  required
                  className="input"
                  placeholder="user_id"
                />
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{t('knowledge.shares.userIdHint')}</p>
              </>
            ) : (
              <select
                id="kb-share-user"
                value={addUserId}
                onChange={(e) => setAddUserId(e.target.value)}
                required
                className="input"
              >
                <option value="">—</option>
                {userOptions.map((u) => (
                  <option key={u.id} value={u.id}>{u.username}</option>
                ))}
              </select>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label htmlFor="kb-share-level" className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {t('knowledge.shares.levelLabel')}
            </label>
            <select
              id="kb-share-level"
              value={addLevel}
              onChange={(e) => {
                if (isLevel(e.target.value)) setAddLevel(e.target.value);
              }}
              className="input w-auto py-1 text-sm"
            >
              {LEVELS.map((level) => (
                <option key={level} value={level}>{t(`knowledge.shares.level.${level}`)}</option>
              ))}
            </select>
            {mayManageSwitch && (
              <label className="flex items-center gap-1 text-sm text-gray-700 dark:text-gray-300">
                <input
                  type="checkbox"
                  checked={addCanDelete}
                  onChange={(e) => setAddCanDelete(e.target.checked)}
                />
                {t('knowledge.shares.mayDelete')}
              </label>
            )}
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400">{t('knowledge.shares.switchHint')}</p>
          <div className="flex justify-end">
            <button type="submit" disabled={busy || !addUserId} className="btn-primary flex items-center gap-2">
              <UserPlus className="w-4 h-4" aria-hidden="true" />
              {t('knowledge.shares.addButton')}
            </button>
          </div>
        </form>
      </div>
    </Modal>
  );
}
