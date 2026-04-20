import React, { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Users, Plus, Trash2, UserCircle } from 'lucide-react';
import apiClient from '../utils/axios';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Modal from '../components/Modal';
import TierBadge from '../components/TierBadge';
import TierPicker from '../components/TierPicker';
import { useConfirmDialog } from '../components/ConfirmDialog';

export default function CirclesSettingsPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [settings, setSettings] = useState(null);
  const [members, setMembers] = useState([]);
  const [userOptions, setUserOptions] = useState([]);
  const [userOptionsBlocked, setUserOptionsBlocked] = useState(false);
  const [loading, setLoading] = useState(true);
  const [savingTier, setSavingTier] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Add-member modal
  const [showAddModal, setShowAddModal] = useState(false);
  const [addUserId, setAddUserId] = useState('');
  const [addTier, setAddTier] = useState(2);
  const [adding, setAdding] = useState(false);
  // Per-member saving guard — prevents out-of-order PATCHes on rapid clicks.
  const [savingMemberIds, setSavingMemberIds] = useState(() => new Set());

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const [settingsResp, membersResp] = await Promise.all([
        apiClient.get('/api/circles/me/settings'),
        apiClient.get('/api/circles/me/members'),
      ]);
      setSettings(settingsResp.data);
      setMembers(membersResp.data || []);
      setError(null);
    } catch (err) {
      setError(t('circles.couldNotLoad'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  // Also pull the user list so the add dialog has a picker. Non-admins hit
  // 403 (USERS_VIEW required); fall back to free-form user_id and surface a
  // hint in the modal so the user understands why there's no dropdown.
  const loadUsers = useCallback(async () => {
    try {
      const resp = await apiClient.get('/api/users');
      setUserOptions(resp.data?.users || resp.data || []);
      setUserOptionsBlocked(false);
    } catch (err) {
      setUserOptions([]);
      setUserOptionsBlocked(err?.response?.status === 403);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadUsers(); }, [loadUsers]);

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [success]);

  const currentDefaultTier = Number(settings?.default_capture_policy?.tier ?? 0);

  const handleDefaultTierChange = async (newTier) => {
    if (savingTier || newTier === currentDefaultTier) return;
    setSavingTier(true);
    try {
      const resp = await apiClient.patch('/api/circles/me/settings', {
        default_capture_policy: { ...(settings?.default_capture_policy || {}), tier: newTier },
      });
      setSettings(resp.data);
      setSuccess(t('common.success'));
    } catch (err) {
      setError(t('circles.couldNotSave'));
    } finally {
      setSavingTier(false);
    }
  };

  const openAddModal = () => {
    setAddUserId('');
    setAddTier(2);
    setShowAddModal(true);
  };

  const handleAdd = async (e) => {
    e.preventDefault();
    const userIdInt = parseInt(addUserId, 10);
    if (!Number.isFinite(userIdInt)) return;
    setAdding(true);
    try {
      await apiClient.post('/api/circles/me/members', {
        member_user_id: userIdInt,
        dimension: 'tier',
        value: addTier,
      });
      setShowAddModal(false);
      setSuccess(t('common.success'));
      await load();
    } catch (err) {
      setError(err?.response?.data?.detail || t('circles.couldNotSave'));
    } finally {
      setAdding(false);
    }
  };

  const handleMemberTierChange = async (member, newTier) => {
    if (savingMemberIds.has(member.member_user_id)) return;
    setSavingMemberIds((prev) => new Set(prev).add(member.member_user_id));
    try {
      await apiClient.patch(`/api/circles/me/members/${member.member_user_id}`, {
        dimension: 'tier',
        value: newTier,
      });
      setMembers((prev) => prev.map((m) =>
        m.member_user_id === member.member_user_id
          ? { ...m, dimensions: { ...(m.dimensions || {}), tier: newTier } }
          : m,
      ));
      setSuccess(t('common.success'));
    } catch {
      setError(t('circles.couldNotSave'));
    } finally {
      setSavingMemberIds((prev) => {
        const next = new Set(prev);
        next.delete(member.member_user_id);
        return next;
      });
    }
  };

  const handleRemove = async (member) => {
    const name = member.member_username || `#${member.member_user_id}`;
    const ok = await confirm({
      title: t('circles.removeMember'),
      message: t('circles.removeMemberConfirm', { name }),
      confirmText: t('common.delete'),
      variant: 'danger',
    });
    if (!ok) return;
    try {
      await apiClient.delete(`/api/circles/me/members/${member.member_user_id}`);
      setMembers((prev) => prev.filter((m) => m.member_user_id !== member.member_user_id));
      setSuccess(t('common.success'));
    } catch {
      setError(t('circles.couldNotSave'));
    }
  };

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      <PageHeader
        icon={Users}
        title={t('circles.settingsTitle')}
        subtitle={t('circles.settingsSubtitle')}
      />

      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

      {loading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </div>
      ) : (
        <>
          {/* Default capture tier */}
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
              disabled={savingTier}
            />
          </section>

          {/* Members */}
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
        </>
      )}

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
            <button type="submit" disabled={adding || !addUserId} className="btn-primary px-4 py-2 rounded-lg disabled:opacity-50">
              {t('common.save')}
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialogComponent />
    </div>
  );
}
