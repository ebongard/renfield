/**
 * Scheduled Tasks (Geplante Aufgaben) — admin page for the backend scheduler.
 *
 * Lists every interval/cron task, shows its next/last run + status, and lets an
 * admin toggle, run-now, edit, create (custom) and delete (non-builtin) tasks.
 */
import { useState, useEffect, Fragment } from 'react';
import type { FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Clock, Plus, RefreshCw, Play, Pencil, Trash2, Loader, Lock, Power, ChevronDown,
} from 'lucide-react';

import PageHeader from '../components/PageHeader';
import Modal from '../components/Modal';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import type { BadgeColor } from '../components/Badge';
import { useConfirmDialog } from '../components/ConfirmDialog';
import { extractApiError } from '../utils/axios';
import { formatDateTime } from '../utils/datetime';
import {
  useScheduledTasks,
  useScheduledTaskRuns,
  useCreateScheduledTask,
  useUpdateScheduledTask,
  useRunScheduledTaskNow,
  useDeleteScheduledTask,
  type ScheduledTask,
  type ScheduleKind,
  type TaskStatus,
} from '../api/resources/scheduledTasks';

type Translate = (key: string, opts?: Record<string, unknown>) => string;

// ---- helpers ---------------------------------------------------------------

/** Human-readable interval, e.g. "alle 5 Min". Picks the coarsest whole unit. */
function humanizeInterval(seconds: number, t: Translate): string {
  if (seconds > 0 && seconds % 3600 === 0) {
    return t('scheduledTasks.everyHours', { count: seconds / 3600 });
  }
  if (seconds > 0 && seconds % 60 === 0) {
    return t('scheduledTasks.everyMinutes', { count: seconds / 60 });
  }
  return t('scheduledTasks.everySeconds', { count: seconds });
}

function describeSchedule(task: ScheduledTask, t: Translate): string {
  if (task.schedule_kind === 'interval' && task.interval_seconds != null) {
    return humanizeInterval(task.interval_seconds, t);
  }
  if (task.schedule_kind === 'cron' && task.cron_expr) {
    return task.cron_expr;
  }
  return t('scheduledTasks.scheduleUnknown');
}

const STATUS_BADGE: Record<TaskStatus, BadgeColor> = {
  ok: 'green',
  error: 'red',
  skipped: 'amber',
};

/** duration_ms → "41s" / "1m 20s" / "—" (null). */
function formatDuration(ms: number | null, t: Translate): string {
  if (ms == null) return '—';
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds < 60) {
    return t('scheduledTasks.runs.durationSeconds', { seconds: totalSeconds });
  }
  return t('scheduledTasks.runs.durationMinutes', {
    minutes: Math.floor(totalSeconds / 60),
    seconds: totalSeconds % 60,
  });
}

/** Inline per-task run-history table. Mounted only while its row is expanded, so
 *  the underlying query is fetched lazily (first expand). */
