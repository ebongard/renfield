import { useState, useEffect, lazy, Suspense } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Brain, Link2, BarChart3, Search, Trash2, Edit3, Merge, X,
  ChevronLeft, ChevronRight, ArrowRight, Lock, Users, Plus, GitBranch,
} from 'lucide-react';
import apiClient from '../utils/axios';
import { extractApiError } from '../utils/axios';
import Modal from '../components/Modal';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import type { BadgeColor } from '../components/Badge';
import { useConfirmDialog } from '../components/ConfirmDialog';
import { useTheme } from '../context/ThemeContext';

const GraphView = lazy(() => import('../components/knowledge-graph/GraphView'));

type EntityType = 'person' | 'place' | 'organization' | 'thing' | 'event' | 'concept';
type Tab = 'entities' | 'relations' | 'stats' | 'graph';

const ENTITY_TYPES: EntityType[] = ['person', 'place', 'organization', 'thing', 'event', 'concept'];

const TYPE_BADGE_COLORS: Record<EntityType, BadgeColor> = {
  person: 'blue',
  place: 'green',
  organization: 'purple',
  thing: 'amber',
  event: 'pink',
  concept: 'teal',
};

const TABS: Tab[] = ['entities', 'relations', 'stats', 'graph'];

interface KgEntity {
  id: number;
  name: string;
  entity_type: EntityType;
  description?: string | null;
  circle_tier?: number;
  mention_count?: number;
  last_seen_at?: string;
}

interface KgEntityRef {
  id: number;
  name: string;
  entity_type?: EntityType;
}

interface KgRelation {
  id: number;
  predicate: string;
  confidence?: number;
  subject?: KgEntityRef;
  object?: KgEntityRef;
}

interface CircleTierInfo {
  tier: number;
  name: string;
  label: string;
  description?: string;
}

interface KgStats {
  entity_count?: number;
  relation_count?: number;
  entity_types?: Record<string, number>;
  top_entities?: KgEntity[];
}

