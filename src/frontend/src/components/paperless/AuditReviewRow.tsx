/**
 * One editable row in the Paperless-audit review queue.
 *
 * Two capabilities layered on the read-only diff:
 *  1. per-field APPLY SELECTION — a checkbox on every applicable field
 *     (suggested-changed or manually edited); checked ⟺ the field will apply.
 *  2. manual EDIT — every suggested value is an inline input (auto-save on blur).
 *
 * State lives here as a per-row draft (overrides + selection) and is persisted
 * via PATCH .../results/{id}. Overrides always apply server-side (an edit is an
 * explicit intent), so field_selection only needs to carry the fields the user
 * wants applied; untouched rows never PATCH and keep the legacy apply-all path.
 */
import { useMemo, useRef, useState } from 'react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { Check, X, Loader, ChevronDown, ChevronRight, Plus, Trash2 } from 'lucide-react';

import Badge from '../Badge';
import {
  useUpdateReview,
  type AuditResult,
  type EditableField,
  type ReviewOverrides,
} from '../../api/resources/paperlessAudit';
import ConfidenceBadge from './ConfidenceBadge';

const SCALAR_FIELDS = ['title', 'correspondent', 'document_type', 'date', 'storage_path'] as const;
type ScalarField = (typeof SCALAR_FIELDS)[number];

interface Draft {
  overrides: ReviewOverrides;
  selection: Set<EditableField>;
}

// --- value accessors -------------------------------------------------------

function scalarCurrent(r: AuditResult, f: ScalarField): string {
  const map: Record<ScalarField, string | null | undefined> = {
    title: r.current_title,
    correspondent: r.current_correspondent,
    document_type: r.current_document_type,
    date: r.current_date,
    storage_path: r.current_storage_path,
  };
  return map[f] ?? '';
}

function scalarSuggested(r: AuditResult, f: ScalarField): string {
  const map: Record<ScalarField, string | null | undefined> = {
    title: r.suggested_title,
    correspondent: r.suggested_correspondent,
    document_type: r.suggested_document_type,
    date: r.suggested_date,
    storage_path: r.suggested_storage_path,
  };
  return map[f] ?? '';
}

