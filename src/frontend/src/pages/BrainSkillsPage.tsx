import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import AdminListPageShell from '../components/AdminListPageShell';
import SkillCard from '../components/SkillCard';
import SkillEditModal from '../components/SkillEditModal';
import {
  useDeleteSkill,
  usePinSkill,
  useSkillsQuery,
  useUnpinSkill,
  useUpdateSkill,
  type Skill,
} from '../api/resources/skills';

/**
 * Owner self-view at /brain/skills. Shows the current user's approved
 * skills (plus public seeds) — the corpus they can browse, edit, pin to
 * protect from the curator, or delete. Drafts are NOT shown here: the
 * draft-gate is the admin's responsibility, and the owner sees only what
 * the system has actually committed to using on their behalf.
 */
export default function BrainSkillsPage() {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<Skill | null>(null);

  const skillsQuery = useSkillsQuery({ status: 'approved', include_seeds: true });
  const update = useUpdateSkill();
  const del = useDeleteSkill();
  const pin = usePinSkill();
  const unpin = useUnpinSkill();

  const skills = skillsQuery.data ?? [];
  const busy = update.isPending || del.isPending || pin.isPending || unpin.isPending;

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

  const handleDelete = (id: number) => {
    if (window.confirm(t('selfLearning.skills.confirmDelete'))) {
      del.mutate(id);
    }
  };

  const handleTogglePin = (skill: Skill) => {
    if (skill.pinned) {
      unpin.mutate(skill.id);
    } else {
      pin.mutate(skill.id);
    }
  };

  return (
    <>
      <AdminListPageShell
        title={t('selfLearning.skills.brainTitle')}
        description={t('selfLearning.skills.brainDescription')}
        isLoading={skillsQuery.isLoading}
        errorMessage={skillsQuery.errorMessage}
        itemCount={skills.length}
        emptyState={t('selfLearning.skills.brainEmpty')}
        testId="brain-skills-page"
      >
        <div className="grid gap-3" data-testid="skills-list">
          {skills.map((skill) => (
            <SkillCard
              key={skill.id}
              skill={skill}
              showOwnerActions={skill.is_owner}
              onEdit={skill.is_owner ? setEditing : undefined}
              onDelete={skill.is_owner ? handleDelete : undefined}
              onTogglePin={skill.is_owner ? handleTogglePin : undefined}
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