export default function KnowledgeGraphPage() {
  const { t } = useTranslation();
  const { theme } = useTheme();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [activeTab, setActiveTab] = useState<Tab>('entities');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Entities state
  const [entities, setEntities] = useState<KgEntity[]>([]);
  const [entitiesTotal, setEntitiesTotal] = useState(0);
  const [entitiesPage, setEntitiesPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState<EntityType | ''>('');
  const [tierFilter, setTierFilter] = useState<string>('all');
  const [availableTiers, setAvailableTiers] = useState<CircleTierInfo[]>([]);
  const [tierMenuEntity, setTierMenuEntity] = useState<KgEntity | null>(null);

  // Relations state
  const [relations, setRelations] = useState<KgRelation[]>([]);
  const [relationsTotal, setRelationsTotal] = useState(0);
  const [relationsPage, setRelationsPage] = useState(1);
  const [entityFilter, setEntityFilter] = useState('');

  // Stats state
  const [stats, setStats] = useState<KgStats | null>(null);

  // Edit modal
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingEntity, setEditingEntity] = useState<KgEntity | null>(null);
  const [formName, setFormName] = useState('');
  const [formType, setFormType] = useState<EntityType>('thing');
  const [formDescription, setFormDescription] = useState('');

  // Merge state
  const [mergeMode, setMergeMode] = useState(false);
  const [mergeSelection, setMergeSelection] = useState<KgEntity[]>([]);

  // Relation edit/create modal
  const [showRelationModal, setShowRelationModal] = useState(false);
  const [editingRelation, setEditingRelation] = useState<KgRelation | null>(null);
  const [relFormPredicate, setRelFormPredicate] = useState('');
  const [relFormConfidence, setRelFormConfidence] = useState(0.8);
  const [relFormSubjectId, setRelFormSubjectId] = useState<number | ''>('');
  const [relFormObjectId, setRelFormObjectId] = useState<number | ''>('');
  const [relFormSubjectSearch, setRelFormSubjectSearch] = useState('');
  const [relFormObjectSearch, setRelFormObjectSearch] = useState('');
  const [subjectResults, setSubjectResults] = useState<KgEntity[]>([]);
  const [objectResults, setObjectResults] = useState<KgEntity[]>([]);
  const [subjectLabel, setSubjectLabel] = useState('');
  const [objectLabel, setObjectLabel] = useState('');

  const PAGE_SIZE = 50;

  // Auto-clear messages
  useEffect(() => {
    if (error || success) {
      const timer = setTimeout(() => { setError(null); setSuccess(null); }, 5000);
      return () => clearTimeout(timer);
    }
  }, [error, success]);

  // Load circle tiers
  const loadTiers = async () => {
    try {
      const response = await apiClient.get<{ tiers: CircleTierInfo[] }>('/api/knowledge-graph/circle-tiers', {
        params: { lang: t('lang') === 'de' ? 'de' : 'en' },
      });
      setAvailableTiers(response.data.tiers);
    } catch (err) {
      console.error('Failed to load circle tiers:', err);
      setAvailableTiers([
        { tier: 0, name: 'self', label: t('knowledgeGraph.personal'), description: '' },
        { tier: 2, name: 'household', label: t('knowledgeGraph.family'), description: '' },
        { tier: 4, name: 'public', label: t('knowledgeGraph.public'), description: '' },
      ]);
    }
  };

  useEffect(() => {
    loadTiers();
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
      if (tierFilter && tierFilter !== 'all') params.set('circle_tier', tierFilter);

      const response = await apiClient.get<{ entities: KgEntity[]; total: number }>(`/api/knowledge-graph/entities?${params}`);
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

      const response = await apiClient.get<{ relations: KgRelation[]; total: number }>(`/api/knowledge-graph/relations?${params}`);
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
      const response = await apiClient.get<KgStats>('/api/knowledge-graph/stats');
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
  }, [activeTab, entitiesPage, relationsPage, typeFilter, searchQuery, tierFilter, entityFilter]);

  // Edit entity
  const openEditModal = (entity: KgEntity) => {
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
  const handleDeleteEntity = async (entity: KgEntity) => {
    const confirmed = await confirm({
      message: t('knowledgeGraph.deleteConfirm', { name: entity.name }),
      variant: 'danger',
    });
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
  const handleDeleteRelation = async (relation: KgRelation) => {
    const confirmed = await confirm({
      message: t('knowledgeGraph.deleteRelationConfirm'),
      variant: 'danger',
    });
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
  const toggleMergeSelection = (entity: KgEntity) => {
    setMergeSelection((prev) => {
      const exists = prev.find((e) => e.id === entity.id);
      if (exists) return prev.filter((e) => e.id !== entity.id);
      if (prev.length >= 2) return [prev[1], entity];
      return [...prev, entity];
    });
  };

  const handleMerge = async () => {
    if (mergeSelection.length !== 2) return;
    const confirmed = await confirm({
      message: t('knowledgeGraph.mergeConfirm', {
        source: mergeSelection[0].name,
        target: mergeSelection[1].name,
      }),
      variant: 'warning',
    });
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

  // Update entity circle_tier
  const handleUpdateCircleTier = async (entity: KgEntity, newTier: number) => {
    try {
      await apiClient.patch(
        `/api/knowledge-graph/entities/${entity.id}/circle-tier`,
        { circle_tier: newTier },
      );

      const tierInfo = availableTiers.find((info) => info.tier === newTier);
      setSuccess(
        t('knowledgeGraph.scopeUpdated', { name: entity.name, scope: tierInfo?.label || String(newTier) })
      );
      setTierMenuEntity(null);
      loadEntities();
    } catch (err) {
      setError(t('common.error'));
    }
  };

  // Filter relations by entity
  const showRelationsForEntity = (entityId: number) => {
    setEntityFilter(String(entityId));
    setRelationsPage(1);
    setActiveTab('relations');
  };

  // Entity search for relation modal
  const searchEntities = async (query: string, setter: (results: KgEntity[]) => void) => {
    if (!query || query.length < 1) { setter([]); return; }
    try {
      const response = await apiClient.get<{ entities: KgEntity[] }>('/api/knowledge-graph/entities', {
        params: { search: query, size: 10 },
      });
      setter(response.data.entities || []);
    } catch {
      setter([]);
    }
  };

  useEffect(() => {
    const timer = setTimeout(() => searchEntities(relFormSubjectSearch, setSubjectResults), 300);
    return () => clearTimeout(timer);
  }, [relFormSubjectSearch]);

  useEffect(() => {
    const timer = setTimeout(() => searchEntities(relFormObjectSearch, setObjectResults), 300);
    return () => clearTimeout(timer);
  }, [relFormObjectSearch]);

  // Open relation edit modal
  const openRelationEditModal = (rel: KgRelation) => {
    setEditingRelation(rel);
    setRelFormPredicate(rel.predicate);
    setRelFormConfidence(rel.confidence || 0.8);
    setRelFormSubjectId(rel.subject?.id ?? '');
    setRelFormObjectId(rel.object?.id ?? '');
    setSubjectLabel(rel.subject?.name || '');
    setObjectLabel(rel.object?.name || '');
    setRelFormSubjectSearch('');
    setRelFormObjectSearch('');
    setSubjectResults([]);
    setObjectResults([]);
    setShowRelationModal(true);
  };

  // Open relation create modal
  const openRelationCreateModal = () => {
    setEditingRelation(null);
    setRelFormPredicate('');
    setRelFormConfidence(0.8);
    setRelFormSubjectId('');
    setRelFormObjectId('');
    setSubjectLabel('');
    setObjectLabel('');
    setRelFormSubjectSearch('');
    setRelFormObjectSearch('');
    setSubjectResults([]);
    setObjectResults([]);
    setShowRelationModal(true);
  };

  // Save relation (create or update)
  const handleSaveRelation = async () => {
    if (!relFormPredicate.trim() || !relFormSubjectId || !relFormObjectId) return;
    if (String(relFormSubjectId) === String(relFormObjectId)) {
      setError(t('knowledgeGraph.selfLinkError'));
      return;
    }
    try {
      if (editingRelation) {
        await apiClient.put(`/api/knowledge-graph/relations/${editingRelation.id}`, {
          predicate: relFormPredicate,
          confidence: relFormConfidence,
          subject_id: Number(relFormSubjectId),
          object_id: Number(relFormObjectId),
        });
      } else {
        await apiClient.post('/api/knowledge-graph/relations', {
          subject_id: Number(relFormSubjectId),
          predicate: relFormPredicate,
          object_id: Number(relFormObjectId),
          confidence: relFormConfidence,
        });
      }
      setShowRelationModal(false);
      setSuccess(t('common.success'));
      loadRelations();
    } catch (err) {
      setError(extractApiError(err, t('common.error')));
    }
  };

  const totalEntitiesPages = Math.ceil(entitiesTotal / PAGE_SIZE);
  const totalRelationsPages = Math.ceil(relationsTotal / PAGE_SIZE);

  return (
    <div className="space-y-6">
      {ConfirmDialogComponent}

      {/* Header */}
      <PageHeader icon={Brain} title={t('knowledgeGraph.title')} subtitle={t('knowledgeGraph.subtitle')} />

      {/* Messages */}
      {error && <Alert variant="error">{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

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
            {tab === 'graph' && <GitBranch className="w-4 h-4" />}
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
              onChange={(e) => { setTypeFilter(e.target.value as EntityType | ''); setEntitiesPage(1); }}
              className="input w-auto"
            >
              <option value="">{t('common.all')}</option>
              {ENTITY_TYPES.map(type => (
                <option key={type} value={type}>{t(`knowledgeGraph.${type}`)}</option>
              ))}
            </select>

            <select
              value={tierFilter}
              onChange={(e) => { setTierFilter(e.target.value); setEntitiesPage(1); }}
              className="input w-auto"
            >
              <option value="all">{t('common.all')}</option>
              {availableTiers.map((tier) => (
                <option key={tier.tier} value={String(tier.tier)}>{tier.label}</option>
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
                            <Badge color={TYPE_BADGE_COLORS[entity.entity_type] || 'amber'}>
                              {t(`knowledgeGraph.${entity.entity_type}`)}
                            </Badge>
                            {entity.circle_tier !== undefined && entity.circle_tier !== 0 && (
                              <Badge color="green">
                                {availableTiers.find(tier => tier.tier === entity.circle_tier)?.label || `tier ${entity.circle_tier}`}
                              </Badge>
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
                                    setTierMenuEntity(tierMenuEntity?.id === entity.id ? null : entity);
                                  }}
                                  className={`btn-icon ${
                                    (entity.circle_tier || 0) === 0 ? 'btn-icon-ghost' :
                                    'text-green-600 dark:text-green-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg'
                                  }`}
                                  title={t('knowledgeGraph.changeScope')}
                                >
                                  {(entity.circle_tier || 0) === 0 ? <Lock className="w-4 h-4" /> : <Users className="w-4 h-4" />}
                                </button>

                                {tierMenuEntity?.id === entity.id && (
                                  <div className="absolute right-0 mt-1 w-48 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-10">
                                    {availableTiers.map((tierInfo) => (
                                      <button
                                        key={tierInfo.tier}
                                        onClick={() => handleUpdateCircleTier(entity, tierInfo.tier)}
                                        className={`w-full px-4 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 ${
                                          (entity.circle_tier || 0) === tierInfo.tier ? 'font-semibold' : ''
                                        }`}
                                        title={tierInfo.description}
                                      >
                                        {tierInfo.label}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>
                              <button
                                onClick={() => showRelationsForEntity(entity.id)}
                                className="btn-icon btn-icon-ghost"
                                title={t('knowledgeGraph.showRelations')}
                              >
                                <Link2 className="w-4 h-4" />
                              </button>
                              <button
                                onClick={() => openEditModal(entity)}
                                className="btn-icon btn-icon-ghost"
                                title={t('common.edit')}
                              >
                                <Edit3 className="w-4 h-4" />
                              </button>
                              <button
                                onClick={() => handleDeleteEntity(entity)}
                                className="btn-icon btn-icon-danger"
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
          {/* Controls */}
          <div className="flex items-center justify-end mb-4">
            <button
              onClick={openRelationCreateModal}
              className="btn-primary flex items-center gap-2 text-sm"
            >
              <Plus className="w-4 h-4" />
              {t('knowledgeGraph.createRelation')}
            </button>
          </div>

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
                    onClick={() => openRelationEditModal(rel)}
                    className="btn-icon btn-icon-ghost"
                    title={t('common.edit')}
                  >
                    <Edit3 className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => handleDeleteRelation(rel)}
                    className="btn-icon btn-icon-danger"
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
                      <Badge color={TYPE_BADGE_COLORS[type as EntityType] || 'amber'} className="min-w-[100px] justify-center">
                        {t(`knowledgeGraph.${type}`)}
                      </Badge>
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

      {/* Graph Tab */}
      {activeTab === 'graph' && (
        <Suspense fallback={
          <div className="flex items-center justify-center h-96">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500" />
          </div>
        }>
          <GraphView
            isDark={theme === 'dark' || (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)}
            onEntityClick={(entityId) => {
              const entity = entities.find(e => e.id === entityId);
              if (entity) {
                setEditingEntity(entity);
                setFormName(entity.name);
                setFormType(entity.entity_type);
                setFormDescription(entity.description || '');
                setShowEditModal(true);
              }
            }}
            onSwitchToEntities={() => setActiveTab('entities')}
          />
        </Suspense>
      )}

      {/* Entity Edit Modal */}
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
                onChange={(e) => setFormType(e.target.value as EntityType)}
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

      {/* Relation Edit/Create Modal */}
      {showRelationModal && (
        <Modal
          isOpen={showRelationModal}
          onClose={() => setShowRelationModal(false)}
          title={editingRelation ? t('knowledgeGraph.editRelation') : t('knowledgeGraph.createRelation')}
        >
          <div className="space-y-4">
            {/* Subject */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('knowledgeGraph.subject')}
              </label>
              {subjectLabel && (
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-gray-900 dark:text-white">{subjectLabel}</span>
                  <button
                    onClick={() => { setSubjectLabel(''); setRelFormSubjectId(''); }}
                    className="text-gray-400 hover:text-red-500"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              )}
              {!subjectLabel && (
                <div className="relative">
                  <input
                    type="text"
                    value={relFormSubjectSearch}
                    onChange={(e) => setRelFormSubjectSearch(e.target.value)}
                    placeholder={t('knowledgeGraph.selectEntity')}
                    className="input w-full"
                  />
                  {subjectResults.length > 0 && (
                    <div className="absolute z-10 w-full mt-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-40 overflow-y-auto">
                      {subjectResults.map(e => (
                        <button
                          key={e.id}
                          onClick={() => {
                            setRelFormSubjectId(e.id);
                            setSubjectLabel(e.name);
                            setRelFormSubjectSearch('');
                            setSubjectResults([]);
                          }}
                          className="w-full px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2"
                        >
                          <Badge color={TYPE_BADGE_COLORS[e.entity_type] || 'amber'}>
                            {e.entity_type}
                          </Badge>
                          {e.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Predicate */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('knowledgeGraph.predicateLabel')}
              </label>
              <input
                type="text"
                value={relFormPredicate}
                onChange={(e) => setRelFormPredicate(e.target.value)}
                placeholder="e.g. lives_in, works_at, knows"
                className="input w-full"
              />
            </div>

            {/* Object */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('knowledgeGraph.object')}
              </label>
              {objectLabel && (
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-gray-900 dark:text-white">{objectLabel}</span>
                  <button
                    onClick={() => { setObjectLabel(''); setRelFormObjectId(''); }}
                    className="text-gray-400 hover:text-red-500"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              )}
              {!objectLabel && (
                <div className="relative">
                  <input
                    type="text"
                    value={relFormObjectSearch}
                    onChange={(e) => setRelFormObjectSearch(e.target.value)}
                    placeholder={t('knowledgeGraph.selectEntity')}
                    className="input w-full"
                  />
                  {objectResults.length > 0 && (
                    <div className="absolute z-10 w-full mt-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-40 overflow-y-auto">
                      {objectResults.map(e => (
                        <button
                          key={e.id}
                          onClick={() => {
                            setRelFormObjectId(e.id);
                            setObjectLabel(e.name);
                            setRelFormObjectSearch('');
                            setObjectResults([]);
                          }}
                          className="w-full px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2"
                        >
                          <Badge color={TYPE_BADGE_COLORS[e.entity_type] || 'amber'}>
                            {e.entity_type}
                          </Badge>
                          {e.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Confidence */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('knowledgeGraph.confidenceLabel')} ({Math.round(relFormConfidence * 100)}%)
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={relFormConfidence}
                onChange={(e) => setRelFormConfidence(parseFloat(e.target.value))}
                className="w-full"
              />
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setShowRelationModal(false)} className="btn-secondary">
                {t('common.cancel')}
              </button>
              <button
                onClick={handleSaveRelation}
                className="btn-primary"
                disabled={!relFormPredicate.trim() || !relFormSubjectId || !relFormObjectId}
              >
                {t('common.save')}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
