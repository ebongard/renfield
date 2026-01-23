/**
 * Users Management Page
 *
 * Admin page for managing users: create, edit, delete, assign roles.
 */
import { useState, useEffect, useCallback } from 'react';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import { useConfirmDialog } from '../components/ConfirmDialog';
import {
  Users, UserPlus, Pencil, Trash2, Loader, AlertCircle, CheckCircle,
  Shield, User, Mic, Link2, Unlink, Eye, EyeOff, RefreshCw
} from 'lucide-react';

export default function UsersPage() {
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
      setError(err.response?.data?.detail || 'Failed to load users');
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

        setSuccess('User updated successfully');
      } else {
        // Create user
        await apiClient.post('/api/users', {
          username: formData.username,
          email: formData.email || null,
          password: formData.password,
          role_id: parseInt(formData.role_id),
          is_active: formData.is_active
        }, { headers });
        setSuccess('User created successfully');
      }

      setShowModal(false);
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to save user');
    } finally {
      setFormLoading(false);
    }
  };

  // Delete user
  const handleDelete = async (user) => {
    if (user.id === currentUser?.id) {
      setError("You cannot delete your own account");
      return;
    }

    const confirmed = await confirm({
      title: 'Delete User',
      message: `Are you sure you want to delete "${user.username}"? This action cannot be undone.`,
      confirmText: 'Delete',
      variant: 'danger'
    });

    if (!confirmed) return;

    try {
      const token = getAccessToken();
      await apiClient.delete(`/api/users/${user.id}`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setSuccess('User deleted successfully');
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to delete user');
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
      await apiClient.post(`/api/users/${linkingUserId}/link-speaker/${speakerId}`, {}, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setSuccess('Speaker linked successfully');
      setShowLinkSpeakerModal(false);
      setLinkingUserId(null);
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to link speaker');
    }
  };

  // Unlink speaker from user
  const handleUnlinkSpeaker = async (userId) => {
    const confirmed = await confirm({
      title: 'Unlink Speaker',
      message: 'Are you sure you want to unlink the speaker from this user? Voice authentication will no longer work for this user.',
      confirmText: 'Unlink',
      variant: 'warning'
    });

    if (!confirmed) return;

    try {
      const token = getAccessToken();
      await apiClient.delete(`/api/users/${userId}/unlink-speaker`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setSuccess('Speaker unlinked successfully');
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to unlink speaker');
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
          <h1 className="text-2xl font-bold text-white mb-2">User Management</h1>
          <p className="text-gray-400">Manage user accounts and permissions</p>
        </div>
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-400 mb-2" />
          <p className="text-gray-400">Loading users...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <h1 className="text-2xl font-bold text-white mb-2">User Management</h1>
        <p className="text-gray-400">Manage user accounts and permissions</p>
      </div>

      {/* Alerts */}
      {error && (
        <div className="card bg-red-900/20 border-red-700">
          <div className="flex items-center space-x-3">
            <AlertCircle className="w-5 h-5 text-red-500" />
            <p className="text-red-400">{error}</p>
          </div>
        </div>
      )}

      {success && (
        <div className="card bg-green-900/20 border-green-700">
          <div className="flex items-center space-x-3">
            <CheckCircle className="w-5 h-5 text-green-500" />
            <p className="text-green-400">{success}</p>
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-wrap gap-3">
        <button onClick={handleCreate} className="btn btn-primary flex items-center space-x-2">
          <UserPlus className="w-4 h-4" />
          <span>Create User</span>
        </button>
        <button onClick={loadData} className="btn bg-gray-700 hover:bg-gray-600 flex items-center space-x-2">
          <RefreshCw className="w-4 h-4" />
          <span>Refresh</span>
        </button>
      </div>

      {/* Users List */}
      <div className="space-y-3">
        {users.length === 0 ? (
          <div className="card text-center py-12">
            <Users className="w-12 h-12 mx-auto text-gray-600 mb-4" />
            <p className="text-gray-400">No users found</p>
          </div>
        ) : (
          users.map((user) => (
            <div key={user.id} className="card hover:bg-gray-750 transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-4">
                  {/* Avatar */}
                  <div className={`w-12 h-12 rounded-full flex items-center justify-center ${
                    user.role_name === 'Admin' ? 'bg-red-900/50' :
                    user.role_name === 'Familie' ? 'bg-blue-900/50' :
                    'bg-gray-700'
                  }`}>
                    {user.role_name === 'Admin' ? (
                      <Shield className="w-6 h-6 text-red-400" />
                    ) : (
                      <User className="w-6 h-6 text-gray-400" />
                    )}
                  </div>

                  {/* Info */}
                  <div>
                    <div className="flex items-center space-x-2">
                      <h3 className="text-lg font-semibold text-white">{user.username}</h3>
                      {user.id === currentUser?.id && (
                        <span className="text-xs bg-primary-600 text-white px-2 py-0.5 rounded">You</span>
                      )}
                      {!user.is_active && (
                        <span className="text-xs bg-red-600 text-white px-2 py-0.5 rounded">Inactive</span>
                      )}
                    </div>
                    <div className="flex items-center space-x-3 text-sm text-gray-400">
                      <span className={`px-2 py-0.5 rounded ${
                        user.role_name === 'Admin' ? 'bg-red-900/30 text-red-400' :
                        user.role_name === 'Familie' ? 'bg-blue-900/30 text-blue-400' :
                        'bg-gray-700 text-gray-300'
                      }`}>
                        {user.role_name}
                      </span>
                      {user.email && <span>{user.email}</span>}
                      {user.speaker_id && (
                        <span className="flex items-center space-x-1 text-green-400">
                          <Mic className="w-3 h-3" />
                          <span>Voice linked</span>
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
                      className="p-2 text-gray-400 hover:text-yellow-400 hover:bg-gray-700 rounded-lg transition-colors"
                      title="Unlink speaker"
                    >
                      <Unlink className="w-5 h-5" />
                    </button>
                  ) : (
                    <button
                      onClick={() => handleLinkSpeaker(user.id)}
                      className="p-2 text-gray-400 hover:text-green-400 hover:bg-gray-700 rounded-lg transition-colors"
                      title="Link speaker"
                      disabled={availableSpeakers.length === 0}
                    >
                      <Link2 className="w-5 h-5" />
                    </button>
                  )}
                  <button
                    onClick={() => handleEdit(user)}
                    className="p-2 text-gray-400 hover:text-primary-400 hover:bg-gray-700 rounded-lg transition-colors"
                    title="Edit user"
                  >
                    <Pencil className="w-5 h-5" />
                  </button>
                  <button
                    onClick={() => handleDelete(user)}
                    className="p-2 text-gray-400 hover:text-red-400 hover:bg-gray-700 rounded-lg transition-colors"
                    title="Delete user"
                    disabled={user.id === currentUser?.id}
                  >
                    <Trash2 className="w-5 h-5" />
                  </button>
                </div>
              </div>

              {/* Additional info */}
              {user.last_login && (
                <div className="mt-3 pt-3 border-t border-gray-700 text-sm text-gray-500">
                  Last login: {new Date(user.last_login).toLocaleString()}
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
        title={editingUser ? 'Edit User' : 'Create User'}
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Username */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Username <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={formData.username}
              onChange={(e) => setFormData({ ...formData, username: e.target.value })}
              className="input w-full"
              placeholder="Enter username"
              required
              minLength={3}
              disabled={formLoading}
            />
          </div>

          {/* Email */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Email
            </label>
            <input
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              className="input w-full"
              placeholder="user@example.com"
              disabled={formLoading}
            />
          </div>

          {/* Password */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Password {!editingUser && <span className="text-red-500">*</span>}
              {editingUser && <span className="text-gray-500">(leave empty to keep current)</span>}
            </label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                value={formData.password}
                onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                className="input w-full pr-10"
                placeholder={editingUser ? '••••••••' : 'Enter password'}
                required={!editingUser}
                minLength={8}
                disabled={formLoading}
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-300"
              >
                {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
              </button>
            </div>
          </div>

          {/* Role */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Role <span className="text-red-500">*</span>
            </label>
            <select
              value={formData.role_id}
              onChange={(e) => setFormData({ ...formData, role_id: e.target.value })}
              className="input w-full"
              required
              disabled={formLoading}
            >
              <option value="">Select a role</option>
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
              className="w-4 h-4 rounded border-gray-600 bg-gray-700 text-primary-600 focus:ring-primary-500"
              disabled={formLoading}
            />
            <label htmlFor="is_active" className="text-sm text-gray-300">
              Account is active
            </label>
          </div>

          {/* Actions */}
          <div className="flex space-x-3 pt-4">
            <button
              type="button"
              onClick={() => setShowModal(false)}
              className="flex-1 btn bg-gray-700 hover:bg-gray-600"
              disabled={formLoading}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 btn btn-primary"
              disabled={formLoading}
            >
              {formLoading ? (
                <Loader className="w-5 h-5 animate-spin mx-auto" />
              ) : (
                editingUser ? 'Update User' : 'Create User'
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
        title="Link Speaker Profile"
      >
        <div className="space-y-4">
          <p className="text-gray-400">
            Select a speaker profile to link with this user for voice authentication.
          </p>

          {availableSpeakers.length === 0 ? (
            <div className="text-center py-6">
              <Mic className="w-12 h-12 mx-auto text-gray-600 mb-3" />
              <p className="text-gray-400">No available speaker profiles</p>
              <p className="text-gray-500 text-sm">All speaker profiles are already linked to users</p>
            </div>
          ) : (
            <div className="space-y-2 max-h-64 overflow-y-auto">
              {availableSpeakers.map((speaker) => (
                <button
                  key={speaker.id}
                  onClick={() => handleLinkSpeakerSubmit(speaker.id)}
                  className="w-full p-3 bg-gray-700 hover:bg-gray-600 rounded-lg text-left transition-colors flex items-center space-x-3"
                >
                  <Mic className="w-5 h-5 text-primary-400" />
                  <div>
                    <p className="text-white font-medium">{speaker.name}</p>
                    <p className="text-gray-400 text-sm">{speaker.embedding_count} voice samples</p>
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
              className="w-full btn bg-gray-700 hover:bg-gray-600"
            >
              Cancel
            </button>
          </div>
        </div>
      </Modal>

      {ConfirmDialogComponent}
    </div>
  );
}
