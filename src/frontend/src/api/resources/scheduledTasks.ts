import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type ScheduleKind = 'interval' | 'cron';
export type TaskStatus = 'ok' | 'error' | 'skipped';

/** A backend-scheduled task. Mirrors ScheduledTaskResponse in
 *  api/routes/scheduled_tasks.py. Built-in tasks are edit-not-delete. */
export interface ScheduledTask {
  id: number;
  name: string;
  handler_key: string;
  schedule_kind: ScheduleKind;
  interval_seconds: number | null;
  cron_expr: string | null;
  params: Record<string, unknown>;
  enabled: boolean;
  run_at_boot: boolean;
  start_at: string | null;
  end_at: string | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: TaskStatus | null;
  last_error: string | null;
  last_duration_ms: number | null;
  is_builtin: boolean;
  created_at: string;
  updated_at: string;
}

/** GET /api/scheduled-tasks response envelope. */
export interface ScheduledTasksResponse {
  tasks: ScheduledTask[];
  available_handlers: string[];
  engine_tick_seconds: number;
}

/** One recorded execution of a scheduled task. Mirrors ScheduledTaskRun in
 *  api/routes/scheduled_tasks.py; the API returns these newest-first. */
export interface ScheduledTaskRun {
  id: number;
  started_at: string;
  finished_at: string | null;
  status: TaskStatus;
  duration_ms: number | null;
  /** The handler's output line (e.g. "deleted=200 remaining=1852"). */
  detail: string | null;
  /** Set only on a failed run. */
  error: string | null;
}

/** Create body — a custom task. `handler_key` must be one of the
 *  `available_handlers`; interval must be ≥ engine tick; cron must parse. */
export interface CreateScheduledTaskInput {
  name: string;
  handler_key: string;
  schedule_kind: ScheduleKind;
  interval_seconds?: number | null;
  cron_expr?: string | null;
  params?: Record<string, unknown>;
  run_at_boot?: boolean;
  enabled?: boolean;
  start_at?: string | null;
  end_at?: string | null;
}

/** PATCH body — any subset of the mutable fields. */
export interface UpdateScheduledTaskInput {
  enabled?: boolean;
  schedule_kind?: ScheduleKind;
  interval_seconds?: number | null;
  cron_expr?: string | null;
  params?: Record<string, unknown>;
  run_at_boot?: boolean;
  start_at?: string | null;
  end_at?: string | null;
}

export interface DeleteScheduledTaskResult {
  success: boolean;
  /** The deleted task's name (backend echoes the name, not a count). */
  deleted: string;
}

async function fetchScheduledTasks(): Promise<ScheduledTasksResponse> {
  const response = await apiClient.get<ScheduledTasksResponse>('/api/scheduled-tasks');
  return response.data;
}

async function fetchScheduledTaskRuns(id: number): Promise<ScheduledTaskRun[]> {
  const response = await apiClient.get<ScheduledTaskRun[]>(
    `/api/scheduled-tasks/${id}/runs`,
    { params: { limit: 50 } },
  );
  return response.data;
}

async function createScheduledTaskRequest(
  input: CreateScheduledTaskInput,
): Promise<ScheduledTask> {
  const response = await apiClient.post<ScheduledTask>('/api/scheduled-tasks', input);
  return response.data;
}

async function updateScheduledTaskRequest(args: {
  id: number;
  input: UpdateScheduledTaskInput;
}): Promise<ScheduledTask> {
  const response = await apiClient.patch<ScheduledTask>(
    `/api/scheduled-tasks/${args.id}`,
    args.input,
  );
  return response.data;
}

async function runScheduledTaskNowRequest(id: number): Promise<ScheduledTask> {
  const response = await apiClient.post<ScheduledTask>(`/api/scheduled-tasks/${id}/run-now`);
  return response.data;
}

async function deleteScheduledTaskRequest(id: number): Promise<DeleteScheduledTaskResult> {
  const response = await apiClient.delete<DeleteScheduledTaskResult>(
    `/api/scheduled-tasks/${id}`,
  );
  return response.data;
}

export function useScheduledTasks() {
  return useApiQuery(
    {
      queryKey: keys.scheduledTasks.list(),
      queryFn: fetchScheduledTasks,
      staleTime: STALE.DEFAULT,
    },
    'scheduledTasks.failedToLoad',
  );
}

/** Per-task run history. `enabled` gates the fetch so the runs only load once
 *  the caller expands the row (lazy). */
export function useScheduledTaskRuns(taskId: number, opts?: { enabled?: boolean }) {
  return useApiQuery(
    {
      queryKey: keys.scheduledTasks.runs(taskId),
      queryFn: () => fetchScheduledTaskRuns(taskId),
      staleTime: STALE.DEFAULT,
      enabled: opts?.enabled ?? true,
    },
    'scheduledTasks.runs.failedToLoad',
  );
}

export function useCreateScheduledTask() {
  const queryClient = useQueryClient();
  return useApiMutation<ScheduledTask, CreateScheduledTaskInput>(
    {
      mutationFn: createScheduledTaskRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.scheduledTasks.all });
      },
    },
    'scheduledTasks.failedToSave',
  );
}

export function useUpdateScheduledTask() {
  const queryClient = useQueryClient();
  return useApiMutation<ScheduledTask, { id: number; input: UpdateScheduledTaskInput }>(
    {
      mutationFn: updateScheduledTaskRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.scheduledTasks.all });
      },
    },
    'scheduledTasks.failedToSave',
  );
}

export function useRunScheduledTaskNow() {
  const queryClient = useQueryClient();
  return useApiMutation<ScheduledTask, number>(
    {
      mutationFn: runScheduledTaskNowRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.scheduledTasks.all });
      },
    },
    'scheduledTasks.failedToRun',
  );
}

export function useDeleteScheduledTask() {
  const queryClient = useQueryClient();
  return useApiMutation<DeleteScheduledTaskResult, number>(
    {
      mutationFn: deleteScheduledTaskRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.scheduledTasks.all });
      },
    },
    'scheduledTasks.failedToDelete',
  );
}
