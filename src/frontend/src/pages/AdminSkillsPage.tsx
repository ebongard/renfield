import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import AdminListPageShell from '../components/AdminListPageShell';
import SkillCard from '../components/SkillCard';
import SkillEditModal from '../components/SkillEditModal';
import {
  useApproveSkill,
  useRejectSkill,
  useSkillsQuery,
  useUpdateSkill,
  type Skill,
  type SkillStatus,
} from '../api/resources/skills';

const STATUS_FILTERS: SkillStatus[] = ['draft', 'approved', 'rejected', 'archived'];

/**
 * Admin Skills Inbox — surfaces every draft skill across all users
 * (admin_view=true) so the household admin can approve or reject the
 * agent's auto-extracted recipes before they enter the live retrieval
 * corpus. The same page also supports the other status filters so an
 * admin can audit the existing approved corpus or revisit rejected
 * skills without leaving the page.
 */
export default function AdminSkillsPage() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<SkillStatus>('draft');
  const [editing, setEditing] = useState<Skill | null>(null);

  const filters = useMemo(
    () => ({ status, admin_view: true, include_seeds: false, limit: 200 }),
    [status],
  );

  const skillsQuery = useSkillsQuery(filters);
  const approve = useApproveSkill();
  const reject = useRejectSkill();
  const update = useUpdateSkill();

  const skills = skillsQuery.data ?? [];
  const busy = approve.isPending || reject.isPending || update.isPending;

  const handleSave = async (input: {
    id: number;
    title: string;
    body_md: string;
    trigger_examples: string[];
    tool_sequence: string[];
  }) => {
    await update.mutateAsync(input);
    setEditing(null);
  };

  const toolbar = (
    <>
      <span className="text-sm text-gray-600 dark:text-gray-400">
        {t('selfLearning.skills.filter.status')}:
      </span>
      {STATUS_FILTERS.map((s) => (
        <button
          key={s}
          type="button"
          className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
            status === s
              ? 'bg-primary-600 text-white'
              : 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
          }`}
          onClick={() => setStatus(s)}
          aria-pressed={status === s}
          data-testid={`status-filter-${s}`}
        >
          {t(`selfLearning.skills.status.${s}`)}
        </button>
      ))}
      <span className="ml-auto text-xs text-gray-500 dark:text-gray-400">
        {t('selfLearning.skills.totalCount', { count: skills.length })}
      </span>
    </>
  );

  return (
    <>
      <AdminListPageShell
        title={t('selfLearning.skills.adminTitle')}
        description={t('selfLearning.skills.adminDescription')}
        toolbar={toolbar}
        isLoading={skillsQuery.isLoading}
        errorMessage={skillsQuery.errorMessage || approve.errorMessage || reject.errorMessage}
        itemCount={skills.length}
        emptyState={t('selfLearning.skills.empty', { status: t(`selfLearning.skills.status.${status}`) })}
        testId="admin-skills-page"
      >
        <div className="grid gap-3" data-testid="skills-list">
          {skills.map((skill) => (
            <SkillCard
              key={skill.id}
              skill={skill}
              showAdminActions
              onApprove={(id) => approve.mutate(id)}
              onReject={(id) => reject.mutate(id)}
              onEdit={setEditing}
              isBusy={busy}
            />
          ))}
        </div>
      </AdminListPageShell>

      <SkillEditModal
        skill={editing}
        isOpen={editing !== null}
        onClose={() => setEditing(null)}
        onSave={handleSave}
        isPending={update.isPending}
        errorMessage={update.errorMessage}
      />
    </>
  );
}