function tagsEqual(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

function objEqual(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const ak = Object.keys(a).sort();
  const bk = Object.keys(b).sort();
  if (ak.length !== bk.length || ak.some((k, i) => k !== bk[i])) return false;
  return ak.every((k) => JSON.stringify(a[k]) === JSON.stringify(b[k]));
}

// --- draft init ------------------------------------------------------------

function initDraft(r: AuditResult): Draft {
  const overrides: ReviewOverrides = { ...(r.user_overrides ?? {}) };
  // Which fields "change" (would apply) given the current overrides.
  const changed = new Set<EditableField>();
  for (const f of SCALAR_FIELDS) {
    const eff = (overrides[f] as string | undefined) ?? scalarSuggested(r, f);
    if (eff && eff !== scalarCurrent(r, f)) changed.add(f);
  }
  const effTags = (overrides.tags ?? r.suggested_tags ?? []) as string[];
  if (effTags.length && !tagsEqual(effTags, r.current_tags ?? [])) changed.add('tags');
  const effCf = (overrides.custom_fields ?? r.suggested_custom_fields ?? {}) as Record<string, unknown>;
  if (Object.keys(effCf).length && !objEqual(effCf, r.current_custom_fields ?? {})) changed.add('custom_fields');

  // Persisted field_selection wins; else default to all changed fields.
  const selection = r.field_selection ? new Set<EditableField>(r.field_selection) : changed;
  return { overrides, selection };
}

interface AuditReviewRowProps {
  result: AuditResult;
  isBulkSelected: boolean;
  onToggleBulkSelected: (id: number) => void;
  onApprove: (ids: number[]) => void | Promise<void>;
  onSkip: (ids: number[]) => void;
  actionLoading: boolean;
  /** Total column count so the custom-fields drawer can span the full row. */
  colSpan: number;
}

export default function AuditReviewRow({
  result: r,
  isBulkSelected,
  onToggleBulkSelected,
  onApprove,
  onSkip,
  actionLoading,
  colSpan,
}: AuditReviewRowProps) {
  const { t } = useTranslation();
  const updateReview = useUpdateReview();
  const [draft, setDraft] = useState<Draft>(() => initDraft(r));
  const [expanded, setExpanded] = useState(false);
  const pendingRef = useRef<Promise<unknown> | null>(null);

  const effScalar = (f: ScalarField): string =>
    (draft.overrides[f] as string | undefined) ?? scalarSuggested(r, f);
  const effTags = (): string[] => (draft.overrides.tags ?? r.suggested_tags ?? []) as string[];
  const effCustom = (): Record<string, unknown> =>
    (draft.overrides.custom_fields ?? r.suggested_custom_fields ?? {}) as Record<string, unknown>;

  const isChanged = (f: EditableField): boolean => {
    if (f === 'tags') return effTags().length > 0 && !tagsEqual(effTags(), r.current_tags ?? []);
    if (f === 'custom_fields')
      return Object.keys(effCustom()).length > 0 && !objEqual(effCustom(), r.current_custom_fields ?? {});
    const v = effScalar(f as ScalarField);
    return !!v && v !== scalarCurrent(r, f as ScalarField);
  };
  const isOverridden = (f: EditableField): boolean => f in draft.overrides;
  const isApplicable = (f: EditableField): boolean => isChanged(f) || isOverridden(f);

  const persist = (next: Draft) => {
    const p = updateReview.mutateAsync({
      id: r.id,
      overrides: next.overrides,
      field_selection: [...next.selection],
    });
    pendingRef.current = p;
    // Swallow — the mutation surfaces its own error toast; a failed save must not
    // reject the row's approve flow below.
    p.catch(() => undefined);
  };

  const commit = (next: Draft) => {
    setDraft(next);
    persist(next);
  };

  // --- scalar editing ---
  const onScalarChange = (f: ScalarField, value: string) => {
    setDraft((d) => ({ ...d, overrides: { ...d.overrides, [f]: value } }));
  };
  const onScalarBlur = (f: ScalarField) => {
    const raw = (draft.overrides[f] as string | undefined) ?? '';
    const overrides = { ...draft.overrides };
    const selection = new Set(draft.selection);
    if (raw.trim() === '' || raw === scalarSuggested(r, f)) {
      // Empty or back-to-suggestion → not an override (clearing isn't supported).
      delete overrides[f];
    } else {
      overrides[f] = raw;
    }
    const effNext = (overrides[f] as string | undefined) ?? scalarSuggested(r, f);
    const nextChanged = !!effNext && effNext !== scalarCurrent(r, f);
    if (nextChanged) selection.add(f);
    else selection.delete(f);
    commit({ overrides, selection });
  };

  // --- selection toggle (checkbox) ---
  const onToggleField = (f: EditableField) => {
    const selection = new Set(draft.selection);
    if (selection.has(f)) selection.delete(f);
    else selection.add(f);
    commit({ ...draft, selection });
  };

  // --- tags editing ---
  const setTags = (tags: string[]) => {
    const overrides = { ...draft.overrides };
    const selection = new Set(draft.selection);
    if (tags.length === 0 || tagsEqual(tags, r.suggested_tags ?? [])) {
      delete overrides.tags; // revert to suggestion / can't clear
    } else {
      overrides.tags = tags;
    }
    const eff = (overrides.tags ?? r.suggested_tags ?? []) as string[];
    if (eff.length && !tagsEqual(eff, r.current_tags ?? [])) selection.add('tags');
    else selection.delete('tags');
    commit({ overrides, selection });
  };

  // --- custom_fields editing ---
  const setCustom = (obj: Record<string, unknown>) => {
    const overrides = { ...draft.overrides };
    const selection = new Set(draft.selection);
    if (Object.keys(obj).length === 0 || objEqual(obj, r.suggested_custom_fields ?? {})) {
      delete overrides.custom_fields;
    } else {
      overrides.custom_fields = obj;
    }
    const eff = (overrides.custom_fields ?? r.suggested_custom_fields ?? {}) as Record<string, unknown>;
    if (Object.keys(eff).length && !objEqual(eff, r.current_custom_fields ?? {})) selection.add('custom_fields');
    else selection.delete('custom_fields');
    commit({ overrides, selection });
  };

  const approve = async () => {
    // Flush any in-flight save so the persisted overlay reflects the latest edit
    // before apply reads it off the row.
    if (pendingRef.current) await pendingRef.current;
    await onApprove([r.id]);
  };

  const editedCount = useMemo(
    () => [...draft.selection].filter((f) => isApplicable(f)).length,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [draft],
  );

  return (
    <>
      <tr className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50 align-top">
        <td className="py-3 px-2">
          <input
            type="checkbox"
            checked={isBulkSelected}
            onChange={() => onToggleBulkSelected(r.id)}
            className="rounded border-gray-300 dark:border-gray-600"
            aria-label={t('paperlessAudit.review.selectRow')}
          />
        </td>
        <td className="py-3 px-2 text-gray-900 dark:text-gray-100 font-mono text-xs">{r.paperless_doc_id}</td>

        {/* title (+ tags editor) */}
        <td className="py-3 px-2 max-w-xs">
          <EditableScalar
            field="title"
            current={scalarCurrent(r, 'title')}
            value={effScalar('title')}
            applicable={isApplicable('title')}
            selected={draft.selection.has('title')}
            overridden={isOverridden('title')}
            onChange={(v) => onScalarChange('title', v)}
            onBlur={() => onScalarBlur('title')}
            onToggle={() => onToggleField('title')}
            t={t}
          />
          <TagsEditor
            tags={effTags()}
            current={r.current_tags ?? []}
            applicable={isApplicable('tags')}
            selected={draft.selection.has('tags')}
            onChange={setTags}
            onToggle={() => onToggleField('tags')}
            t={t}
          />
        </td>

        <td className="py-3 px-2">
          <EditableScalar
            field="correspondent" current={scalarCurrent(r, 'correspondent')} value={effScalar('correspondent')}
            applicable={isApplicable('correspondent')} selected={draft.selection.has('correspondent')}
            overridden={isOverridden('correspondent')}
            onChange={(v) => onScalarChange('correspondent', v)} onBlur={() => onScalarBlur('correspondent')}
            onToggle={() => onToggleField('correspondent')} t={t}
          />
        </td>
        <td className="py-3 px-2">
          <EditableScalar
            field="document_type" current={scalarCurrent(r, 'document_type')} value={effScalar('document_type')}
            applicable={isApplicable('document_type')} selected={draft.selection.has('document_type')}
            overridden={isOverridden('document_type')}
            onChange={(v) => onScalarChange('document_type', v)} onBlur={() => onScalarBlur('document_type')}
            onToggle={() => onToggleField('document_type')} t={t}
          />
        </td>
        <td className="py-3 px-2">
          <EditableScalar
            field="date" current={scalarCurrent(r, 'date')} value={effScalar('date')} placeholder="YYYY-MM-DD"
            applicable={isApplicable('date')} selected={draft.selection.has('date')}
            overridden={isOverridden('date')}
            onChange={(v) => onScalarChange('date', v)} onBlur={() => onScalarBlur('date')}
            onToggle={() => onToggleField('date')} t={t}
          />
        </td>
        <td className="py-3 px-2">
          {r.detected_language && (
            <Badge color="blue" className="font-mono">{r.detected_language}</Badge>
          )}
        </td>
        <td className="py-3 px-2">
          <EditableScalar
            field="storage_path" current={scalarCurrent(r, 'storage_path')} value={effScalar('storage_path')}
            applicable={isApplicable('storage_path')} selected={draft.selection.has('storage_path')}
            overridden={isOverridden('storage_path')}
            onChange={(v) => onScalarChange('storage_path', v)} onBlur={() => onScalarBlur('storage_path')}
            onToggle={() => onToggleField('storage_path')} t={t}
          />
        </td>
        <td className="py-3 px-2">
          {r.missing_fields && r.missing_fields.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {r.missing_fields.map((f, i) => (
                <Badge key={i} color="yellow">{f}</Badge>
              ))}
            </div>
          )}
        </td>
        <td className="py-3 px-2"><ConfidenceBadge value={r.confidence} /></td>
        <td className="py-3 px-2 text-right">
          <div className="flex items-center justify-end gap-1">
            <button
              onClick={() => setExpanded((x) => !x)}
              className="btn-icon btn-icon-ghost"
              title={t('paperlessAudit.review.customFields')}
              aria-expanded={expanded}
            >
              {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </button>
            <button
              onClick={approve}
              disabled={actionLoading || editedCount === 0}
              className="btn-icon text-green-600 dark:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/20 disabled:opacity-40"
              title={t('paperlessAudit.review.approve')}
            >
              {actionLoading ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            </button>
            <button
              onClick={() => onSkip([r.id])}
              disabled={actionLoading}
              className="btn-icon btn-icon-ghost"
              title={t('paperlessAudit.review.skip')}
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-gray-50 dark:bg-gray-800/40 border-b border-gray-100 dark:border-gray-800">
          <td colSpan={colSpan} className="py-3 px-6">
            <CustomFieldsEditor
              value={effCustom()}
              applicable={isApplicable('custom_fields')}
              selected={draft.selection.has('custom_fields')}
              onChange={setCustom}
              onToggle={() => onToggleField('custom_fields')}
              t={t}
            />
          </td>
        </tr>
      )}
    </>
  );
}

