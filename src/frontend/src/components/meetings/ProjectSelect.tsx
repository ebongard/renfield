import { useTranslation } from 'react-i18next';

import type { Project } from '../../api/resources/projects';

/** Shared project dropdown — "— no project —" clears the link. Renders nothing
 *  when there are no projects (household / projects-off). */
export default function ProjectSelect({
  projects, value, onChange, disabled, ariaLabel, className = 'input',
}: {
  projects: Project[];
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
  ariaLabel: string;
  className?: string;
}) {
  const { t } = useTranslation();
  if (projects.length === 0) return null;
  return (
    <select
      className={className}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      disabled={disabled}
      aria-label={ariaLabel}
    >
      <option value="">{t('meetings.noProject')}</option>
      {projects.map((p) => (
        <option key={p.id} value={p.id}>{p.name}</option>
      ))}
    </select>
  );
}
