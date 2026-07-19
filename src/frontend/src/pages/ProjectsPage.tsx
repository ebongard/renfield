import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { FolderKanban, FileText, Loader, XCircle, Plus, ChevronRight } from 'lucide-react';

import PageHeader from '../components/PageHeader';
import { formatDate } from '../utils/datetime';
import TierBadge from '../components/TierBadge';
import { useProjectsQuery, useCreateProject } from '../api/resources/projects';

export default function ProjectsPage() {
  const { t } = useTranslation();
  const projectsQuery = useProjectsQuery();
  const createProject = useCreateProject();
  const projects = projectsQuery.data ?? [];

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || createProject.isPending) return;
    try {
      await createProject.mutateAsync({ name: trimmed, description: description.trim() || null });
      setName('');
      setDescription('');
    } catch {
      // Error is surfaced via createProject.errorMessage; keep the form filled.
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        icon={FolderKanban}
        title={t('projects.title')}
        subtitle={t('projects.subtitle')}
      />

      {/* Create form */}
      <form onSubmit={handleSubmit} className="card space-y-3">
        <h2 className="text-base font-semibold text-gray-900 dark:text-white">
          {t('projects.createTitle')}
        </h2>
        <input
          className="input"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t('projects.namePlaceholder')}
          aria-label={t('projects.namePlaceholder')}
          maxLength={255}
        />
        <textarea
          className="input"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={t('projects.descriptionPlaceholder')}
          aria-label={t('projects.descriptionPlaceholder')}
          rows={2}
        />
        {createProject.errorMessage && (
          <p className="text-sm text-red-600 dark:text-red-400">{createProject.errorMessage}</p>
        )}
        <button
          type="submit"
          className="btn-primary inline-flex items-center gap-2"
          disabled={!name.trim() || createProject.isPending}
        >
          {createProject.isPending ? (
            <Loader className="w-4 h-4 animate-spin" />
          ) : (
            <Plus className="w-4 h-4" />
          )}
          {createProject.isPending ? t('projects.creating') : t('projects.create')}
        </button>
      </form>

      {/* List */}
      <div className="space-y-4">
        {projectsQuery.isLoading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('projects.loading')}</p>
          </div>
        ) : projectsQuery.errorMessage ? (
          <div className="card text-center py-12">
            <XCircle className="w-12 h-12 mx-auto text-red-500 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{projectsQuery.errorMessage}</p>
          </div>
        ) : projects.length === 0 ? (
          <div className="card text-center py-12">
            <FolderKanban className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{t('projects.noProjects')}</p>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('projects.noProjectsDesc')}</p>
          </div>
        ) : (
          projects.map((project) => (
            <Link
              key={project.id}
              to={`/projects/${project.id}`}
              className="card block hover:border-primary-300 dark:hover:border-primary-700 transition-colors"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <h3 className="text-base font-semibold text-gray-900 dark:text-white mb-1 truncate">
                    {project.name}
                  </h3>
                  {project.description && (
                    <p className="text-sm text-gray-500 dark:text-gray-400 mb-2">
                      {project.description}
                    </p>
                  )}
                  <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
                    <span className="inline-flex items-center gap-1">
                      <FileText className="w-3.5 h-3.5" />
                      {t('projects.documentCount', { count: project.document_count })}
                    </span>
                    <span>{t('projects.created')}: {formatDate(project.created_at)}</span>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <TierBadge tier={project.circle_tier} />
                  <ChevronRight className="w-4 h-4 text-gray-400" />
                </div>
              </div>
            </Link>
          ))
        )}
      </div>
    </div>
  );
}