// --- field sub-components ---------------------------------------------------

interface SelectBoxProps {
  applicable: boolean;
  selected: boolean;
  overridden?: boolean;
  onToggle: () => void;
  label: string;
}
function SelectBox({ applicable, selected, overridden, onToggle, label }: SelectBoxProps) {
  if (!applicable) return null;
  return (
    <input
      type="checkbox"
      checked={selected}
      disabled={overridden}
      onChange={onToggle}
      title={overridden ? label : undefined}
      aria-label={label}
      className="mt-1 rounded border-gray-300 dark:border-gray-600 disabled:opacity-60"
    />
  );
}

interface EditableScalarProps {
  field: EditableField;
  current: string;
  value: string;
  applicable: boolean;
  selected: boolean;
  overridden: boolean;
  placeholder?: string;
  onChange: (v: string) => void;
  onBlur: () => void;
  onToggle: () => void;
  t: TFunction;
}
function EditableScalar({ current, value, applicable, selected, overridden, placeholder, onChange, onBlur, onToggle, t }: EditableScalarProps) {
  return (
    <div className="flex items-start gap-1.5">
      <SelectBox applicable={applicable} selected={selected} overridden={overridden} onToggle={onToggle} label={t('paperlessAudit.review.applyField')} />
      <div className="min-w-0 flex-1 space-y-0.5">
        {current && current !== value && (
          <div className="text-red-600 dark:text-red-400 line-through text-xs truncate">{current}</div>
        )}
        <input
          type="text"
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onBlur}
          className="input py-1 px-1.5 text-xs w-full"
        />
      </div>
    </div>
  );
}

