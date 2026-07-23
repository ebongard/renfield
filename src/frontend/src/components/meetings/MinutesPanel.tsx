import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Loader, CheckCircle, Check, Trash2, X, ClipboardList, Sparkles, Plus, RefreshCw, Pencil,
} from 'lucide-react';

import {
  useMinutes, useGenerateMinutes, useUpdateMinutes, useConfirmMinutes, useDeleteMinutes,
  type MinutesBody,
} from '../../api/resources/meetings';

export function emptyMinutes(): MinutesBody {
  return { summary: '', decisions: [], action_items: [] };
}

/** §2 Phase 3 minutes: generate DRAFT → edit → confirm (renders into the
 *  transcript document). Gated by the caller on meeting_minutes_enabled. */
export default function MinutesPanel({ meetingId }: { meetingId: number }) {
  const { t } = useTranslation();
  const minutesQuery = useMinutes(meetingId, true);
  const generate = useGenerateMinutes();
  const update = useUpdateMinutes();
  const confirm = useConfirmMinutes();
  const discard = useDeleteMinutes();

  const status = minutesQuery.data?.minutes_status ?? 'none';
  const serverBody = minutesQuery.data?.minutes ?? null;

  // Local editable draft, seeded from the server body. A `draft` meeting is
  // always editing; a `confirmed` one is read-only until the user taps Edit.
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState<MinutesBody>(emptyMinutes());

  // Reseed whenever the server body CONTENT changes (generate / save / confirm /
  // reload). We key on the serialized value, not the object reference, so a
  // referentially-new-but-equal query result doesn't clobber in-progress edits.
  const serverKey = JSON.stringify(serverBody);
  useEffect(() => {
    setBody(serverBody ?? emptyMinutes());
    setEditing(false);
    // serverBody is intentionally read through serverKey (content identity).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);

  const isBusy =
    generate.isPending || update.isPending || confirm.isPending || discard.isPending;
  const errorMessage =
    generate.errorMessage || update.errorMessage || confirm.errorMessage ||
    discard.errorMessage || minutesQuery.errorMessage;

  const patch = (next: Partial<MinutesBody>) => setBody((b) => ({ ...b, ...next }));

  // Dirty = the on-screen body diverges from the last server-saved body. Key
  // order is stable (backend + emptyMinutes + patch all preserve it), so a
  // serialized compare is reliable; a rare false-positive only costs one extra PUT.
  const dirty = JSON.stringify(body) !== serverKey;

  const onGenerate = () => { if (!isBusy) generate.mutate(meetingId); };
  const onSave = () => { if (!isBusy) update.mutate({ meetingId, body }); };
  // Confirm must persist live edits FIRST — otherwise the backend confirms the
  // last-saved draft and the reseed effect silently discards the user's edits
  // (the "generate → tweak → Confirm" data-loss path). PUT reverts to draft, so
  // the immediately-following confirm (which requires draft) still succeeds.
  const onConfirm = async () => {
    if (isBusy) return;
    try {
      if (dirty) await update.mutateAsync({ meetingId, body });
      await confirm.mutateAsync(meetingId);
    } catch {
      // Surfaced via update/confirm.errorMessage; state stays editable.
    }
  };
  const onDiscard = () => { if (!isBusy) discard.mutate(meetingId); };

  const header = (
    <h4 className="flex items-center gap-1.5 text-sm font-semibold text-gray-900 dark:text-white">
      <ClipboardList className="w-4 h-4" aria-hidden="true" />
      {t('meetings.minutes.title')}
    </h4>
  );

  if (minutesQuery.isLoading) {
    return (
      <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
        {header}
        <div className="py-3 text-center">
          <Loader className="w-5 h-5 animate-spin mx-auto text-gray-400" />
        </div>
      </div>
    );
  }

  const showForm = status === 'draft' || editing;

  return (
    <div className="border-t border-gray-200 dark:border-gray-700 pt-3 space-y-3">
      <div className="flex items-center justify-between gap-2">
        {header}
        {status === 'confirmed' && !editing && (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-green-600 dark:text-green-400">
            <CheckCircle className="w-3.5 h-3.5" aria-hidden="true" />
            {t('meetings.minutes.confirmedBadge')}
          </span>
        )}
      </div>

      {status === 'none' ? (
        <div className="space-y-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">{t('meetings.minutes.noneHint')}</p>
          <button
            type="button"
            className="btn-primary inline-flex items-center gap-2"
            onClick={onGenerate}
            disabled={isBusy}
          >
            {generate.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
            {t('meetings.minutes.generate')}
          </button>
        </div>
      ) : showForm ? (
        <MinutesForm body={body} onChange={patch} disabled={isBusy} />
      ) : (
        <MinutesReadonly body={body} />
      )}

      {status !== 'none' && (
        <div className="flex flex-wrap items-center gap-2">
          {showForm ? (
            <>
              <button
                type="button"
                className="btn-secondary inline-flex items-center gap-1.5"
                onClick={onSave}
                disabled={isBusy}
              >
                {update.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                {t('meetings.minutes.save')}
              </button>
              {status === 'draft' && (
                <button
                  type="button"
                  className="btn-primary inline-flex items-center gap-1.5"
                  onClick={onConfirm}
                  disabled={isBusy}
                >
                  {confirm.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
                  {t('meetings.minutes.confirm')}
                </button>
              )}
              <button
                type="button"
                className="btn-secondary inline-flex items-center gap-1.5"
                onClick={onGenerate}
                disabled={isBusy}
                title={t('meetings.minutes.regenerateHint')}
              >
                <RefreshCw className="w-4 h-4" />
                {t('meetings.minutes.regenerate')}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="btn-secondary inline-flex items-center gap-1.5"
              onClick={() => setEditing(true)}
              disabled={isBusy}
            >
              <Pencil className="w-4 h-4" />
              {t('meetings.minutes.edit')}
            </button>
          )}
          <button
            type="button"
            className="ml-auto p-1.5 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700"
            onClick={onDiscard}
            disabled={isBusy}
            aria-label={t('meetings.minutes.discard')}
            title={t('meetings.minutes.discard')}
          >
            {discard.isPending ? <Loader className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
          </button>
        </div>
      )}

      {status === 'confirmed' && !editing && (
        <p className="text-xs text-gray-500 dark:text-gray-400">{t('meetings.minutes.confirmedHint')}</p>
      )}
      {errorMessage && <p className="text-sm text-red-600 dark:text-red-400">{errorMessage}</p>}
    </div>
  );
}

/** Read-only rendering of confirmed minutes. */
function MinutesReadonly({ body }: { body: MinutesBody }) {
  const { t } = useTranslation();
  const empty = !body.summary && body.decisions.length === 0 && body.action_items.length === 0;
  if (empty) {
    return <p className="text-sm text-gray-500 dark:text-gray-400">{t('meetings.minutes.emptyBody')}</p>;
  }
  return (
    <div className="space-y-3 text-sm">
      {body.summary && (
        <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{body.summary}</p>
      )}
      {body.decisions.length > 0 && (
        <div>
          <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
            {t('meetings.minutes.decisions')}
          </h5>
          <ul className="list-disc list-inside space-y-0.5 text-gray-700 dark:text-gray-300">
            {body.decisions.map((d, i) => (
              <li key={i}>
                {d.text}
                {d.made_by && <span className="text-gray-500 dark:text-gray-400"> — {d.made_by}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {body.action_items.length > 0 && (
        <div>
          <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
            {t('meetings.minutes.actionItems')}
          </h5>
          <ul className="list-disc list-inside space-y-0.5 text-gray-700 dark:text-gray-300">
            {body.action_items.map((a, i) => (
              <li key={i}>
                {a.text}
                {a.owner && <span className="text-gray-500 dark:text-gray-400"> — {a.owner}</span>}
                {a.due_hint && <span className="text-gray-500 dark:text-gray-400"> ({a.due_hint})</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** Editable draft form for the minutes body. */
function MinutesForm({
  body, onChange, disabled,
}: {
  body: MinutesBody;
  onChange: (next: Partial<MinutesBody>) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();

  const setDecision = (i: number, field: 'text' | 'made_by', value: string) => {
    const decisions = body.decisions.map((d, j) => (j === i ? { ...d, [field]: value } : d));
    onChange({ decisions });
  };
  const addDecision = () => onChange({ decisions: [...body.decisions, { text: '', made_by: '' }] });
  const removeDecision = (i: number) => onChange({ decisions: body.decisions.filter((_, j) => j !== i) });

  const setAction = (i: number, field: 'text' | 'owner' | 'due_hint', value: string) => {
    const action_items = body.action_items.map((a, j) => (j === i ? { ...a, [field]: value } : a));
    onChange({ action_items });
  };
  const addAction = () =>
    onChange({ action_items: [...body.action_items, { text: '', owner: '', due_hint: '' }] });
  const removeAction = (i: number) =>
    onChange({ action_items: body.action_items.filter((_, j) => j !== i) });

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
          {t('meetings.minutes.summary')}
        </label>
        <textarea
          className="input"
          value={body.summary}
          onChange={(e) => onChange({ summary: e.target.value })}
          placeholder={t('meetings.minutes.summaryPlaceholder')}
          aria-label={t('meetings.minutes.summary')}
          rows={3}
          maxLength={4000}
          disabled={disabled}
        />
      </div>

      <div className="space-y-2">
        <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
          {t('meetings.minutes.decisions')}
        </h5>
        {body.decisions.map((d, i) => (
          <div key={i} className="flex items-start gap-2">
            <input
              className="input flex-1"
              type="text"
              value={d.text}
              onChange={(e) => setDecision(i, 'text', e.target.value)}
              placeholder={t('meetings.minutes.decisionPlaceholder')}
              aria-label={t('meetings.minutes.decisionAria', { n: i + 1 })}
              maxLength={1000}
              disabled={disabled}
            />
            <input
              className="input w-32 shrink-0"
              type="text"
              value={d.made_by}
              onChange={(e) => setDecision(i, 'made_by', e.target.value)}
              placeholder={t('meetings.minutes.byPlaceholder')}
              aria-label={t('meetings.minutes.madeByAria', { n: i + 1 })}
              maxLength={200}
              disabled={disabled}
            />
            <button
              type="button"
              className="p-2 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700 shrink-0"
              onClick={() => removeDecision(i)}
              disabled={disabled}
              aria-label={t('meetings.minutes.removeDecision', { n: i + 1 })}
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        ))}
        <button
          type="button"
          className="inline-flex items-center gap-1 text-sm text-primary-600 dark:text-primary-400 hover:underline"
          onClick={addDecision}
          disabled={disabled}
        >
          <Plus className="w-3.5 h-3.5" />
          {t('meetings.minutes.addDecision')}
        </button>
      </div>

      <div className="space-y-2">
        <h5 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
          {t('meetings.minutes.actionItems')}
        </h5>
        {body.action_items.map((a, i) => (
          <div key={i} className="flex items-start gap-2">
            <input
              className="input flex-1"
              type="text"
              value={a.text}
              onChange={(e) => setAction(i, 'text', e.target.value)}
              placeholder={t('meetings.minutes.actionPlaceholder')}
              aria-label={t('meetings.minutes.actionAria', { n: i + 1 })}
              maxLength={1000}
              disabled={disabled}
            />
            <input
              className="input w-28 shrink-0"
              type="text"
              value={a.owner}
              onChange={(e) => setAction(i, 'owner', e.target.value)}
              placeholder={t('meetings.minutes.ownerPlaceholder')}
              aria-label={t('meetings.minutes.ownerAria', { n: i + 1 })}
              maxLength={200}
              disabled={disabled}
            />
            <input
              className="input w-28 shrink-0"
              type="text"
              value={a.due_hint}
              onChange={(e) => setAction(i, 'due_hint', e.target.value)}
              placeholder={t('meetings.minutes.duePlaceholder')}
              aria-label={t('meetings.minutes.dueAria', { n: i + 1 })}
              maxLength={200}
              disabled={disabled}
            />
            <button
              type="button"
              className="p-2 rounded text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700 shrink-0"
              onClick={() => removeAction(i)}
              disabled={disabled}
              aria-label={t('meetings.minutes.removeAction', { n: i + 1 })}
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        ))}
        <button
          type="button"
          className="inline-flex items-center gap-1 text-sm text-primary-600 dark:text-primary-400 hover:underline"
          onClick={addAction}
          disabled={disabled}
        >
          <Plus className="w-3.5 h-3.5" />
          {t('meetings.minutes.addAction')}
        </button>
      </div>
    </div>
  );
}
