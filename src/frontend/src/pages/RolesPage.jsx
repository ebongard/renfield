/**
 * Roles Management Page
 *
 * Admin page for managing roles and permissions.
 */
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import { useConfirmDialog } from '../components/ConfirmDialog';
import {
  Shield, Plus, Pencil, Trash2, Loader, AlertCircle, CheckCircle,
  Lock, RefreshCw, ChevronDown, ChevronRight, Info
} from 'lucide-react';

// Permission categories for better organization
const PERMISSION_CATEGORIES = {
  'Knowledge Bases': {
    description: 'Access to knowledge base documents',
    permissions: ['kb.none', 'kb.own', 'kb.shared', 'kb.all']
  },
  'Home Assistant': {
    description: 'Smart home device control',
    permissions: ['ha.none', 'ha.read', 'ha.control', 'ha.full']
  },
  'Cameras': {
    description: 'Camera and video access',
    permissions: ['cam.none', 'cam.view', 'cam.full']
  },
  'Conversations': {
    description: 'Chat history access',
    permissions: ['chat.own', 'chat.all']
  },
  'Rooms & Devices': {
    description: 'Room and device management',
    permissions: ['rooms.read', 'rooms.manage']
  },
  'Speakers': {
    description: 'Voice profile management',
    permissions: ['speakers.own', 'speakers.all']
  },
  'Administration': {
    description: 'System administration',
    permissions: ['admin']
  }
};

// Permission descriptions
const PERMISSION_DESCRIPTIONS = {
  'kb.none': 'No access to knowledge bases',
  'kb.own': 'Access only own knowledge bases',
  'kb.shared': 'Access own and shared knowledge bases',
  'kb.all': 'Full access to all knowledge bases',
  'ha.none': 'No access to Home Assistant',
  'ha.read': 'View device states only',
  'ha.control': 'Control devices (on/off)',
  'ha.full': 'Full control including services',
  'cam.none': 'No camera access',
  'cam.view': 'View camera events',
  'cam.full': 'Full camera access including snapshots',
  'chat.own': 'Access only own conversations',
  'chat.all': 'Access all conversations',
  'rooms.read': 'View rooms and devices',
  'rooms.manage': 'Manage rooms and devices',
  'speakers.own': 'Manage own speaker profile',
  'speakers.all': 'Manage all speaker profiles',
  'admin': 'Full system administration'
};