interface TagsEditorProps {
  tags: string[];
  current: string[];
  applicable: boolean;
  selected: boolean;
  onChange: (tags: string[]) => void;
  onToggle: () => void;
  t: TFunction;
}
function TagsEditor({ tags, current, applicable, selected, onChange, onToggle, t }: TagsEditorProps) {
  const [entry, setEntry] = useState('');
  const add = () => {
    const v = entry.trim();
    if (v && !tags.includes(v)) onChange([...tags, v]);
    setEntry('');
  };
  return (
    <div className="mt-1.5 flex items-start gap-1.5">
      <SelectBox applicable={applicable} selected={selected} onToggle={onToggle} label={t('paperlessAudit.review.applyField')} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap gap-1">
          {tags.map((tag) => (
            <Badge key={tag} color="accent" className="inline-flex items-center gap-1">
              {tag}
              <button onClick={() => onChange(tags.filter((x) => x !== tag))} className="hover:text-red-500" aria-label={t('common.remove')}>
                <X className="w-3 h-3" />
              </button>
            </Badge>
          ))}
          {current.length > 0 && !tagsEqual(tags, current) && (
            <span className="text-red-500 dark:text-red-400 line-through text-[10px] self-center">{current.join(', ')}</span>
          )}
        </div>
        <div className="mt-1 flex items-center gap-1">
          <input
            type="text"
            value={entry}
            onChange={(e) => setEntry(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }}
            placeholder={t('paperlessAudit.review.addTag')}
            className="input py-0.5 px-1.5 text-xs w-24"
          />
          <button onClick={add} className="btn-icon btn-icon-ghost" aria-label={t('paperlessAudit.review.addTag')}>
            <Plus className="w-3 h-3" />
          </button>
        </div>
      </div>
    </div>
  );
}

