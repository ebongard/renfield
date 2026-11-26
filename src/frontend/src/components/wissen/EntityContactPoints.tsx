import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Phone, Smartphone, Printer, Trash2 } from 'lucide-react';

import {
  CONTACT_POINT_ERROR_CODES,
  useContactPoints,
  useCreateContactPoint,
  useDeleteContactPoint,
  type ContactPoint,
  type ContactPointKind,
  type ContactPointLabel,
} from '../../api/resources/contactPoints';

const KIND_ICON = { phone: Phone, mobile: Smartphone, fax: Printer } as const;
const KINDS: ContactPointKind[] = ['phone', 'mobile', 'fax'];
const LABELS: ContactPointLabel[] = ['private', 'work'];

interface EntityContactPointsProps {
  entityId: number;
}

/** The backend sends a stable error code as `detail`; anything else falls back. */
function useErrorText(error: unknown, fallback: string | null): string | null {
  const { t } = useTranslation();
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  if (typeof detail === 'string' && CONTACT_POINT_ERROR_CODES.has(detail)) {
    return t(`contactPoints.errors.${detail}`);
  }
  return fallback;
}

/**
 * "Kontaktdaten" of a KG person/organization (#1240): the numbers stored as
 * attributes of the entity, with their source. The caller renders this only
 * while `contact_points_enabled` is on. A user who may not edit the entity gets
 * a 403 on add/delete, shown inline.
 */
export default function EntityContactPoints({ entityId }: EntityContactPointsProps) {
  const { t } = useTranslation();
  const formId = useId();
  const list = useContactPoints(entityId, true);
  const create = useCreateContactPoint();
  const remove = useDeleteContactPoint(entityId);
  const [kind, setKind] = useState<ContactPointKind>('mobile');
  const [label, setLabel] = useState<ContactPointLabel | ''>('');
  const [value, setValue] = useState('');

  const createError = useErrorText(create.error, create.errorMessage);
  const removeError = useErrorText(remove.error, remove.errorMessage);
  const points: ContactPoint[] = list.data ?? [];

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!value.trim()) return;
    create
      .mutateAsync({ entityId, kind, value: value.trim(), label: label || null })
      .then(() => setValue(''))
      // The error is rendered from create.error below; nothing else to do here.
      .catch(() => undefined);
  };

  return (
    <section
      className="pt-2 mt-2 border-t border-gray-100 dark:border-gray-700/60"
      aria-labelledby={`${formId}-title`}
      data-testid="entity-contact-points"
    >
      <h3 id={`${formId}-title`} className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
        {t('contactPoints.title')}
      </h3>

      {list.errorMessage && (
        <p className="text-sm text-red-600 dark:text-red-400" role="alert">{list.errorMessage}</p>
      )}
      {!list.isLoading && !list.errorMessage && points.length === 0 && (
        <p className="text-sm text-gray-500 dark:text-gray-400">{t('contactPoints.empty')}</p>
      )}

      {points.length > 0 && (
        <ul className="space-y-1 mb-2">
          {points.map((p) => {
            const Icon = KIND_ICON[p.kind] ?? Phone;
            return (
              <li key={p.id} className="flex items-center gap-2 text-sm" data-testid="contact-point">
                <Icon className="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                <span className="sr-only">{t(`contactPoints.kind.${p.kind}`)}</span>
                <a
                  href={p.kind === 'fax' ? undefined : `tel:${p.normalized}`}
                  className="text-gray-900 dark:text-white tabular-nums hover:text-primary-600 dark:hover:text-primary-400"
                >
                  {p.value}
                </a>
                {p.label && (
                  <span className="text-xs text-gray-500 dark:text-gray-400">{t(`contactPoints.label.${p.label}`)}</span>
                )}
                <span className="text-xs text-gray-400 dark:text-gray-500">{t(`contactPoints.source.${p.source_type}`, p.source_type)}</span>
                <button
                  type="button"
                  onClick={() => remove.mutate(p.id)}
                  disabled={remove.isPending}
                  className="ml-auto min-w-11 min-h-11 flex items-center justify-center rounded-md text-gray-400 hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50"
                  aria-label={t('contactPoints.remove', { value: p.value })}
                >
                  <Trash2 className="w-4 h-4" aria-hidden="true" />
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {removeError && <p className="text-xs text-red-600 dark:text-red-400" role="alert">{removeError}</p>}

      <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2" aria-label={t('contactPoints.add')}>
        <label className="flex flex-col text-xs text-gray-500 dark:text-gray-400">
          {t('contactPoints.kindLabel')}
          <select className="input py-1" value={kind} onChange={(e) => setKind(e.target.value as ContactPointKind)}>
            {KINDS.map((k) => <option key={k} value={k}>{t(`contactPoints.kind.${k}`)}</option>)}
          </select>
        </label>
        <label className="flex flex-col flex-1 min-w-[9rem] text-xs text-gray-500 dark:text-gray-400">
          {t('contactPoints.valueLabel')}
          <input
            className="input py-1"
            type="tel"
            inputMode="tel"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={t('contactPoints.valuePlaceholder')}
            maxLength={64}
          />
        </label>
        <label className="flex flex-col text-xs text-gray-500 dark:text-gray-400">
          {t('contactPoints.labelLabel')}
          <select className="input py-1" value={label} onChange={(e) => setLabel(e.target.value as ContactPointLabel | '')}>
            <option value="">{t('contactPoints.label.none')}</option>
            {LABELS.map((l) => <option key={l} value={l}>{t(`contactPoints.label.${l}`)}</option>)}
          </select>
        </label>
        <button type="submit" className="btn-primary min-h-11" disabled={create.isPending || !value.trim()}>
          {t('contactPoints.add')}
        </button>
      </form>
      {createError && <p className="mt-1 text-xs text-red-600 dark:text-red-400" role="alert">{createError}</p>}
    </section>
  );
}