export default function RolesPage() {
  const { t } = useTranslation();
  const { getAccessToken } = useAuth();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [roles, setRoles] = useState([]);
  const [allPermissions, setAllPermissions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Modal states
  const [showModal, setShowModal] = useState(false);
  const [editingRole, setEditingRole] = useState(null);

  // Form state
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    permissions: []
  });
  const [formLoading, setFormLoading] = useState(false);
  const [expandedCategories, setExpandedCategories] = useState({});

  // Load data
  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const token = getAccessToken();
      const headers = { Authorization: `Bearer ${token}` };

      const [rolesRes, permsRes] = await Promise.all([
        apiClient.get('/api/roles', { headers }),
        apiClient.get('/api/auth/permissions', { headers })
      ]);

      setRoles(Array.isArray(rolesRes.data) ? rolesRes.data : []);
      setAllPermissions(Array.isArray(permsRes.data) ? permsRes.data : []);
    } catch (err) {
      setError(err.response?.data?.detail || t('roles.failedToLoad'));
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

  // Toggle category expansion
  const toggleCategory = (category) => {
    setExpandedCategories(prev => ({
      ...prev,
      [category]: !prev[category]
    }));
  };

  // Open create modal
  const handleCreate = () => {
    setEditingRole(null);
    setFormData({
      name: '',
      description: '',
      permissions: []
    });
    // Expand all categories for new role
    const expanded = {};
    Object.keys(PERMISSION_CATEGORIES).forEach(cat => {
      expanded[cat] = true;
    });
    setExpandedCategories(expanded);
    setShowModal(true);
  };

  // Open edit modal
  const handleEdit = (role) => {
    setEditingRole(role);
    setFormData({
      name: role.name,
      description: role.description || '',
      permissions: [...role.permissions]
    });
    // Expand categories that have selected permissions
    const expanded = {};
    Object.entries(PERMISSION_CATEGORIES).forEach(([cat, { permissions }]) => {
      expanded[cat] = permissions.some(p => role.permissions.includes(p));
    });
    setExpandedCategories(expanded);
    setShowModal(true);
  };

  // Toggle permission
  const togglePermission = (permission) => {
    setFormData(prev => {
      const permissions = prev.permissions.includes(permission)
        ? prev.permissions.filter(p => p !== permission)
        : [...prev.permissions, permission];
      return { ...prev, permissions };
    });
  };

  // Select all in category
  const selectAllInCategory = (category) => {
    const categoryPermissions = PERMISSION_CATEGORIES[category].permissions;
    setFormData(prev => {
      const newPermissions = [...prev.permissions];
      categoryPermissions.forEach(p => {
        if (!newPermissions.includes(p)) {
          newPermissions.push(p);
        }
      });
      return { ...prev, permissions: newPermissions };
    });
  };

  // Clear all in category
  const clearCategory = (category) => {
    const categoryPermissions = PERMISSION_CATEGORIES[category].permissions;
    setFormData(prev => ({
      ...prev,
      permissions: prev.permissions.filter(p => !categoryPermissions.includes(p))
    }));
  };

  // Submit form
  const handleSubmit = async (e) => {
    e.preventDefault();
    setFormLoading(true);

    try {
      const token = getAccessToken();
      const headers = { Authorization: `Bearer ${token}` };

      const data = {
        name: formData.name,
        description: formData.description || null,
        permissions: formData.permissions
      };

      if (editingRole) {
        await apiClient.patch(`/api/roles/${editingRole.id}`, data, { headers });
        setSuccess(t('roles.roleUpdated'));
      } else {
        await apiClient.post('/api/roles', data, { headers });
        setSuccess(t('roles.roleCreated'));
      }

      setShowModal(false);
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || t('roles.failedToSave'));
    } finally {
      setFormLoading(false);
    }
  };

  // Delete role
  const handleDelete = async (role) => {
    if (role.is_system) {
      setError(t('roles.systemRoleCannotDelete'));
      return;
    }

    const confirmed = await confirm({
      title: t('roles.deleteRole'),
      message: t('roles.deleteRoleConfirm', { name: role.name }),
      confirmText: t('common.delete'),
      variant: 'danger'
    });

    if (!confirmed) return;

    try {
      const token = getAccessToken();
      await apiClient.delete(`/api/roles/${role.id}`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      setSuccess(t('roles.roleDeleted'));
      loadData();
    } catch (err) {
      setError(err.response?.data?.detail || t('roles.failedToDelete'));
    }
  };

  // Get count of permissions in category
  const getCategoryPermissionCount = (category, permissions) => {
    const categoryPerms = PERMISSION_CATEGORIES[category].permissions;
    return permissions.filter(p => categoryPerms.includes(p)).length;
  };

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="card">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('roles.title')}</h1>
          <p className="text-gray-500 dark:text-gray-400">{t('roles.subtitle')}</p>
        </div>
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
          <p className="text-gray-500 dark:text-gray-400">{t('roles.loading')}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('roles.title')}</h1>
        <p className="text-gray-500 dark:text-gray-400">{t('roles.subtitle')}</p>
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
          <Plus className="w-4 h-4" />
          <span>{t('roles.createRole')}</span>
        </button>
        <button onClick={loadData} className="btn btn-secondary flex items-center space-x-2">
          <RefreshCw className="w-4 h-4" />
          <span>{t('common.refresh')}</span>
        </button>
      </div>

      {/* Roles List */}
      <div className="space-y-3">
        {roles.length === 0 ? (
          <div className="card text-center py-12">
            <Shield className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-4" />
            <p className="text-gray-500 dark:text-gray-400">{t('roles.noRolesFound')}</p>
          </div>
        ) : (
          roles.map((role) => (
            <div key={role.id} className="card hover:bg-gray-50 dark:hover:bg-gray-750 transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-4">
                  {/* Icon */}
                  <div className={`w-12 h-12 rounded-full flex items-center justify-center ${
                    role.name === 'Admin' ? 'bg-red-100 dark:bg-red-900/50' :
                    role.name === 'Familie' ? 'bg-blue-100 dark:bg-blue-900/50' :
                    'bg-gray-200 dark:bg-gray-700'
                  }`}>
                    <Shield className={`w-6 h-6 ${
                      role.name === 'Admin' ? 'text-red-600 dark:text-red-400' :
                      role.name === 'Familie' ? 'text-blue-600 dark:text-blue-400' :
                      'text-gray-500 dark:text-gray-400'
                    }`} />
                  </div>

                  {/* Info */}
                  <div>
                    <div className="flex items-center space-x-2">
                      <h3 className="text-lg font-semibold text-gray-900 dark:text-white">{role.name}</h3>
                      {role.is_system && (
                        <span className="flex items-center space-x-1 text-xs bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 px-2 py-0.5 rounded-sm">
                          <Lock className="w-3 h-3" />
                          <span>{t('roles.system')}</span>
                        </span>
                      )}
                    </div>
                    {role.description && (
                      <p className="text-gray-500 dark:text-gray-400 text-sm">{role.description}</p>
                    )}
                    <div className="flex flex-wrap gap-1 mt-2">
                      {role.permissions.slice(0, 5).map((perm) => (
                        <span
                          key={perm}
                          className="text-xs bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 px-2 py-0.5 rounded-sm"
                          title={PERMISSION_DESCRIPTIONS[perm]}
                        >
                          {perm}
                        </span>
                      ))}
                      {role.permissions.length > 5 && (
                        <span className="text-xs bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-400 px-2 py-0.5 rounded-sm">
                          {t('roles.more', { count: role.permissions.length - 5 })}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {/* Actions */}
                <div className="flex items-center space-x-2">
                  <button
                    onClick={() => handleEdit(role)}
                    className="p-2 text-gray-500 hover:text-primary-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-primary-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                    title={t('roles.editRole')}
                  >
                    <Pencil className="w-5 h-5" />
                  </button>
                  {!role.is_system && (
                    <button
                      onClick={() => handleDelete(role)}
                      className="p-2 text-gray-500 hover:text-red-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-red-400 dark:hover:bg-gray-700 rounded-lg transition-colors"
                      title={t('roles.deleteRole')}
                    >
                      <Trash2 className="w-5 h-5" />
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Create/Edit Modal */}
      <Modal
        isOpen={showModal}
        onClose={() => setShowModal(false)}
        title={editingRole ? t('roles.editRole') : t('roles.createRole')}
        maxWidth="max-w-2xl"
      >
        <form onSubmit={handleSubmit} className="space-y-6">
          {/* Basic Info */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('roles.roleName')} <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="input w-full"
                placeholder={t('roles.roleNamePlaceholder')}
                required
                disabled={formLoading || editingRole?.is_system}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('common.description')}
              </label>
              <input
                type="text"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                className="input w-full"
                placeholder={t('roles.descriptionPlaceholder')}
                disabled={formLoading}
              />
            </div>
          </div>

          {/* Permissions */}
          <div>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-lg font-medium text-gray-900 dark:text-white">{t('roles.permissions')}</h3>
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {t('roles.selected', { count: formData.permissions.length })}
              </span>
            </div>

            <div className="space-y-2 max-h-96 overflow-y-auto pr-2">
              {Object.entries(PERMISSION_CATEGORIES).map(([category, { description, permissions }]) => {
                const isExpanded = expandedCategories[category];
                const selectedCount = getCategoryPermissionCount(category, formData.permissions);

                return (
                  <div key={category} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                    {/* Category Header */}
                    <button
                      type="button"
                      onClick={() => toggleCategory(category)}
                      className="w-full flex items-center justify-between p-3 bg-gray-100 hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-750 transition-colors"
                    >
                      <div className="flex items-center space-x-3">
                        {isExpanded ? (
                          <ChevronDown className="w-4 h-4 text-gray-500 dark:text-gray-400" />
                        ) : (
                          <ChevronRight className="w-4 h-4 text-gray-500 dark:text-gray-400" />
                        )}
                        <div className="text-left">
                          <p className="text-gray-900 dark:text-white font-medium">{category}</p>
                          <p className="text-gray-400 dark:text-gray-500 text-xs">{description}</p>
                        </div>
                      </div>
                      <span className={`text-sm px-2 py-0.5 rounded ${
                        selectedCount > 0 ? 'bg-primary-600 text-white' : 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                      }`}>
                        {selectedCount}/{permissions.length}
                      </span>
                    </button>

                    {/* Category Permissions */}
                    {isExpanded && (
                      <div className="p-3 bg-gray-50 dark:bg-gray-850 border-t border-gray-200 dark:border-gray-700">
                        {/* Quick actions */}
                        <div className="flex space-x-2 mb-3">
                          <button
                            type="button"
                            onClick={() => selectAllInCategory(category)}
                            className="text-xs text-primary-600 hover:text-primary-500 dark:text-primary-400 dark:hover:text-primary-300"
                          >
                            {t('roles.selectAll')}
                          </button>
                          <span className="text-gray-300 dark:text-gray-600">|</span>
                          <button
                            type="button"
                            onClick={() => clearCategory(category)}
                            className="text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300"
                          >
                            {t('roles.clear')}
                          </button>
                        </div>

                        {/* Permission checkboxes */}
                        <div className="space-y-2">
                          {permissions.map((perm) => (
                            <label
                              key={perm}
                              className="flex items-start space-x-3 p-2 rounded-sm hover:bg-gray-100 dark:hover:bg-gray-800 cursor-pointer"
                            >
                              <input
                                type="checkbox"
                                checked={formData.permissions.includes(perm)}
                                onChange={() => togglePermission(perm)}
                                className="mt-0.5 w-4 h-4 rounded-sm border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-primary-600 focus:ring-primary-500"
                                disabled={formLoading}
                              />
                              <div>
                                <p className="text-gray-900 dark:text-white text-sm font-medium">{perm}</p>
                                <p className="text-gray-400 dark:text-gray-500 text-xs">
                                  {PERMISSION_DESCRIPTIONS[perm]}
                                </p>
                              </div>
                            </label>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Info for system roles */}
          {editingRole?.is_system && (
            <div className="flex items-start space-x-3 p-3 bg-yellow-100 dark:bg-yellow-900/20 border border-yellow-300 dark:border-yellow-700 rounded-lg">
              <Info className="w-5 h-5 text-yellow-500 shrink-0 mt-0.5" />
              <div>
                <p className="text-yellow-700 dark:text-yellow-400 font-medium">{t('roles.systemRole')}</p>
                <p className="text-yellow-600 dark:text-yellow-400/70 text-sm">
                  {t('roles.systemRoleInfo')}
                </p>
              </div>
            </div>
          )}

          {/* Actions */}
          <div className="flex space-x-3 pt-4 border-t border-gray-200 dark:border-gray-700">
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
                editingRole ? t('roles.updateRole') : t('roles.createRole')
              )}
            </button>
          </div>
        </form>
      </Modal>

      {ConfirmDialogComponent}
    </div>
  );
}