interface CustomFieldsEditorProps {
  value: Record<string, unknown>;
  applicable: boolean;
  selected: boolean;
  onChange: (obj: Record<string, unknown>) => void;
  onToggle: () => void;
  t: TFunction;
}
function CustomFieldsEditor({ value, applicable, selected, onChange, onToggle, t }: CustomFieldsEditorProps) {
  const [k, setK] = useState('');
  const [v, setV] = useState('');
  const entries = Object.entries(value);
  const addPair = () => {
    const key = k.trim();
    if (!key) return;
    onChange({ ...value, [key]: v });
    setK('');
    setV('');
  };
  const removeKey = (key: string) => {
    const next = { ...value };
    delete next[key];
    onChange(next);
  };
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <SelectBox applicable={applicable} selected={selected} onToggle={onToggle} label={t('paperlessAudit.review.applyField')} />
        <span className="text-xs font-medium text-gray-600 dark:text-gray-300">{t('paperlessAudit.review.customFields')}</span>
      </div>
      {entries.length > 0 && (
        <div className="space-y-1">
          {entries.map(([key, val]) => (
            <div key={key} className="flex items-center gap-2 text-xs">
              <span className="font-mono text-gray-500 dark:text-gray-400">{key}</span>
              <input
                type="text"
                value={typeof val === 'string' ? val : JSON.stringify(val)}
                onChange={(e) => onChange({ ...value, [key]: e.target.value })}
                className="input py-0.5 px-1.5 text-xs w-48"
              />
              <button onClick={() => removeKey(key)} className="btn-icon btn-icon-ghost" aria-label={t('common.remove')}>
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="flex items-center gap-1">
        <input type="text" value={k} onChange={(e) => setK(e.target.value)} placeholder={t('paperlessAudit.review.cfKey')} className="input py-0.5 px-1.5 text-xs w-32" />
        <input type="text" value={v} onChange={(e) => setV(e.target.value)} placeholder={t('paperlessAudit.review.cfValue')} className="input py-0.5 px-1.5 text-xs w-48" />
        <button onClick={addPair} className="btn-icon btn-icon-ghost" aria-label={t('paperlessAudit.review.cfAdd')}>
          <Plus className="w-3 h-3" />
        </button>
      </div>
    </div>
  );
}
