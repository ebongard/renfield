import { useState, useEffect } from 'react';
import type { FormEvent } from 'react';
import type { AxiosError } from 'axios';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { Users, Plus, Trash2, UserCircle, Share2, Inbox } from 'lucide-react';
import apiClient from '../utils/axios';
import { extractApiError } from '../utils/axios';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Modal from '../components/Modal';
import TierBadge from '../components/TierBadge';
import type { CircleTier } from '../components/TierBadge';
import TierPicker from '../components/TierPicker';
import PairInitiatorModal from '../components/PairInitiatorModal';
import PairResponderModal from '../components/PairResponderModal';
import { useConfirmDialog } from '../components/ConfirmDialog';
import {
  useCircleSettingsQuery,
  useCircleMembersQuery,
  usePatchCircleSettings,
  useAddCircleMember,
  useUpdateCircleMember,
  useDeleteCircleMember,
  type CircleMember,
} from '../api/resources/circles';

interface UserOption {
  id: number;
  username: string;
}

export default function CirclesSettingsPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const settingsQuery = useCircleSettingsQuery();
  const membersQuery = useCircleMembersQuery();
  const settings = settingsQuery.data;
  const members = membersQuery.data ?? [];
  const loading = settingsQuery.isLoading || membersQuery.isLoading;

  const patchSettings = usePatchCircleSettings();
  const addMember = useAddCircleMember();
  const updateMember = useUpdateCircleMember();
  const deleteMember = useDeleteCircleMember();

  const [userOptions, setUserOptions] = useState<UserOption[]>([]);
  const [userOptionsBlocked, setUserOptionsBlocked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [showAddModal, setShowAddModal] = useState(false);
  const [showPairInitModal, setShowPairInitModal] = useState(false);
  const [showPairRespModal, setShowPairRespModal] = useState(false);
  const [addUserId, setAddUserId] = useState('');
  const [addTier, setAddTier] = useState<CircleTier>(2);
  const [savingMemberIds, setSavingMemberIds] = useState<Set<number>>(() => new Set());

  // User list for add-member dropdown. Non-admins hit 403 (USERS_VIEW required);
  // fall back to free-form user_id and surface a hint in the modal.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await apiClient.get<{ users?: UserOption[] } | UserOption[]>('/api/users');
        if (cancelled) return;
        const list = Array.isArray(resp.data) ? resp.data : (resp.data.users ?? []);
        setUserOptions(list);
        setUserOptionsBlocked(false);
      } catch (err) {
        if (cancelled) return;
        setUserOptions([]);
        const status = (err as AxiosError | undefined)?.response?.status;
        setUserOptionsBlocked(status === 403);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [success]);

  const displayError = error ?? settingsQuery.errorMessage ?? membersQuery.errorMessage;

  const currentDefaultTier = Number(settings?.default_capture_policy?.tier ?? 0);

  const handleDefaultTierChange = async (newTier: CircleTier) => {
    if (patchSettings.isPending || newTier === currentDefaultTier) return;
    try {
      await patchSettings.mutateAsync({
        default_capture_policy: { ...(settings?.default_capture_policy || {}), tier: newTier },
      });
      setSuccess(t('common.success'));
    } catch (err) {
      setError(extractApiError(err, t('circles.couldNotSave')));
    }
  };

  const openAddModal = () => {
    setAddUserId('');
    setAddTier(2);
    setShowAddModal(true);
  };

  const handleAdd = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const userIdInt = parseInt(addUserId, 10);
    if (!Number.isFinite(userIdInt)) return;
    try {
      await addMember.mutateAsync({
        member_user_id: userIdInt,
        dimension: 'tier',
        value: addTier,
      });
      setShowAddModal(false);
      setSuccess(t('common.success'));
    } catch (err) {
      setError(extractApiError(err, t('circles.couldNotSave')));
    }
  };

  const handleMemberTierChange = async (member: CircleMember, newTier: CircleTier) => {
    if (savingMemberIds.has(member.member_user_id)) return;
    setSavingMemberIds((prev) => new Set(prev).add(member.member_user_id));
    try {
      await updateMember.mutateAsync({
        memberUserId: member.member_user_id,
        dimension: 'tier',
        value: newTier,
      });
      setSuccess(t('common.success'));
    } catch (err) {
      setError(extractApiError(err, t('circles.couldNotSave')));
    } finally {
      setSavingMemberIds((prev) => {
        const next = new Set(prev);
        next.delete(member.member_user_id);
        return next;
      });
    }
  };

  const handleRemove = async (member: CircleMember) => {
    const name = member.member_username || `#${member.member_user_id}`;
    const ok = await confirm({
      title: t('circles.removeMember'),
      message: t('circles.removeMemberConfirm', { name }),
      confirmLabel: t('common.delete'),
      variant: 'danger',
    });
    if (!ok) return;
    try {
      await deleteMember.mutateAsync(member.member_user_id);
      setSuccess(t('common.success'));
    } catch (err) {
      setError(extractApiError(err, t('circles.couldNotSave')));
    }
  };

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      <PageHeader
        icon={Users}
        title={t('circles.settingsTitle')}
        subtitle={t('circles.settingsSubtitle')}
      />

      {displayError && <Alert variant="error" onClose={() => setError(null)}>{displayError}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

      {loading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </div>
      ) : (
        <>
          <section className="card space-y-3">
            <div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                {t('circles.defaultCaptureTier')}
              </h2>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                {t('circles.defaultCaptureHint')}
              </p>
            </div>
            <TierPicker
              value={currentDefaultTier}
              onChange={handleDefaultTierChange}
              disabled={patchSettings.isPending}
            />
          </section>

          <section className="card space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                {t('circles.members')}
              </h2>
              <button
                type="button"
                onClick={openAddModal}
                className="btn-primary inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm"
              >
                <Plus className="w-4 h-4" />
                {t('circles.addMember')}
              </button>
            </div>

            {members.length === 0 ? (
              <div className="text-center py-8">
                <UserCircle className="w-10 h-10 mx-auto mb-2 text-gray-300 dark:text-gray-600" aria-hidden="true" />
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  {t('circles.noMembers')}
                </p>
              </div>
            ) : (
              <ul className="space-y-3">
                {members.map((member) => {
                  const tier = Number(member.dimensions?.tier ?? 4);
                  return (
                    <li
                      key={member.member_user_id}
                      className={`atom-row tier-ring-${tier} flex-col sm:flex-row sm:items-center`}
                    >
                      <div className="flex-1 min-w-0 flex items-center gap-3">
                        <UserCircle className="w-8 h-8 text-gray-400 flex-shrink-0" aria-hidden="true" />
                        <div className="min-w-0">
                          <p className="font-medium text-gray-900 dark:text-white truncate">
                            {member.member_username || `#${member.member_user_id}`}
                          </p>
                          <TierBadge tier={tier} className="mt-1" />
                        </div>
                      </div>
                      <div className="sm:ml-4 sm:flex-shrink-0 flex items-center gap-2 mt-3 sm:mt-0">
                        <TierPicker
                          value={tier}
                          onChange={(nt) => handleMemberTierChange(member, nt)}
                          disabled={savingMemberIds.has(member.member_user_id)}
                        />
                        <button
                          type="button"
                          onClick={() => handleRemove(member)}
                          className="btn-icon btn-icon-danger"
                          title={t('circles.removeMember')}
                          aria-label={t('circles.removeMember')}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section className="card space-y-4">
            <div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                {t('circles.pairingTitle')}
              </h2>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                {t('circles.pairingSubtitle')}{' '}
                <Link to="/settings/circles/peers" className="text-primary-600 hover:underline">
                  {t('circles.pairingManageLinkText')}
                </Link>
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => setShowPairInitModal(true)}
                className="btn-primary inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm"
              >
                <Share2 className="w-4 h-4" />
                {t('circles.pairInitiate')}
              </button>
              <button
                type="button"
                onClick={() => setShowPairRespModal(true)}
                className="btn-secondary inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm"
              >
                <Inbox className="w-4 h-4" />
                {t('circles.pairAccept')}
              </button>
            </div>
          </section>
        </>
      )}

      <PairInitiatorModal
        isOpen={showPairInitModal}
        onClose={() => setShowPairInitModal(false)}
        onPaired={() => setSuccess(t('circles.pairSuccess'))}
      />
      <PairResponderModal
        isOpen={showPairRespModal}
        onClose={() => setShowPairRespModal(false)}
        onPaired={() => setSuccess(t('circles.pairSuccess'))}
      />

      <Modal isOpen={showAddModal} onClose={() => setShowAddModal(false)} title={t('circles.addMember')}>
        <form onSubmit={handleAdd} className="space-y-4">
          <div>
            <label htmlFor="add-user" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('circles.addMemberUser')}
            </label>
            {userOptions.length > 0 ? (
              <select
                id="add-user"
                value={addUserId}
                onChange={(e) => setAddUserId(e.target.value)}
                required
                className="input"
              >
                <option value="">—</option>
                {userOptions.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.username} (#{u.id})
                  </option>
                ))}
              </select>
            ) : (
              <>
                <input
                  id="add-user"
                  type="number"
                  min="1"
                  value={addUserId}
                  onChange={(e) => setAddUserId(e.target.value)}
                  required
                  className="input"
                  placeholder="user_id"
                />
                {userOptionsBlocked && (
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    {t('circles.addMemberUserIdHint')}
                  </p>
                )}
              </>
            )}
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('circles.addMemberTier')}
            </label>
            <TierPicker value={addTier} onChange={setAddTier} />
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setShowAddModal(false)} className="btn-secondary px-4 py-2 rounded-lg">
              {t('common.cancel')}
            </button>
            <button type="submit" disabled={addMember.isPending || !addUserId} className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50">
              {t('common.save')}
            </button>
          </div>
        </form>
      </Modal>

      {ConfirmDialogComponent}
    </div>
  );
}