function TaskRunHistory({ taskId }: { taskId: number }) {
  const { t } = useTranslation();
  const runsQuery = useScheduledTaskRuns(taskId, { enabled: true });
  const runs = runsQuery.data ?? [];

  if (runsQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 py-2 text-sm text-gray-500 dark:text-gray-400">
        <Loader className="w-4 h-4 animate-spin" />
        <span>{t('scheduledTasks.runs.loading')}</span>
      </div>
    );
  }

  if (runsQuery.errorMessage) {
    return <Alert variant="error">{runsQuery.errorMessage}</Alert>;
  }

  if (runs.length === 0) {
    return (
      <p className="py-2 text-sm text-gray-500 dark:text-gray-400">
        {t('scheduledTasks.runs.empty')}
      </p>
    );
  }

  return (
    <div>
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
        {t('scheduledTasks.runs.title')}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-gray-400 dark:text-gray-500">
              <th className="px-3 py-2 font-medium">{t('scheduledTasks.runs.colTime')}</th>
              <th className="px-3 py-2 font-medium">{t('scheduledTasks.runs.colStatus')}</th>
              <th className="px-3 py-2 font-medium">{t('scheduledTasks.runs.colDuration')}</th>
              <th className="px-3 py-2 font-medium">{t('scheduledTasks.runs.colOutput')}</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.id}
                className="border-t border-gray-100 dark:border-gray-800"
              >
                <td className="px-3 py-2 align-top whitespace-nowrap text-gray-600 dark:text-gray-400">
                  {formatDateTime(run.started_at)}
                </td>
                <td className="px-3 py-2 align-top">
                  <Badge color={STATUS_BADGE[run.status]}>
                    {t(`scheduledTasks.status.${run.status}`)}
                  </Badge>
                </td>
                <td className="px-3 py-2 align-top whitespace-nowrap text-gray-600 dark:text-gray-400">
                  {formatDuration(run.duration_ms, t)}
                </td>
                <td className="px-3 py-2 align-top">
                  {run.error ? (
                    <span className="font-mono text-xs break-words text-red-600 dark:text-red-400">
                      {run.error}
                    </span>
                  ) : run.detail ? (
                    <span className="font-mono text-xs break-words text-gray-600 dark:text-gray-400">
                      {run.detail}
                    </span>
                  ) : (
                    <span className="text-gray-400 dark:text-gray-600">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface TaskFormData {
  name: string;
  handler_key: string;
  schedule_kind: ScheduleKind;
  interval_seconds: string;
  cron_expr: string;
  params: string;
  run_at_boot: boolean;
  enabled: boolean;
  start_at: string;
  end_at: string;
}

const emptyForm: TaskFormData = {
  name: '',
  handler_key: '',
  schedule_kind: 'interval',
  interval_seconds: '300',
  cron_expr: '',
  params: '{}',
  run_at_boot: false,
  enabled: true,
  start_at: '',
  end_at: '',
};

/** ISO / naive-UTC string → the `YYYY-MM-DDTHH:mm` a datetime-local input wants. */
function toLocalInput(value: string | null): string {
  if (!value) return '';
  return value.slice(0, 16);
}

// ---- component -------------------------------------------------------------

export default function ScheduledTasksPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const tasksQuery = useScheduledTasks();
  const tasks = tasksQuery.data?.tasks ?? [];
  const availableHandlers = tasksQuery.data?.available_handlers ?? [];
  const engineTick = tasksQuery.data?.engine_tick_seconds ?? null;

  const createTask = useCreateScheduledTask();
  const updateTask = useUpdateScheduledTask();
  const runTask = useRunScheduledTaskNow();
  const deleteTask = useDeleteScheduledTask();

  const [success, setSuccess] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);

  const [expandedTaskId, setExpandedTaskId] = useState<number | null>(null);

  const [showModal, setShowModal] = useState(false);
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null);
  const [formData, setFormData] = useState<TaskFormData>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);

  const error = tasksQuery.errorMessage ?? mutationError;
  const formLoading = createTask.isPending || updateTask.isPending;

  useEffect(() => {
    if (mutationError || success) {
      const timer = setTimeout(() => {
        setMutationError(null);
        setSuccess(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [mutationError, success]);

  const openCreate = () => {
    setEditingTask(null);
    setFormError(null);
    setFormData({ ...emptyForm, handler_key: availableHandlers[0] ?? '' });
    setShowModal(true);
  };

  const openEdit = (task: ScheduledTask) => {
    setEditingTask(task);
    setFormError(null);
    setFormData({
      name: task.name,
      handler_key: task.handler_key,
      schedule_kind: task.schedule_kind,
      interval_seconds: task.interval_seconds != null ? String(task.interval_seconds) : '',
      cron_expr: task.cron_expr ?? '',
      params: JSON.stringify(task.params ?? {}, null, 2),
      run_at_boot: task.run_at_boot,
      enabled: task.enabled,
      start_at: toLocalInput(task.start_at),
      end_at: toLocalInput(task.end_at),
    });
    setShowModal(true);
  };

  const handleToggleEnabled = async (task: ScheduledTask) => {
    try {
      await updateTask.mutateAsync({ id: task.id, input: { enabled: !task.enabled } });
      setSuccess(t('scheduledTasks.saved'));
    } catch (err) {
      setMutationError(extractApiError(err, t('scheduledTasks.failedToSave')));
    }
  };

  const handleRunNow = async (task: ScheduledTask) => {
    try {
      await runTask.mutateAsync(task.id);
      setSuccess(t('scheduledTasks.runTriggered', { name: task.name }));
    } catch (err) {
      setMutationError(extractApiError(err, t('scheduledTasks.failedToRun')));
    }
  };

  const handleDelete = async (task: ScheduledTask) => {
    if (task.is_builtin) {
      setMutationError(t('scheduledTasks.builtinCannotDelete'));
      return;
    }
    const confirmed = await confirm({
      title: t('scheduledTasks.deleteTask'),
      message: t('scheduledTasks.deleteConfirm', { name: task.name }),
      confirmLabel: t('common.delete'),
      variant: 'danger',
    });
    if (!confirmed) return;
    try {
      await deleteTask.mutateAsync(task.id);
      setSuccess(t('scheduledTasks.deleted'));
    } catch (err) {
      setMutationError(extractApiError(err, t('scheduledTasks.failedToDelete')));
    }
  };

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setFormError(null);

    // Parse params JSON up front so a typo doesn't reach the network.
    let params: Record<string, unknown> = {};
    const paramsText = formData.params.trim();
    if (paramsText) {
      try {
        const parsed: unknown = JSON.parse(paramsText);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          setFormError(t('scheduledTasks.paramsMustBeObject'));
          return;
        }
        params = parsed as Record<string, unknown>;
      } catch {
        setFormError(t('scheduledTasks.paramsInvalid'));
        return;
      }
    }

    const isInterval = formData.schedule_kind === 'interval';
    const intervalSeconds = isInterval ? Number(formData.interval_seconds) : null;
    if (isInterval && (!Number.isFinite(intervalSeconds) || (intervalSeconds ?? 0) <= 0)) {
      setFormError(t('scheduledTasks.intervalInvalid'));
      return;
    }
    if (!isInterval && !formData.cron_expr.trim()) {
      setFormError(t('scheduledTasks.cronRequired'));
      return;
    }

    const scheduleFields = {
      schedule_kind: formData.schedule_kind,
      interval_seconds: isInterval ? intervalSeconds : null,
      cron_expr: isInterval ? null : formData.cron_expr.trim(),
      params,
      run_at_boot: formData.run_at_boot,
      start_at: formData.start_at || null,
      end_at: formData.end_at || null,
    };

    try {
      if (editingTask) {
        await updateTask.mutateAsync({
          id: editingTask.id,
          input: { ...scheduleFields, enabled: formData.enabled },
        });
        setSuccess(t('scheduledTasks.saved'));
      } else {
        await createTask.mutateAsync({
          name: formData.name.trim(),
          handler_key: formData.handler_key,
          enabled: formData.enabled,
          ...scheduleFields,
        });
        setSuccess(t('scheduledTasks.created'));
      }
      setShowModal(false);
    } catch (err) {
      setFormError(extractApiError(err, t('scheduledTasks.failedToSave')));
    }
  };

  if (tasksQuery.isLoading) {
    return (
      <div className="space-y-6">
        <PageHeader icon={Clock} title={t('scheduledTasks.title')} subtitle={t('scheduledTasks.subtitle')} />
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
          <p className="text-gray-500 dark:text-gray-400">{t('scheduledTasks.loading')}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader icon={Clock} title={t('scheduledTasks.title')} subtitle={t('scheduledTasks.subtitle')} />

      {error && <Alert variant="error">{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

      <div className="flex flex-wrap items-center gap-3">
        <button onClick={openCreate} className="btn-primary inline-flex items-center gap-2">
          <Plus className="w-4 h-4" />
          <span>{t('scheduledTasks.createTask')}</span>
        </button>
        <button onClick={() => tasksQuery.refetch()} className="btn-secondary inline-flex items-center gap-2">
          <RefreshCw className="w-4 h-4" />
          <span>{t('common.refresh')}</span>
        </button>
        {engineTick != null && (
          <span className="text-sm text-gray-500 dark:text-gray-400">
            {t('scheduledTasks.engineTick', { seconds: engineTick })}
          </span>
        )}
      </div>

      {tasks.length === 0 ? (
        <div className="card text-center py-12">
          <Clock className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-3" />
          <p className="font-medium text-gray-700 dark:text-gray-300">{t('scheduledTasks.noTasks')}</p>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('scheduledTasks.noTasksDesc')}</p>
        </div>
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">
                <th className="px-4 py-3 font-medium">{t('scheduledTasks.colName')}</th>
                <th className="px-4 py-3 font-medium">{t('scheduledTasks.colSchedule')}</th>
                <th className="px-4 py-3 font-medium">{t('scheduledTasks.colWindow')}</th>
                <th className="px-4 py-3 font-medium">{t('scheduledTasks.colNextRun')}</th>
                <th className="px-4 py-3 font-medium">{t('scheduledTasks.colLastRun')}</th>
                <th className="px-4 py-3 font-medium">{t('scheduledTasks.colEnabled')}</th>
                <th className="px-4 py-3 font-medium text-right">{t('scheduledTasks.colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <Fragment key={task.id}>
                <tr
                  className="border-b border-gray-100 dark:border-gray-800 last:border-0 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
                >
                  {/* Name + badges */}
                  <td className="px-4 py-3 align-top">
                    <div className="font-medium text-gray-900 dark:text-white">{task.name}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400 font-mono">{task.handler_key}</div>
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {task.is_builtin && (
                        <Badge color="gray" icon={Lock}>{t('scheduledTasks.builtin')}</Badge>
                      )}
                      {task.run_at_boot && (
                        <Badge color="teal" icon={Power}>{t('scheduledTasks.runAtBoot')}</Badge>
                      )}
                    </div>
                  </td>

                  {/* Schedule */}
                  <td className="px-4 py-3 align-top text-gray-700 dark:text-gray-300">
                    {task.schedule_kind === 'cron' ? (
                      <span className="font-mono text-xs">{describeSchedule(task, t)}</span>
                    ) : (
                      describeSchedule(task, t)
                    )}
                  </td>

                  {/* Window */}
                  <td className="px-4 py-3 align-top text-gray-600 dark:text-gray-400 text-xs">
                    {task.start_at || task.end_at ? (
                      <>
                        <div>{task.start_at ? formatDateTime(task.start_at) : t('scheduledTasks.windowOpen')}</div>
                        <div>{task.end_at ? formatDateTime(task.end_at) : t('scheduledTasks.windowOpen')}</div>
                      </>
                    ) : (
                      <span className="text-gray-400 dark:text-gray-600">{t('scheduledTasks.windowNone')}</span>
                    )}
                  </td>

                  {/* Next run */}
                  <td className="px-4 py-3 align-top text-gray-600 dark:text-gray-400 text-xs whitespace-nowrap">
                    {task.next_run_at ? formatDateTime(task.next_run_at) : '—'}
                  </td>

                  {/* Last run + status */}
                  <td className="px-4 py-3 align-top text-xs whitespace-nowrap">
                    <div className="text-gray-600 dark:text-gray-400">
                      {task.last_run_at ? formatDateTime(task.last_run_at) : '—'}
                    </div>
                    <div className="mt-1">
                      {task.last_status ? (
                        <span title={task.last_error ?? undefined}>
                          <Badge color={STATUS_BADGE[task.last_status]}>
                            {t(`scheduledTasks.status.${task.last_status}`)}
                          </Badge>
                        </span>
                      ) : (
                        <Badge color="gray">{t('scheduledTasks.status.never')}</Badge>
                      )}
                    </div>
                  </td>

                  {/* Enabled toggle */}
                  <td className="px-4 py-3 align-top">
                    <button
                      type="button"
                      role="switch"
                      aria-checked={task.enabled}
                      aria-label={t(task.enabled ? 'scheduledTasks.disable' : 'scheduledTasks.enable')}
                      onClick={() => handleToggleEnabled(task)}
                      disabled={updateTask.isPending}
                      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-900 ${
                        task.enabled
                          ? 'bg-accent-600 dark:bg-accent-500'
                          : 'bg-gray-300 dark:bg-gray-600'
                      }`}
                    >
                      <span
                        className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                          task.enabled ? 'translate-x-6' : 'translate-x-1'
                        }`}
                      />
                    </button>
                  </td>

                  {/* Actions */}
                  <td className="px-4 py-3 align-top">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        type="button"
                        onClick={() =>
                          setExpandedTaskId((cur) => (cur === task.id ? null : task.id))
                        }
                        aria-expanded={expandedTaskId === task.id}
                        aria-label={t('scheduledTasks.runs.toggle')}
                        className="inline-flex items-center gap-1 p-2 rounded-lg text-gray-500 hover:text-accent-600 dark:text-gray-400 dark:hover:text-accent-400 hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors"
                        title={t('scheduledTasks.runs.toggle')}
                      >
                        <ChevronDown
                          className={`w-4 h-4 transition-transform ${
                            expandedTaskId === task.id ? 'rotate-180' : ''
                          }`}
                        />
                      </button>
                      <button
                        type="button"
                        onClick={() => handleRunNow(task)}
                        disabled={!task.enabled || runTask.isPending}
                        className="p-2 rounded-lg text-gray-500 hover:text-accent-600 dark:text-gray-400 dark:hover:text-accent-400 hover:bg-gray-100 dark:hover:bg-gray-700/50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        title={t('scheduledTasks.runNow')}
                      >
                        <Play className="w-4 h-4" />
                      </button>
                      <button
                        type="button"
                        onClick={() => openEdit(task)}
                        className="p-2 rounded-lg text-gray-500 hover:text-primary-600 dark:text-gray-400 dark:hover:text-primary-400 hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors"
                        title={t('scheduledTasks.editTask')}
                      >
                        <Pencil className="w-4 h-4" />
                      </button>
                      {!task.is_builtin && (
                        <button
                          type="button"
                          onClick={() => handleDelete(task)}
                          className="p-2 rounded-lg text-gray-500 hover:text-red-600 dark:text-gray-400 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors"
                          title={t('scheduledTasks.deleteTask')}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>

                {expandedTaskId === task.id && (
                  <tr className="bg-gray-50 dark:bg-gray-800/30">
                    <td colSpan={7} className="px-4 py-3">
                      <TaskRunHistory taskId={task.id} />
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create / Edit modal */}
      <Modal
        isOpen={showModal}
        onClose={() => setShowModal(false)}
        title={editingTask ? t('scheduledTasks.editTask') : t('scheduledTasks.createTask')}
        maxWidth="max-w-2xl"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('scheduledTasks.fieldName')} <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                className="input w-full"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                placeholder={t('scheduledTasks.namePlaceholder')}
                required
                disabled={formLoading || editingTask?.is_builtin}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('scheduledTasks.fieldHandler')} <span className="text-red-500">*</span>
              </label>
              <select
                className="input w-full"
                value={formData.handler_key}
                onChange={(e) => setFormData({ ...formData, handler_key: e.target.value })}
                required
                disabled={formLoading || editingTask != null}
              >
                {/* An editing built-in may reference a handler no longer offered;
                    keep it selectable so the value round-trips. */}
                {editingTask && !availableHandlers.includes(editingTask.handler_key) && (
                  <option value={editingTask.handler_key}>{editingTask.handler_key}</option>
                )}
                {availableHandlers.length === 0 && !editingTask && (
                  <option value="">{t('scheduledTasks.noHandlers')}</option>
                )}
                {availableHandlers.map((h) => (
                  <option key={h} value={h}>{h}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('scheduledTasks.fieldScheduleKind')}
              </label>
              <select
                className="input w-full"
                value={formData.schedule_kind}
                onChange={(e) => setFormData({ ...formData, schedule_kind: e.target.value as ScheduleKind })}
                disabled={formLoading}
              >
                <option value="interval">{t('scheduledTasks.kindInterval')}</option>
                <option value="cron">{t('scheduledTasks.kindCron')}</option>
              </select>
            </div>
            {formData.schedule_kind === 'interval' ? (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t('scheduledTasks.fieldInterval')} <span className="text-red-500">*</span>
                </label>
                <input
                  type="number"
                  min={1}
                  className="input w-full"
                  value={formData.interval_seconds}
                  onChange={(e) => setFormData({ ...formData, interval_seconds: e.target.value })}
                  disabled={formLoading}
                />
              </div>
            ) : (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t('scheduledTasks.fieldCron')} <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  className="input w-full font-mono"
                  value={formData.cron_expr}
                  onChange={(e) => setFormData({ ...formData, cron_expr: e.target.value })}
                  placeholder={t('scheduledTasks.cronPlaceholder')}
                  disabled={formLoading}
                />
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('scheduledTasks.fieldStartAt')}
              </label>
              <input
                type="datetime-local"
                className="input w-full"
                value={formData.start_at}
                onChange={(e) => setFormData({ ...formData, start_at: e.target.value })}
                disabled={formLoading}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('scheduledTasks.fieldEndAt')}
              </label>
              <input
                type="datetime-local"
                className="input w-full"
                value={formData.end_at}
                onChange={(e) => setFormData({ ...formData, end_at: e.target.value })}
                disabled={formLoading}
              />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('scheduledTasks.fieldParams')}
            </label>
            <textarea
              className="input w-full font-mono text-xs"
              rows={4}
              value={formData.params}
              onChange={(e) => setFormData({ ...formData, params: e.target.value })}
              placeholder="{}"
              spellCheck={false}
              disabled={formLoading}
            />
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{t('scheduledTasks.paramsHint')}</p>
          </div>

          <div className="flex flex-wrap gap-6">
            <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
              <input
                type="checkbox"
                className="w-4 h-4 rounded-sm border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-primary-600 focus:ring-primary-500"
                checked={formData.enabled}
                onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })}
                disabled={formLoading}
              />
              {t('scheduledTasks.fieldEnabled')}
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
              <input
                type="checkbox"
                className="w-4 h-4 rounded-sm border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-primary-600 focus:ring-primary-500"
                checked={formData.run_at_boot}
                onChange={(e) => setFormData({ ...formData, run_at_boot: e.target.checked })}
                disabled={formLoading}
              />
              {t('scheduledTasks.fieldRunAtBoot')}
            </label>
          </div>

          {formError && <Alert variant="error">{formError}</Alert>}

          <div className="flex gap-3 pt-4 border-t border-gray-200 dark:border-gray-700">
            <button
              type="button"
              onClick={() => setShowModal(false)}
              className="flex-1 btn-secondary"
              disabled={formLoading}
            >
              {t('common.cancel')}
            </button>
            <button type="submit" className="flex-1 btn-primary" disabled={formLoading}>
              {formLoading ? (
                <Loader className="w-5 h-5 animate-spin mx-auto" />
              ) : editingTask ? (
                t('scheduledTasks.saveTask')
              ) : (
                t('scheduledTasks.createTask')
              )}
            </button>
          </div>
        </form>
      </Modal>

      {ConfirmDialogComponent}
    </div>
  );
}
