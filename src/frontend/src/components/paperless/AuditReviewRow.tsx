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
import { useEffect, useId, useRef, useState } from 'react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { Check, X, Loader, ChevronDown, ChevronRight, Plus, Trash2, Pencil, Calendar, ChevronsUpDown } from 'lucide-react';

import Badge from '../Badge';
import {
  useUpdateReview,
  type AuditResult,
  type EditableField,
  type ReviewOverrides,
  type TaxonomyResponse,
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
  /** Register this row's latest in-flight save so the page can flush it before
   *  apply (covers both single-row and bulk approve). */
  onRegisterPending: (id: number, save: Promise<unknown>) => void;
  /** Paperless taxonomy for the lookup fields (correspondents/types/tags/paths). */
  taxonomy?: TaxonomyResponse;
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
  onRegisterPending,
  taxonomy,
  colSpan,
}: AuditReviewRowProps) {
  const { t } = useTranslation();
  const updateReview = useUpdateReview();
  const [draft, setDraft] = useState<Draft>(() => initDraft(r));
  const [expanded, setExpanded] = useState(false);
  // draftRef mirrors `draft` so event handlers read the latest state without a
  // stale closure (two interactions before a re-render can't clobber each other).
  const draftRef = useRef<Draft>(draft);
  // Serialize saves so out-of-order network delivery can't leave an older payload
  // as the last server write; the tail settles when the row is fully persisted.
  const chainRef = useRef<Promise<unknown>>(Promise.resolve());

  // Re-seed the draft when the SERVER data changes (e.g. an apply/skip of another
  // row invalidates the query and this still-pending row refetches with fresh
  // suggested_* / persisted overlay). Keyed on a signature of the server fields —
  // our own edits don't invalidate, so this never clobbers an in-progress draft.
  const serverSig = JSON.stringify([
    r.current_title, r.suggested_title, r.current_correspondent, r.suggested_correspondent,
    r.current_document_type, r.suggested_document_type, r.current_date, r.suggested_date,
    r.current_storage_path, r.suggested_storage_path, r.current_tags, r.suggested_tags,
    r.current_custom_fields, r.suggested_custom_fields, r.user_overrides, r.field_selection,
  ]);
  useEffect(() => {
    const seeded = initDraft(r);
    draftRef.current = seeded;
    setDraft(seeded);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSig]);

  const effScalar = (f: ScalarField): string =>
    (draft.overrides[f] as string | undefined) ?? scalarSuggested(r, f);
  const effTags = (): string[] => (draft.overrides.tags ?? r.suggested_tags ?? []) as string[];
  const effCustom = (): Record<string, unknown> =>
    (draft.overrides.custom_fields ?? r.suggested_custom_fields ?? {}) as Record<string, unknown>;

  // A field APPLIES iff its effective value differs from current (matches the
  // backend `eff != current` guard). An override equal to current is a no-op, so
  // it's not applicable — no stuck disabled checkbox.
  const isChanged = (f: EditableField): boolean => {
    if (f === 'tags') return effTags().length > 0 && !tagsEqual(effTags(), r.current_tags ?? []);
    if (f === 'custom_fields')
      return Object.keys(effCustom()).length > 0 && !objEqual(effCustom(), r.current_custom_fields ?? {});
    const v = effScalar(f as ScalarField);
    return !!v && v !== scalarCurrent(r, f as ScalarField);
  };
  const isOverridden = (f: EditableField): boolean => f in draft.overrides;
  const isApplicable = (f: EditableField): boolean => isChanged(f);

  const persist = (next: Draft) => {
    const run = () =>
      updateReview.mutateAsync({
        id: r.id,
        overrides: next.overrides,
        field_selection: [...next.selection],
      });
    // Chain after the previous save settles (run on both fulfil and reject so a
    // transient failure doesn't wedge the queue). The mutation surfaces its own
    // error toast; the tail never rejects so the page's flush can't throw.
    const tail = chainRef.current.then(run, run).catch(() => undefined);
    chainRef.current = tail;
    onRegisterPending(r.id, tail);
  };

  const commit = (next: Draft) => {
    draftRef.current = next;
    setDraft(next);
    persist(next);
  };

  // --- scalar editing (click-to-edit commits the final value) ---
  const onScalarSave = (f: ScalarField, raw: string) => {
    const d = draftRef.current;
    const overrides = { ...d.overrides };
    const selection = new Set(d.selection);
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
    const d = draftRef.current;
    const selection = new Set(d.selection);
    if (selection.has(f)) selection.delete(f);
    else selection.add(f);
    commit({ ...d, selection });
  };

  // --- tags editing ---
  const setTags = (tags: string[]) => {
    const d = draftRef.current;
    const overrides = { ...d.overrides };
    const selection = new Set(d.selection);
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
    const d = draftRef.current;
    const overrides = { ...d.overrides };
    const selection = new Set(d.selection);
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
            current={scalarCurrent(r, 'title')}
            value={effScalar('title')}
            applicable={isApplicable('title')}
            selected={draft.selection.has('title')}
            overridden={isOverridden('title')}
            onSave={(v) => onScalarSave('title', v)}
            onToggle={() => onToggleField('title')}
            t={t}
          />
          <TagsEditor
            tags={effTags()}
            current={r.current_tags ?? []}
            applicable={isApplicable('tags')}
            selected={draft.selection.has('tags')}
            options={taxonomy?.tags ?? []}
            onChange={setTags}
            onToggle={() => onToggleField('tags')}
            t={t}
          />
        </td>

        <td className="py-3 px-2">
          <EditableScalar
            current={scalarCurrent(r, 'correspondent')} value={effScalar('correspondent')}
            applicable={isApplicable('correspondent')} selected={draft.selection.has('correspondent')}
            overridden={isOverridden('correspondent')} options={taxonomy?.correspondents ?? []}
            onSave={(v) => onScalarSave('correspondent', v)}
            onToggle={() => onToggleField('correspondent')} t={t}
          />
        </td>
        <td className="py-3 px-2">
          <EditableScalar
            current={scalarCurrent(r, 'document_type')} value={effScalar('document_type')}
            applicable={isApplicable('document_type')} selected={draft.selection.has('document_type')}
            overridden={isOverridden('document_type')} options={taxonomy?.document_types ?? []}
            onSave={(v) => onScalarSave('document_type', v)}
            onToggle={() => onToggleField('document_type')} t={t}
          />
        </td>
        <td className="py-3 px-2">
          <EditableScalar
            current={scalarCurrent(r, 'date')} value={effScalar('date')} placeholder={t('paperlessAudit.review.datePlaceholder')}
            applicable={isApplicable('date')} selected={draft.selection.has('date')}
            overridden={isOverridden('date')} type="date"
            onSave={(v) => onScalarSave('date', v)}
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
            current={scalarCurrent(r, 'storage_path')} value={effScalar('storage_path')}
            applicable={isApplicable('storage_path')} selected={draft.selection.has('storage_path')}
            overridden={isOverridden('storage_path')} options={taxonomy?.storage_paths ?? []}
            onSave={(v) => onScalarSave('storage_path', v)}
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
              onClick={() => onApprove([r.id])}
              disabled={actionLoading}
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

// Jira-style inline edit: reads as plain text; hover shows a subtle bg + an
// affordance icon; click swaps to an editor; Enter/blur commits, Escape cancels.
//  - options → a native <datalist> lookup: pick an existing Paperless value or
//    type a new one to create it (create happens server-side on apply).
//  - type='date' → a native calendar picker (auto-opened via showPicker()).
// Native controls are used deliberately: they escape the table's overflow-x-auto
// clipping that an absolute dropdown would hit, and are keyboard/i18n-accessible.
interface InlineEditProps {
  value: string;
  current?: string;
  placeholder?: string;
  ariaLabel: string;
  type?: 'text' | 'date';
  options?: string[];
  onSave: (value: string) => void;
}
function InlineEdit({ value, current, placeholder, ariaLabel, type = 'text', options, onSave }: InlineEditProps) {
  const [editing, setEditing] = useState(false);
  const [buffer, setBuffer] = useState(value);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  useEffect(() => {
    if (!editing) return;
    inputRef.current?.focus();
    if (type === 'date') {
      // Open the calendar immediately (supported in modern browsers).
      inputRef.current?.showPicker?.();
    } else {
      inputRef.current?.select();
    }
  }, [editing, type]);

  const commit = () => {
    setEditing(false);
    if (buffer !== value) onSave(buffer);
  };
  const cancel = () => {
    setEditing(false);
    setBuffer(value);
  };

  if (editing) {
    return (
      <>
        <input
          ref={inputRef}
          type={type === 'date' ? 'date' : 'text'}
          list={options ? listId : undefined}
          value={buffer}
          placeholder={placeholder}
          aria-label={ariaLabel}
          onChange={(e) => setBuffer(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); commit(); }
            else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
          }}
          className="input py-1 px-1.5 text-xs w-full"
        />
        {options && (
          <datalist id={listId}>
            {options.map((o) => <option key={o} value={o} />)}
          </datalist>
        )}
      </>
    );
  }
  const Icon = type === 'date' ? Calendar : options ? ChevronsUpDown : Pencil;
  return (
    <button
      type="button"
      onClick={() => { setBuffer(value); setEditing(true); }}
      aria-label={ariaLabel}
      className="group/edit -mx-1.5 flex w-full items-center gap-1 rounded px-1.5 py-1 text-left transition-colors duration-75 hover:bg-gray-100 dark:hover:bg-gray-700/50"
    >
      <span className="min-w-0 flex-1 space-y-0.5">
        {current && current !== value && (
          <span className="block truncate text-[11px] text-red-500 line-through dark:text-red-400">{current}</span>
        )}
        <span className={`block truncate text-xs ${value ? 'text-gray-900 dark:text-gray-100' : 'italic text-gray-400 dark:text-gray-500'}`}>
          {value || placeholder || '—'}
        </span>
      </span>
      <Icon className="w-3 h-3 shrink-0 text-gray-400 opacity-0 transition-opacity group-hover/edit:opacity-60" />
    </button>
  );
}

