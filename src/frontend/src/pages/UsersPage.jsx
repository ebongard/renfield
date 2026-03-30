/**
 * Users Management Page
 *
 * Admin page for managing users: create, edit, delete, assign roles.
 */
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient, { extractApiError, extractFieldErrors } from '../utils/axios';
import Modal from '../components/Modal';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import { useConfirmDialog } from '../components/ConfirmDialog';
import {
  Users, UserPlus, UserCog, Pencil, Trash2, Loader,
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
    is_active: true,
    personality_style: 'freundlich',
    personality_prompt: ''
  });
  const [showPassword, setShowPassword] = useState(false);
  const [formLoading, setFormLoading] = useState(false);
  const [fieldErrors, setFieldErrors] = useState({});

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
      setError(extractApiError(err, t('users.failedToLoad')));
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
    const defaultRoleId = String(roles.find(r => r.name === 'Gast')?.id || roles[0]?.id || '');
    setEditingUser(null);
    setFormData({
      username: '',
      first_name: '',
      last_name: '',
      email: '',
      password: '',
      role_id: defaultRoleId,
      is_active: true,
      personality_style: 'freundlich',
      personality_prompt: ''
    });
    setShowPassword(false);
    setFieldErrors({});
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
      role_id: String(user.role_id),
      is_active: user.is_active,
      personality_style: user.personality_style || 'freundlich',
      personality_prompt: user.personality_prompt || ''
    });
    setShowPassword(false);
    setFieldErrors({});
    setShowModal(true);
  };

  // Update form field and clear its error
  const updateField = (field, value) => {
    setFormData(prev => ({ ...prev, [field]: value }));
    if (fieldErrors[field]) {
      setFieldErrors(prev => { const next = { ...prev }; delete next[field]; return next; });
    }
  };

  // Submit form
  const handleSubmit = async (e) => {
    e.preventDefault();
    // Client-side validation (replaces native HTML5 validation disabled by noValidate)
    const errors = {};
    if (!formData.username || formData.username.length < 3) {
      errors.username = t('users.validationUsernameMin', { defaultValue: 'Mindestens 3 Zeichen' });
    }
    if (!editingUser && (!formData.password || formData.password.length < 8)) {
      errors.password = t('users.validationPasswordMin', { defaultValue: 'Mindestens 8 Zeichen' });
    }
    if (!formData.role_id) {
      errors.role_id = t('users.validationRoleRequired', { defaultValue: 'Rolle ist erforderlich' });
    }
    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors);
      return;
    }
    setFormLoading(true);

    try {
      const token = getAccessToken();
      const headers = { Authorization: `Bearer ${token}` };
      const roleId = parseInt(formData.role_id, 10);
      if (editingUser) {
        // Update user
        const updateData = {
          username: formData.username,
          first_name: formData.first_name || null,
          last_name: formData.last_name || null,
          email: formData.email || null,
          role_id: roleId,
          is_active: formData.is_active,
          personality_style: formData.personality_style,
          personality_prompt: formData.personality_prompt || null
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
        const createPayload = {
          username: formData.username,
          first_name: formData.first_name || null,
          last_name: formData.last_name || null,
          email: formData.email || null,
          password: formData.password,
          role_id: roleId,
          is_active: formData.is_active,
          personality_style: formData.personality_style,
          personality_prompt: formData.personality_prompt || null
        };
        await apiClient.post('/api/users', createPayload, { headers });
        setSuccess(t('users.userCreated'));
      }

      setShowModal(false);
      loadData();
    } catch (err) {
      const fields = extractFieldErrors(err);
      if (Object.keys(fields).length > 0) {
        setFieldErrors(fields);
      } else {
        setError(extractApiError(err, t('users.failedToSave')));
      }
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
      setError(extractApiError(err, t('users.failedToDelete')));
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
      setError(extractApiError(err, t('users.failedToLink')));
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
      setError(extractApiError(err, t('users.failedToUnlink')));
    }
  };

  // Get available speakers (not linked to any user)
  const availableSpeakers = Array.isArray(speakers) && Array.isArray(users)
    ? speakers.filter(s => !users.some(u => u.speaker_id === s.id))
    : [];

  if (loading) {
    return (
      <div className="space-y-6">
        <PageHeader icon={UserCog} title={t('users.title')} subtitle={t('users.subtitle')} />
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
      <PageHeader icon={UserCog} title={t('users.title')} subtitle={t('users.subtitle')} />

      {/* Alerts */}
      {error && <Alert variant="error">{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

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
                      <h3 className="text-base font-semibold text-gray-900 dark:text-white">
                        {user.first_name || user.last_name
                          ? `${user.first_name || ''} ${user.last_name || ''}`.trim()
                          : user.username}
                      </h3>
                      {(user.first_name || user.last_name) && (
                        <span className="text-sm text-gray-500 dark:text-gray-400">({user.username})</span>
                      )}
                      {user.id === currentUser?.id && (
                        <Badge color="accent">{t('users.you')}</Badge>
                      )}
                      {!user.is_active && (
                        <Badge color="red">{t('users.inactive')}</Badge>
                      )}
                    </div>
                    <div className="flex items-center space-x-3 text-sm text-gray-500 dark:text-gray-400">
                      <Badge color={
                        user.role_name === 'Admin' ? 'red' :
                        user.role_name === 'Familie' ? 'blue' :
                        'gray'
                      }>
                        {user.role_name}
                      </Badge>
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
                      className="btn-icon btn-icon-ghost"
                      title={t('users.unlinkSpeaker')}
                    >
                      <Unlink className="w-5 h-5" />
                    </button>
                  ) : (
                    <button
                      onClick={() => handleLinkSpeaker(user.id)}
                      className="btn-icon btn-icon-ghost"
                      title={t('users.linkSpeaker')}
                      disabled={availableSpeakers.length === 0}
                    >
                      <Link2 className="w-5 h-5" />
                    </button>
                  )}
                  <button
                    onClick={() => handleEdit(user)}
                    className="btn-icon btn-icon-ghost"
                    title={t('users.editUser')}
                  >
                    <Pencil className="w-5 h-5" />
                  </button>
                  <button
                    onClick={() => handleDelete(user)}
                    className="btn-icon btn-icon-danger"
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
        <form onSubmit={handleSubmit} noValidate className="space-y-4">
          {/* Username */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('auth.username')} <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={formData.username}
              onChange={(e) => updateField('username', e.target.value)}
              className={`input w-full ${fieldErrors.username ? 'input-error' : ''}`}
              placeholder={t('auth.enterUsername')}
              required
              minLength={3}
              disabled={formLoading}
            />
            {fieldErrors.username && (
              <p className="mt-1 text-sm text-red-600 dark:text-red-400">{fieldErrors.username}</p>
            )}
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
                onChange={(e) => updateField('first_name', e.target.value)}
                className={`input w-full ${fieldErrors.first_name ? 'input-error' : ''}`}
                placeholder={t('users.firstNamePlaceholder')}
                maxLength={100}
                disabled={formLoading}
              />
              {fieldErrors.first_name && (
                <p className="mt-1 text-sm text-red-600 dark:text-red-400">{fieldErrors.first_name}</p>
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('users.lastName')}
              </label>
              <input
                type="text"
                value={formData.last_name}
                onChange={(e) => updateField('last_name', e.target.value)}
                className={`input w-full ${fieldErrors.last_name ? 'input-error' : ''}`}
                placeholder={t('users.lastNamePlaceholder')}
                maxLength={100}
                disabled={formLoading}
              />
              {fieldErrors.last_name && (
                <p className="mt-1 text-sm text-red-600 dark:text-red-400">{fieldErrors.last_name}</p>
              )}
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
              onChange={(e) => updateField('email', e.target.value)}
              className={`input w-full ${fieldErrors.email ? 'input-error' : ''}`}
              placeholder={t('auth.emailPlaceholder')}
              disabled={formLoading}
            />
            {fieldErrors.email && (
              <p className="mt-1 text-sm text-red-600 dark:text-red-400">{fieldErrors.email}</p>
            )}
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
                onChange={(e) => updateField('password', e.target.value)}
                className={`input w-full pr-10 ${fieldErrors.password ? 'input-error' : ''}`}
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
            {fieldErrors.password && (
              <p className="mt-1 text-sm text-red-600 dark:text-red-400">{fieldErrors.password}</p>
            )}
          </div>

          {/* Role */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('users.role')} <span className="text-red-500">*</span>
            </label>
            <select
              value={formData.role_id}
              onChange={(e) => updateField('role_id', e.target.value)}
              className={`input w-full ${fieldErrors.role_id ? 'input-error' : ''}`}
              required
              disabled={formLoading}
            >
              <option value="">{t('users.selectRole')}</option>
              {roles.map((role) => (
                <option key={role.id} value={String(role.id)}>
                  {role.name} - {role.description}
                </option>
              ))}
            </select>
            {fieldErrors.role_id && (
              <p className="mt-1 text-sm text-red-600 dark:text-red-400">{fieldErrors.role_id}</p>
            )}
          </div>

          {/* Active */}
          <div className="flex items-center space-x-3">
            <input
              type="checkbox"
              id="is_active"
              checked={formData.is_active}
              onChange={(e) => updateField('is_active', e.target.checked)}
              className="w-4 h-4 rounded-sm border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-primary-600 focus:ring-primary-500"
              disabled={formLoading}
            />
            <label htmlFor="is_active" className="text-sm text-gray-700 dark:text-gray-300">
              {t('users.accountActive')}
            </label>
          </div>

          {/* Personality Style */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('users.personalityStyle')}
            </label>
            <select
              value={formData.personality_style}
              onChange={(e) => updateField('personality_style', e.target.value)}
              className="input w-full"
              disabled={formLoading}
            >
              <option value="freundlich">{t('users.styles.freundlich')}</option>
              <option value="direkt">{t('users.styles.direkt')}</option>
              <option value="formell">{t('users.styles.formell')}</option>
              <option value="casual">{t('users.styles.casual')}</option>
            </select>
          </div>

          {/* Personality Prompt */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('users.personalityPrompt')}
              <span className="text-gray-400 dark:text-gray-500 font-normal ml-1">({t('common.optional')})</span>
            </label>
            <textarea
              value={formData.personality_prompt}
              onChange={(e) => updateField('personality_prompt', e.target.value)}
              className="input w-full"
              rows={3}
              placeholder={t('users.personalityPromptPlaceholder')}
              disabled={formLoading}
            />
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
