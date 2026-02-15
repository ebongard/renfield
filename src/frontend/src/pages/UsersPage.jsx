/**
 * Users Management Page
 *
 * Admin page for managing users: create, edit, delete, assign roles.
 */
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import { useConfirmDialog } from '../components/ConfirmDialog';
import {
  Users, UserPlus, Pencil, Trash2, Loader, AlertCircle, CheckCircle,
  Shield, User, Mic, Link2, Unlink, Eye, EyeOff, RefreshCw
} from 'lucide-react';

export default function UsersPage() {
  const { t, i18n } = useTranslation();
  const { user: currentUser, getAccessToken } = useAuth();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState([]);
  const [speakers, setSpeakers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Modal states
  const [showModal, setShowModal] = useState(false);
  const [editingUser, setEditingUser] = useState(null);
  const [showLinkSpeakerModal, setShowLinkSpeakerModal] = useState(false);
  const [linkingUserId, setLinkingUserId] = useState(null);

  // Form state
  const [formData, setFormData] = useState({
    username: '',
    first_name: '',
    last_name: '',
    email: '',
    password: '',
    role_id: '',
    is_active: true
  });
  const [showPassword, setShowPassword] = useState(false);
  const [formLoading, setFormLoading] = useState(false);

  // Load data
  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const token = getAccessToken();
      const headers = { Authorization: `Bearer ${token}` };

      const [usersRes, rolesRes, speakersRes] = await Promise.all([
        apiClient.get('/api/users', { headers }),
        apiClient.get('/api/roles', { headers }),
        apiClient.get('/api/speakers', { headers }).catch(() => ({ data: [] }))
      ]);

      // API returns { users: [], total, page, page_size } for users
      // and { roles: [] } for roles
      setUsers(usersRes.data.users || usersRes.data || []);
      setRoles(rolesRes.data.roles || rolesRes.data || []);
      setSpeakers(speakersRes.data || []);
    } catch (err) {
      setError(err.response?.data?.detail || t('users.failedToLoad'));
    } finally {
      setLoading(false);
    }
  }, [getAccessToken]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Auto-clear alerts
  useEffect(() => {
    if (error || success) {
      const timer = setTimeout(() => {
        setError(null);
        setSuccess(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [error, success]);

  // Open create modal
  const handleCreate = () => {
    setEditingUser(null);
    setFormData({
      username: '',
      first_name: '',
      last_name: '',
      email: '',
      password: '',
      role_id: roles.find(r => r.name === 'Gast')?.id || roles[0]?.id || '',
      is_active: true
    });
    setShowPassword(false);
    setShowModal(true);
  };

  // Open edit modal
  const handleEdit = (user) => {
    setEditingUser(user);
    setFormData({
      username: user.username,
      first_name: user.first_name || '',
      last_name: user.last_name || '',
      email: user.email || '',
      password: '',
      role_id: user.role_id,
      is_active: user.is_active
    });
    setShowPassword(false);
    setShowModal(true);
  };

  // Submit form
  const handleSubmit = async (e) => {
    e.preventDefault();
    setFormLoading(true);

    try {
      const token = getAccessToken();
      const headers = { Authorization: `Bearer ${token}` };

      if (editingUser) {
        // Update user
        const updateData = {
          username: formData.username,
          first_name: formData.first_name || null,
          last_name: formData.last_name || null,
          email: formData.email || null,
          role_id: parseInt(formData.role_id),
          is_active: formData.is_active
        };

        await apiClient.patch(`/api/users/${editingUser.id}`, updateData, { headers });

        // Update password separately if provided
        if (formData.password) {
          await apiClient.post(`/api/users/${editingUser.id}/reset-password`, {
            new_password: formData.password
          }, { headers });
        }

        setSuccess(t('users.userUpdated'));
      } else {
        // Create user
        await apiClient.post('/api/users', {
          username: formData.username,
          first_name: formData.first_name || null,
          last_name: formData.last_name || null,
          email: formData.email || null,
          password: formData.password,
          role_id: parseInt(formData.role_id),
          is_active: formData.is_active
        }, { headers });
        setSuccess(t('users.userCreated'));
      }

      setShowModal(false);
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || t('users.failedToSave'));
    } finally {
      setFormLoading(false);
    }
  };

  // Delete user
  const handleDelete = async (user) => {
    if (user.id === currentUser?.id) {
      setError(t('users.cannotDeleteOwnAccount'));
      return;
    }

    const confirmed = await confirm({
      title: t('users.deleteUser'),
      message: t('users.deleteUserConfirm', { username: user.username }),
      confirmText: t('common.delete'),
      variant: 'danger'
    });

    if (!confirmed) return;

    try {
      const token = getAccessToken();
      await apiClient.delete(`/api/users/${user.id}`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setSuccess(t('users.userDeleted'));
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || t('users.failedToDelete'));
    }
  };

  // Link speaker to user
  const handleLinkSpeaker = (userId) => {
    setLinkingUserId(userId);
    setShowLinkSpeakerModal(true);
  };

  // Submit speaker link
  const handleLinkSpeakerSubmit = async (speakerId) => {
    try {
      const token = getAccessToken();
      await apiClient.post(`/api/users/${linkingUserId}/link-speaker`, { speaker_id: speakerId }, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setSuccess(t('users.speakerLinked'));
      setShowLinkSpeakerModal(false);
      setLinkingUserId(null);
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || t('users.failedToLink'));
    }
  };

  // Unlink speaker from user
  const handleUnlinkSpeaker = async (userId) => {
    const confirmed = await confirm({
      title: t('users.unlinkSpeaker'),
      message: t('users.unlinkSpeakerConfirm'),
      confirmText: t('users.unlink'),
      variant: 'warning'
    });

    if (!confirmed) return;

    try {
      const token = getAccessToken();
      await apiClient.delete(`/api/users/${userId}/unlink-speaker`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setSuccess(t('users.speakerUnlinked'));
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || t('users.failedToUnlink'));
    }
  };

  // Get available speakers (not linked to any user)
  const availableSpeakers = Array.isArray(speakers) && Array.isArray(users)
    ? speakers.filter(s => !users.some(u => u.speaker_id === s.id))
    : [];

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="card">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('users.title')}</h1>
          <p className="text-gray-500 dark:text-gray-400">{t('users.subtitle')}</p>
        </div>
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
          <p className="text-gray-500 dark:text-gray-400">{t('users.loading')}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('users.title')}</h1>
        <p className="text-gray-500 dark:text-gray-400">{t('users.subtitle')}</p>
      </div>

      {/* Alerts */}
      {error && (
        <div className="card bg-red-100 dark:bg-red-900/20 border-red-300 dark:border-red-700">
          <div className="flex items-center space-x-3">
            <AlertCircle className="w-5 h-5 text-red-500" />
            <p className="text-red-700 dark:text-red-400">{error}</p>
          </div>
        </div>
      )}

      {success && (
        <div className="card bg-green-100 dark:bg-green-900/20 border-green-300 dark:border-green-700">
          <div className="flex items-center space-x-3">
            <CheckCircle className="w-5 h-5 text-green-500" />
            <p className="text-green-700 dark:text-green-400">{success}</p>
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-wrap gap-3">
        <button onClick={handleCreate} className="btn btn-primary flex items-center space-x-2">
          <UserPlus className="w-4 h-4" />
          <span>{t('users.createUser')}</span>
        </button>
        <button onClick={loadData} className="btn btn-secondary flex items-center space-x-2">
          <RefreshCw className="w-4 h-4" />
          <span>{t('common.refresh')}</span>
        </button>
      </div>

      {/* Users List */}
      <div className="space-y-3">
        {users.length === 0 ? (
          <div className="card text-center py-12">
            <Users className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-4" />
            <p className="text-gray-500 dark:text-gray-400">{t('users.noUsersFound')}</p>
          </div>
        ) : (
          users.map((user) => (
            <div key={user.id} className="card hover:bg-gray-50 dark:hover:bg-gray-750 transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-4">
                  {/* Avatar */}
                  <div className={`w-12 h-12 rounded-full flex items-center justify-center ${
                    user.role_name === 'Admin' ? 'bg-red-100 dark:bg-red-900/50' :
                    user.role_name === 'Familie' ? 'bg-blue-100 dark:bg-blue-900/50' :
                    'bg-gray-200 dark:bg-gray-700'
                  }`}>
                    {user.role_name === 'Admin' ? (
                      <Shield className="w-6 h-6 text-red-600 dark:text-red-400" />
                    ) : (
                      <User className="w-6 h-6 text-gray-500 dark:text-gray-400" />
                    )}
                  </div>

                  {/* Info */}
                  <div>
                    <div className="flex items-center space-x-2">
                      <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                        {user.first_name || user.last_name
                          ? `${user.first_name || ''} ${user.last_name || ''}`.trim()
                          : user.username}
                      </h3>
                      {(user.first_name || user.last_name) && (
                        <span className="text-sm text-gray-500 dark:text-gray-400">({user.username})</span>
                      )}
                      {user.id === currentUser?.id && (
                        <span className="text-xs bg-primary-600 text-white px-2 py-0.5 rounded-sm">{t('users.you')}</span>
                      )}
                      {!user.is_active && (
                        <span className="text-xs bg-red-600 text-white px-2 py-0.5 rounded-sm">{t('users.inactive')}</span>
                      )}
                    </div>
                    <div className="flex items-center space-x-3 text-sm text-gray-500 dark:text-gray-400">
                      <span className={`px-2 py-0.5 rounded ${
                        user.role_name === 'Admin' ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400' :
                        user.role_name === 'Familie' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400' :
                        'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
                      }`}>
                        {user.role_name}
                      </span>
                      {user.email && <span>{user.email}</span>}
                      {user.speaker_id && (
                        <span className="flex items-center space-x-1 text-green-600 dark:text-green-400">
                          <Mic className="w-3 h-3" />
                          <span>{t('users.voiceLinked')}</span>
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {/* Actions */}
                <div className="flex items-center space-x-2">
                  {user.speaker_id ? (
                    <button
                      onClick={() => handleUnlinkSpeaker(user.id)}
                      className="p-2 text-gray-500 hover:text-yellow-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-yellow-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                      title={t('users.unlinkSpeaker')}
                    >
                      <Unlink className="w-5 h-5" />
                    </button>
                  ) : (
                    <button
                      onClick={() => handleLinkSpeaker(user.id)}
                      className="p-2 text-gray-500 hover:text-green-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-green-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                      title={t('users.linkSpeaker')}
                      disabled={availableSpeakers.length === 0}
                    >
                      <Link2 className="w-5 h-5" />
                    </button>
                  )}
                  <button
                    onClick={() => handleEdit(user)}
                    className="p-2 text-gray-500 hover:text-primary-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-primary-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                    title={t('users.editUser')}
                  >
                    <Pencil className="w-5 h-5" />
                  </button>
                  <button
                    onClick={() => handleDelete(user)}
                    className="p-2 text-gray-500 hover:text-red-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-red-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                    title={t('users.deleteUser')}
                    disabled={user.id === currentUser?.id}
                  >
                    <Trash2 className="w-5 h-5" />
                  </button>
                </div>
              </div>

              {/* Additional info */}
              {user.last_login && (
                <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700 text-sm text-gray-400 dark:text-gray-500">
                  {t('users.lastLogin')}: {new Date(user.last_login).toLocaleString(i18n.language)}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      {/* Create/Edit Modal */}
      <Modal
        isOpen={showModal}
        onClose={() => setShowModal(false)}
        title={editingUser ? t('users.editUser') : t('users.createUser')}
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Username */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('auth.username')} <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={formData.username}
              onChange={(e) => setFormData({ ...formData, username: e.target.value })}
              className="input w-full"
              placeholder={t('auth.enterUsername')}
              required
              minLength={3}
              disabled={formLoading}
            />
          </div>

          {/* First Name / Last Name */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('users.firstName')}
              </label>
              <input
                type="text"
                value={formData.first_name}
                onChange={(e) => setFormData({ ...formData, first_name: e.target.value })}
                className="input w-full"
                placeholder={t('users.firstNamePlaceholder')}
                maxLength={100}
                disabled={formLoading}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('users.lastName')}
              </label>
              <input
                type="text"
                value={formData.last_name}
                onChange={(e) => setFormData({ ...formData, last_name: e.target.value })}
                className="input w-full"
                placeholder={t('users.lastNamePlaceholder')}
                maxLength={100}
                disabled={formLoading}
              />
            </div>
          </div>

          {/* Email */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('auth.email')}
            </label>
            <input
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              className="input w-full"
              placeholder={t('auth.emailPlaceholder')}
              disabled={formLoading}
            />
          </div>

          {/* Password */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('auth.password')} {!editingUser && <span className="text-red-500">*</span>}
              {editingUser && <span className="text-gray-400 dark:text-gray-500">({t('users.leaveEmptyToKeep')})</span>}
            </label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                value={formData.password}
                onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                className="input w-full pr-10"
                placeholder={editingUser ? '••••••••' : t('auth.enterPassword')}
                required={!editingUser}
                minLength={8}
                disabled={formLoading}
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300"
              >
                {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
              </button>
            </div>
          </div>

          {/* Role */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('users.role')} <span className="text-red-500">*</span>
            </label>
            <select
              value={formData.role_id}
              onChange={(e) => setFormData({ ...formData, role_id: e.target.value })}
              className="input w-full"
              required
              disabled={formLoading}
            >
              <option value="">{t('users.selectRole')}</option>
              {roles.map((role) => (
                <option key={role.id} value={role.id}>
                  {role.name} - {role.description}
                </option>
              ))}
            </select>
          </div>

          {/* Active */}
          <div className="flex items-center space-x-3">
            <input
              type="checkbox"
              id="is_active"
              checked={formData.is_active}
              onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
              className="w-4 h-4 rounded-sm border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-primary-600 focus:ring-primary-500"
              disabled={formLoading}
            />
            <label htmlFor="is_active" className="text-sm text-gray-700 dark:text-gray-300">
              {t('users.accountActive')}
            </label>
          </div>

          {/* Actions */}
          <div className="flex space-x-3 pt-4">
            <button
              type="button"
              onClick={() => setShowModal(false)}
              className="flex-1 btn btn-secondary"
              disabled={formLoading}
            >
              {t('common.cancel')}
            </button>
            <button
              type="submit"
              className="flex-1 btn btn-primary"
              disabled={formLoading}
            >
              {formLoading ? (
                <Loader className="w-5 h-5 animate-spin mx-auto" />
              ) : (
                editingUser ? t('users.updateUser') : t('users.createUser')
              )}
            </button>
          </div>
        </form>
      </Modal>

      {/* Link Speaker Modal */}
      <Modal
        isOpen={showLinkSpeakerModal}
        onClose={() => {
          setShowLinkSpeakerModal(false);
          setLinkingUserId(null);
        }}
        title={t('users.linkSpeakerProfile')}
      >
        <div className="space-y-4">
          <p className="text-gray-500 dark:text-gray-400">
            {t('users.selectSpeakerForVoice')}
          </p>

          {availableSpeakers.length === 0 ? (
            <div className="text-center py-6">
              <Mic className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-3" />
              <p className="text-gray-500 dark:text-gray-400">{t('users.noAvailableSpeakers')}</p>
              <p className="text-gray-400 dark:text-gray-500 text-sm">{t('users.allSpeakersLinked')}</p>
            </div>
          ) : (
            <div className="space-y-2 max-h-64 overflow-y-auto">
              {availableSpeakers.map((speaker) => (
                <button
                  key={speaker.id}
                  onClick={() => handleLinkSpeakerSubmit(speaker.id)}
                  className="w-full p-3 bg-gray-100 hover:bg-gray-200 dark:bg-gray-700 dark:hover:bg-gray-600 rounded-lg text-left transition-colors flex items-center space-x-3"
                >
                  <Mic className="w-5 h-5 text-primary-400" />
                  <div>
                    <p className="text-gray-900 dark:text-white font-medium">{speaker.name}</p>
                    <p className="text-gray-500 dark:text-gray-400 text-sm">{t('users.voiceSamplesCount', { count: speaker.embedding_count })}</p>
                  </div>
                </button>
              ))}
            </div>
          )}

          <div className="pt-4">
            <button
              onClick={() => {
                setShowLinkSpeakerModal(false);
                setLinkingUserId(null);
              }}
              className="w-full btn btn-secondary"
            >
              {t('common.cancel')}
            </button>
          </div>
        </div>
      </Modal>

      {ConfirmDialogComponent}
    </div>
  );
}