interface EditableScalarProps {
  current: string;
  value: string;
  applicable: boolean;
  selected: boolean;
  overridden: boolean;
  placeholder?: string;
  type?: 'text' | 'date';
  options?: string[];
  onSave: (v: string) => void;
  onToggle: () => void;
  t: TFunction;
}
function EditableScalar({ current, value, applicable, selected, overridden, placeholder, type, options, onSave, onToggle, t }: EditableScalarProps) {
  return (
    <div className="flex items-start gap-1.5">
      <SelectBox applicable={applicable} selected={selected} overridden={overridden} onToggle={onToggle} label={t('paperlessAudit.review.applyField')} />
      <div className="min-w-0 flex-1">
        <InlineEdit
          value={value}
          current={current}
          placeholder={placeholder}
          type={type}
          options={options}
          ariaLabel={t('paperlessAudit.review.editField')}
          onSave={onSave}
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
  options: string[];
  onChange: (tags: string[]) => void;
  onToggle: () => void;
  t: TFunction;
}
function TagsEditor({ tags, current, applicable, selected, options, onChange, onToggle, t }: TagsEditorProps) {
  const [adding, setAdding] = useState(false);
  const [entry, setEntry] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();
  useEffect(() => { if (adding) inputRef.current?.focus(); }, [adding]);
  const add = () => {
    const v = entry.trim();
    if (v && !tags.includes(v)) onChange([...tags, v]);
    setEntry('');
    setAdding(false);
  };
  const suggestions = options.filter((o) => !tags.includes(o));
  return (
    <div className="mt-1.5 flex items-start gap-1.5">
      <SelectBox applicable={applicable} selected={selected} onToggle={onToggle} label={t('paperlessAudit.review.applyField')} />
      <div className="min-w-0 flex-1 flex flex-wrap items-center gap-1">
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
        {adding ? (
          <>
            <input
              ref={inputRef}
              type="text"
              list={listId}
              value={entry}
              onChange={(e) => setEntry(e.target.value)}
              onBlur={add}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); add(); }
                else if (e.key === 'Escape') { e.preventDefault(); setEntry(''); setAdding(false); }
              }}
              placeholder={t('paperlessAudit.review.addTag')}
              className="input py-0.5 px-1.5 text-xs w-28"
            />
            <datalist id={listId}>
              {suggestions.map((o) => <option key={o} value={o} />)}
            </datalist>
          </>
        ) : (
          <button
            onClick={() => setAdding(true)}
            className="inline-flex items-center gap-0.5 rounded px-1 py-0.5 text-[11px] text-gray-400 transition-colors duration-75 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700/50 dark:hover:text-gray-300"
          >
            <Plus className="w-3 h-3" /> {t('paperlessAudit.review.addTag')}
          </button>
        )}
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
// Preserve JSON types (number/bool/null) round-tripping through a text input;
// anything not valid JSON stays a plain string — so a numeric custom field
// edited to "5" saves as 5, not "5" (no silent type corruption).
function coerceCfValue(s: string): unknown {
  const trimmed = s.trim();
  if (trimmed === '') return s;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (typeof parsed === 'number' || typeof parsed === 'boolean' || parsed === null) return parsed;
  } catch {
    /* not JSON → keep the raw string */
  }
  return s;
}

function CustomFieldsEditor({ value, applicable, selected, onChange, onToggle, t }: CustomFieldsEditorProps) {
  const [k, setK] = useState('');
  const [v, setV] = useState('');
  const entries = Object.entries(value);
  const addPair = () => {
    const key = k.trim();
    if (!key) return;
    onChange({ ...value, [key]: coerceCfValue(v) });
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
              <span className="font-mono text-gray-500 dark:text-gray-400 w-24 shrink-0 truncate">{key}</span>
              <div className="w-48">
                <InlineEdit
                  value={typeof val === 'string' ? val : JSON.stringify(val)}
                  ariaLabel={t('paperlessAudit.review.editField')}
                  onSave={(nv) => onChange({ ...value, [key]: coerceCfValue(nv) })}
                />
              </div>
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
