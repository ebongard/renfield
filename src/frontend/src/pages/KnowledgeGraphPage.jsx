import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Brain, Link2, BarChart3, Search, Trash2, Edit3, Merge, X,
  ChevronLeft, ChevronRight, ArrowRight, Lock, Users,
} from 'lucide-react';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import { useConfirmDialog } from '../components/ConfirmDialog';

const ENTITY_TYPES = ['person', 'place', 'organization', 'thing', 'event', 'concept'];

const TYPE_COLORS = {
  person: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  place: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  organization: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
  thing: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  event: 'bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-300',
  concept: 'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-300',
};

const TABS = ['entities', 'relations', 'stats'];

export default function KnowledgeGraphPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [activeTab, setActiveTab] = useState('entities');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Entities state
  const [entities, setEntities] = useState([]);
  const [entitiesTotal, setEntitiesTotal] = useState(0);
  const [entitiesPage, setEntitiesPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [scopeFilter, setScopeFilter] = useState('all');
  const [availableScopes, setAvailableScopes] = useState([]);
  const [scopeMenuEntity, setScopeMenuEntity] = useState(null);

  // Relations state
  const [relations, setRelations] = useState([]);
  const [relationsTotal, setRelationsTotal] = useState(0);
  const [relationsPage, setRelationsPage] = useState(1);
  const [entityFilter, setEntityFilter] = useState('');

  // Stats state
  const [stats, setStats] = useState(null);

  // Edit modal
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingEntity, setEditingEntity] = useState(null);
  const [formName, setFormName] = useState('');
  const [formType, setFormType] = useState('thing');
  const [formDescription, setFormDescription] = useState('');

  // Merge state
  const [mergeMode, setMergeMode] = useState(false);
  const [mergeSelection, setMergeSelection] = useState([]);

  const PAGE_SIZE = 50;

  // Auto-clear messages
  useEffect(() => {
    if (error || success) {
      const timer = setTimeout(() => { setError(null); setSuccess(null); }, 5000);
      return () => clearTimeout(timer);
    }
  }, [error, success]);

  // Load scopes
  const loadScopes = async () => {
    try {
      const response = await apiClient.get('/api/knowledge-graph/scopes', {
        params: { lang: t('lang') === 'de' ? 'de' : 'en' }
      });
      setAvailableScopes(response.data.scopes);
    } catch (err) {
      console.error('Failed to load KG scopes:', err);
      // Fallback to default scopes
      setAvailableScopes([
        { name: 'personal', label: t('knowledgeGraph.personal'), description: '' },
        { name: 'family', label: t('knowledgeGraph.family'), description: '' },
        { name: 'public', label: t('knowledgeGraph.public'), description: '' },
      ]);
    }
  };

  // Load scopes on mount
  useEffect(() => {
    loadScopes();
  }, []);

  // Load entities
  const loadEntities = async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      params.set('page', String(entitiesPage));
      params.set('size', String(PAGE_SIZE));
      if (typeFilter) params.set('type', typeFilter);
      if (searchQuery) params.set('search', searchQuery);
      if (scopeFilter && scopeFilter !== 'all') params.set('scope', scopeFilter);

      const response = await apiClient.get(`/api/knowledge-graph/entities?${params}`);
      setEntities(response.data.entities);
      setEntitiesTotal(response.data.total);
      setError(null);
    } catch (err) {
      setError(t('knowledgeGraph.couldNotLoad'));
    } finally {
      setLoading(false);
    }
  };

  // Load relations
  const loadRelations = async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      params.set('page', String(relationsPage));
      params.set('size', String(PAGE_SIZE));
      if (entityFilter) params.set('entity_id', entityFilter);

      const response = await apiClient.get(`/api/knowledge-graph/relations?${params}`);
      setRelations(response.data.relations);
      setRelationsTotal(response.data.total);
      setError(null);
    } catch (err) {
      setError(t('knowledgeGraph.couldNotLoad'));
    } finally {
      setLoading(false);
    }
  };

  // Load stats
  const loadStats = async () => {
    try {
      setLoading(true);
      const response = await apiClient.get('/api/knowledge-graph/stats');
      setStats(response.data);
      setError(null);
    } catch (err) {
      setError(t('knowledgeGraph.couldNotLoad'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === 'entities') loadEntities();
    else if (activeTab === 'relations') loadRelations();
    else if (activeTab === 'stats') loadStats();
  }, [activeTab, entitiesPage, relationsPage, typeFilter, searchQuery, scopeFilter, entityFilter]);

  // Edit entity
  const openEditModal = (entity) => {
    setEditingEntity(entity);
    setFormName(entity.name);
    setFormType(entity.entity_type);
    setFormDescription(entity.description || '');
    setShowEditModal(true);
  };

  const handleSaveEntity = async () => {
    if (!editingEntity) return;
    try {
      await apiClient.put(`/api/knowledge-graph/entities/${editingEntity.id}`, {
        name: formName,
        entity_type: formType,
        description: formDescription || null,
      });
      setShowEditModal(false);
      setSuccess(t('common.success'));
      loadEntities();
    } catch (err) {
      setError(t('common.error'));
    }
  };

  // Delete entity
  const handleDeleteEntity = async (entity) => {
    const confirmed = await confirm(
      t('knowledgeGraph.deleteConfirm', { name: entity.name })
    );
    if (!confirmed) return;
    try {
      await apiClient.delete(`/api/knowledge-graph/entities/${entity.id}`);
      setSuccess(t('common.success'));
      loadEntities();
    } catch (err) {
      setError(t('common.error'));
    }
  };

  // Delete relation
  const handleDeleteRelation = async (relation) => {
    const confirmed = await confirm(t('knowledgeGraph.deleteRelationConfirm'));
    if (!confirmed) return;
    try {
      await apiClient.delete(`/api/knowledge-graph/relations/${relation.id}`);
      setSuccess(t('common.success'));
      loadRelations();
    } catch (err) {
      setError(t('common.error'));
    }
  };

  // Merge entities
  const toggleMergeSelection = (entity) => {
    setMergeSelection(prev => {
      const exists = prev.find(e => e.id === entity.id);
      if (exists) return prev.filter(e => e.id !== entity.id);
      if (prev.length >= 2) return [prev[1], entity];
      return [...prev, entity];
    });
  };

  const handleMerge = async () => {
    if (mergeSelection.length !== 2) return;
    const confirmed = await confirm(
      t('knowledgeGraph.mergeConfirm', {
        source: mergeSelection[0].name,
        target: mergeSelection[1].name,
      })
    );
    if (!confirmed) return;
    try {
      await apiClient.post('/api/knowledge-graph/entities/merge', {
        source_id: mergeSelection[0].id,
        target_id: mergeSelection[1].id,
      });
      setMergeMode(false);
      setMergeSelection([]);
      setSuccess(t('common.success'));
      loadEntities();
    } catch (err) {
      setError(t('common.error'));
    }
  };

  // Update entity scope
  const handleUpdateScope = async (entity, newScope) => {
    try {
      await apiClient.patch(
        `/api/knowledge-graph/entities/${entity.id}/scope`,
        { scope: newScope }
      );

      const scopeInfo = availableScopes.find(s => s.name === newScope);
      setSuccess(
        t('knowledgeGraph.scopeUpdated', { name: entity.name, scope: scopeInfo?.label || newScope })
      );
      setScopeMenuEntity(null);
      loadEntities();
    } catch (err) {
      setError(t('common.error'));
    }
  };

  // Filter relations by entity
  const showRelationsForEntity = (entityId) => {
    setEntityFilter(String(entityId));
    setRelationsPage(1);
    setActiveTab('relations');
  };

  const totalEntitiesPages = Math.ceil(entitiesTotal / PAGE_SIZE);
  const totalRelationsPages = Math.ceil(relationsTotal / PAGE_SIZE);

  return (
    <div>
      {ConfirmDialogComponent}

      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-indigo-100 dark:bg-indigo-900/30 rounded-lg">
            <Brain className="w-6 h-6 text-indigo-600 dark:text-indigo-400" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
              {t('knowledgeGraph.title')}
            </h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {t('knowledgeGraph.subtitle')}
            </p>
          </div>
        </div>
      </div>

      {/* Messages */}
      {error && (
        <div className="mb-4 p-3 bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 rounded-lg">
          {error}
        </div>
      )}
      {success && (
        <div className="mb-4 p-3 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300 rounded-lg">
          {success}
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-gray-200 dark:border-gray-700">
        {TABS.map(tab => (
          <button
            key={tab}
            onClick={() => { setActiveTab(tab); setMergeMode(false); setMergeSelection([]); }}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab
                ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            {tab === 'entities' && <Brain className="w-4 h-4" />}
            {tab === 'relations' && <Link2 className="w-4 h-4" />}
            {tab === 'stats' && <BarChart3 className="w-4 h-4" />}
            {t(`knowledgeGraph.${tab}`)}
          </button>
        ))}
      </div>

      {/* Entities Tab */}
      {activeTab === 'entities' && (
        <div>
          {/* Controls */}
          <div className="flex flex-wrap items-center gap-3 mb-4">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => { setSearchQuery(e.target.value); setEntitiesPage(1); }}
                placeholder={t('knowledgeGraph.searchPlaceholder')}
                className="input pl-9 w-full"
              />
            </div>

            <select
              value={typeFilter}
              onChange={(e) => { setTypeFilter(e.target.value); setEntitiesPage(1); }}
              className="input w-auto"
            >
              <option value="">{t('common.all')}</option>
              {ENTITY_TYPES.map(type => (
                <option key={type} value={type}>{t(`knowledgeGraph.${type}`)}</option>
              ))}
            </select>

            <select
              value={scopeFilter}
              onChange={(e) => { setScopeFilter(e.target.value); setEntitiesPage(1); }}
              className="input w-auto"
            >
              <option value="all">{t('common.all')}</option>
              {availableScopes.map(scope => (
                <option key={scope.name} value={scope.name}>{scope.label}</option>
              ))}
            </select>

            <button
              onClick={() => { setMergeMode(!mergeMode); setMergeSelection([]); }}
              className={`btn-secondary flex items-center gap-2 ${mergeMode ? 'ring-2 ring-indigo-500' : ''}`}
            >
              <Merge className="w-4 h-4" />
              {t('knowledgeGraph.merge')}
            </button>
          </div>

          {/* Merge bar */}
          {mergeMode && (
            <div className="mb-4 p-3 bg-indigo-50 dark:bg-indigo-900/20 rounded-lg flex items-center justify-between">
              <div className="text-sm text-indigo-700 dark:text-indigo-300">
                {mergeSelection.length === 0 && t('knowledgeGraph.mergeSelectFirst')}
                {mergeSelection.length === 1 && t('knowledgeGraph.mergeSelectSecond', { name: mergeSelection[0].name })}
                {mergeSelection.length === 2 && (
                  <span>
                    {mergeSelection[0].name} <ArrowRight className="w-4 h-4 inline" /> {mergeSelection[1].name}
                  </span>
                )}
              </div>
              <div className="flex gap-2">
                {mergeSelection.length === 2 && (
                  <button onClick={handleMerge} className="btn-primary text-sm">
                    {t('knowledgeGraph.merge')}
                  </button>
                )}
                <button onClick={() => { setMergeMode(false); setMergeSelection([]); }} className="btn-secondary text-sm">
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}

          {/* Table */}
          {loading ? (
            <div className="text-center py-12 text-gray-500 dark:text-gray-400">
              {t('common.loading')}
            </div>
          ) : entities.length === 0 ? (
            <div className="text-center py-12">
              <Brain className="w-12 h-12 mx-auto text-gray-300 dark:text-gray-600 mb-3" />
              <p className="text-gray-500 dark:text-gray-400">{t('knowledgeGraph.noEntities')}</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-gray-200 dark:border-gray-700">
                    {mergeMode && <th className="py-3 px-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase" />}
                    <th className="py-3 px-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">{t('knowledgeGraph.entityName')}</th>
                    <th className="py-3 px-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">{t('knowledgeGraph.entityType')}</th>
                    <th className="py-3 px-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">{t('knowledgeGraph.mentions')}</th>
                    <th className="py-3 px-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">{t('knowledgeGraph.lastSeen')}</th>
                    <th className="py-3 px-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                  {entities.map(entity => {
                    const isSelected = mergeSelection.find(e => e.id === entity.id);
                    return (
                      <tr
                        key={entity.id}
                        className={`hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors ${
                          isSelected ? 'bg-indigo-50 dark:bg-indigo-900/20' : ''
                        }`}
                        onClick={mergeMode ? () => toggleMergeSelection(entity) : undefined}
                        style={mergeMode ? { cursor: 'pointer' } : undefined}
                      >
                        {mergeMode && (
                          <td className="py-3 px-3">
                            <input
                              type="checkbox"
                              checked={!!isSelected}
                              readOnly
                              className="rounded border-gray-300 dark:border-gray-600"
                            />
                          </td>
                        )}
                        <td className="py-3 px-3">
                          <div>
                            <span className="font-medium text-gray-900 dark:text-white">{entity.name}</span>
                            {entity.description && (
                              <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 truncate max-w-xs">{entity.description}</p>
                            )}
                          </div>
                        </td>
                        <td className="py-3 px-3">
                          <div className="flex items-center gap-2">
                            <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${TYPE_COLORS[entity.entity_type] || TYPE_COLORS.thing}`}>
                              {t(`knowledgeGraph.${entity.entity_type}`)}
                            </span>
                            {entity.scope && entity.scope !== 'personal' && (
                              <span className="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300">
                                {availableScopes.find(s => s.name === entity.scope)?.label || entity.scope}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="py-3 px-3 text-sm text-gray-600 dark:text-gray-300">{entity.mention_count}</td>
                        <td className="py-3 px-3 text-sm text-gray-500 dark:text-gray-400">
                          {entity.last_seen_at ? new Date(entity.last_seen_at).toLocaleDateString() : '-'}
                        </td>
                        <td className="py-3 px-3 text-right">
                          {!mergeMode && (
                            <div className="flex items-center justify-end gap-1">
                              <div className="relative">
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setScopeMenuEntity(scopeMenuEntity?.id === entity.id ? null : entity);
                                  }}
                                  className={`p-1.5 rounded ${
                                    entity.scope === 'personal' ? 'text-gray-400' :
                                    'text-green-600 dark:text-green-400'
                                  } hover:bg-gray-100 dark:hover:bg-gray-800`}
                                  title={t('knowledgeGraph.changeScope')}
                                >
                                  {entity.scope === 'personal' ? <Lock className="w-4 h-4" /> : <Users className="w-4 h-4" />}
                                </button>

                                {scopeMenuEntity?.id === entity.id && (
                                  <div className="absolute right-0 mt-1 w-48 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-10">
                                    {availableScopes.map((scopeInfo) => (
                                      <button
                                        key={scopeInfo.name}
                                        onClick={() => handleUpdateScope(entity, scopeInfo.name)}
                                        className={`w-full px-4 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 ${
                                          entity.scope === scopeInfo.name ? 'font-semibold' : ''
                                        }`}
                                        title={scopeInfo.description}
                                      >
                                        {scopeInfo.label}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>
                              <button
                                onClick={() => showRelationsForEntity(entity.id)}
                                className="p-1.5 rounded text-gray-400 hover:text-indigo-600 dark:hover:text-indigo-400 hover:bg-gray-100 dark:hover:bg-gray-800"
                                title={t('knowledgeGraph.showRelations')}
                              >
                                <Link2 className="w-4 h-4" />
                              </button>
                              <button
                                onClick={() => openEditModal(entity)}
                                className="p-1.5 rounded text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-gray-100 dark:hover:bg-gray-800"
                                title={t('common.edit')}
                              >
                                <Edit3 className="w-4 h-4" />
                              </button>
                              <button
                                onClick={() => handleDeleteEntity(entity)}
                                className="p-1.5 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-800"
                                title={t('common.delete')}
                              >
                                <Trash2 className="w-4 h-4" />
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {totalEntitiesPages > 1 && (
            <div className="flex items-center justify-between mt-4">
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {entitiesTotal} {t('knowledgeGraph.entities').toLowerCase()}
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setEntitiesPage(p => Math.max(1, p - 1))}
                  disabled={entitiesPage <= 1}
                  className="btn-secondary p-2 disabled:opacity-50"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <span className="text-sm text-gray-600 dark:text-gray-300">
                  {entitiesPage} / {totalEntitiesPages}
                </span>
                <button
                  onClick={() => setEntitiesPage(p => Math.min(totalEntitiesPages, p + 1))}
                  disabled={entitiesPage >= totalEntitiesPages}
                  className="btn-secondary p-2 disabled:opacity-50"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Relations Tab */}
      {activeTab === 'relations' && (
        <div>
          {/* Filter bar */}
          {entityFilter && (
            <div className="mb-4 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg flex items-center justify-between">
              <span className="text-sm text-gray-600 dark:text-gray-300">
                {t('knowledgeGraph.filteredByEntity')} #{entityFilter}
              </span>
              <button
                onClick={() => { setEntityFilter(''); setRelationsPage(1); }}
                className="btn-secondary text-sm flex items-center gap-1"
              >
                <X className="w-3 h-3" /> {t('knowledgeGraph.clearFilter')}
              </button>
            </div>
          )}

          {loading ? (
            <div className="text-center py-12 text-gray-500 dark:text-gray-400">
              {t('common.loading')}
            </div>
          ) : relations.length === 0 ? (
            <div className="text-center py-12">
              <Link2 className="w-12 h-12 mx-auto text-gray-300 dark:text-gray-600 mb-3" />
              <p className="text-gray-500 dark:text-gray-400">{t('knowledgeGraph.noRelations')}</p>
            </div>
          ) : (
            <div className="space-y-2">
              {relations.map(rel => (
                <div
                  key={rel.id}
                  className="card flex items-center gap-3"
                >
                  <span className="font-medium text-gray-900 dark:text-white">
                    {rel.subject?.name || '?'}
                  </span>
                  <span className="px-2 py-0.5 rounded bg-indigo-100 dark:bg-indigo-900/30 text-indigo-700 dark:text-indigo-300 text-sm font-medium">
                    {rel.predicate}
                  </span>
                  <span className="font-medium text-gray-900 dark:text-white">
                    {rel.object?.name || '?'}
                  </span>
                  <span className="ml-auto text-xs text-gray-400">{Math.round((rel.confidence || 0) * 100)}%</span>
                  <button
                    onClick={() => handleDeleteRelation(rel)}
                    className="p-1.5 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-800"
                    title={t('common.delete')}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Pagination */}
          {totalRelationsPages > 1 && (
            <div className="flex items-center justify-between mt-4">
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {relationsTotal} {t('knowledgeGraph.relations').toLowerCase()}
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setRelationsPage(p => Math.max(1, p - 1))}
                  disabled={relationsPage <= 1}
                  className="btn-secondary p-2 disabled:opacity-50"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <span className="text-sm text-gray-600 dark:text-gray-300">
                  {relationsPage} / {totalRelationsPages}
                </span>
                <button
                  onClick={() => setRelationsPage(p => Math.min(totalRelationsPages, p + 1))}
                  disabled={relationsPage >= totalRelationsPages}
                  className="btn-secondary p-2 disabled:opacity-50"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Stats Tab */}
      {activeTab === 'stats' && (
        <div>
          {loading || !stats ? (
            <div className="text-center py-12 text-gray-500 dark:text-gray-400">
              {t('common.loading')}
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {/* Entity count card */}
              <div className="card">
                <div className="flex items-center gap-3 mb-2">
                  <Brain className="w-5 h-5 text-indigo-500" />
                  <span className="text-sm font-medium text-gray-500 dark:text-gray-400">{t('knowledgeGraph.entities')}</span>
                </div>
                <p className="text-3xl font-bold text-gray-900 dark:text-white">{stats.entity_count}</p>
              </div>

              {/* Relation count card */}
              <div className="card">
                <div className="flex items-center gap-3 mb-2">
                  <Link2 className="w-5 h-5 text-indigo-500" />
                  <span className="text-sm font-medium text-gray-500 dark:text-gray-400">{t('knowledgeGraph.relations')}</span>
                </div>
                <p className="text-3xl font-bold text-gray-900 dark:text-white">{stats.relation_count}</p>
              </div>

              {/* Types card */}
              <div className="card">
                <div className="flex items-center gap-3 mb-2">
                  <BarChart3 className="w-5 h-5 text-indigo-500" />
                  <span className="text-sm font-medium text-gray-500 dark:text-gray-400">{t('knowledgeGraph.entityTypes')}</span>
                </div>
                <p className="text-3xl font-bold text-gray-900 dark:text-white">
                  {Object.keys(stats.entity_types || {}).length}
                </p>
              </div>

              {/* Type distribution */}
              <div className="card md:col-span-3">
                <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-4">{t('knowledgeGraph.typeDistribution')}</h3>
                <div className="space-y-3">
                  {Object.entries(stats.entity_types || {}).sort((a, b) => b[1] - a[1]).map(([type, count]) => (
                    <div key={type} className="flex items-center gap-3">
                      <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium min-w-[100px] justify-center ${TYPE_COLORS[type] || TYPE_COLORS.thing}`}>
                        {t(`knowledgeGraph.${type}`)}
                      </span>
                      <div className="flex-1 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-indigo-500 rounded-full transition-all"
                          style={{ width: `${stats.entity_count ? (count / stats.entity_count * 100) : 0}%` }}
                        />
                      </div>
                      <span className="text-sm font-medium text-gray-600 dark:text-gray-300 w-10 text-right">{count}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Edit Modal */}
      {showEditModal && (
        <Modal isOpen={showEditModal} onClose={() => setShowEditModal(false)} title={t('common.edit')}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('knowledgeGraph.entityName')}
              </label>
              <input
                type="text"
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                className="input w-full"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('knowledgeGraph.entityType')}
              </label>
              <select
                value={formType}
                onChange={(e) => setFormType(e.target.value)}
                className="input w-full"
              >
                {ENTITY_TYPES.map(type => (
                  <option key={type} value={type}>{t(`knowledgeGraph.${type}`)}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('common.description')}
              </label>
              <textarea
                value={formDescription}
                onChange={(e) => setFormDescription(e.target.value)}
                className="input w-full"
                rows={3}
              />
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setShowEditModal(false)} className="btn-secondary">
                {t('common.cancel')}
              </button>
              <button onClick={handleSaveEntity} className="btn-primary" disabled={!formName.trim()}>
                {t('common.save')}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
