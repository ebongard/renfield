import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed';
export type TaskFilter = TaskStatus | 'all';

export interface Task {
  id: string | number;
  status: TaskStatus;
  title: string;
  task_type: string;
  created_at: string;
  completed_at?: string | null;
}

export interface TaskListResponse {
  tasks: Task[];
}

async function fetchTasks(filter: TaskFilter): Promise<TaskListResponse> {
  const params = filter !== 'all' ? { status: filter } : {};
  const response = await apiClient.get<TaskListResponse>('/api/tasks/list', { params });
  return response.data;
}

export function useTasksQuery(filter: TaskFilter) {
  return useApiQuery(
    {
      queryKey: keys.tasks.list(filter),
      queryFn: () => fetchTasks(filter),
      staleTime: STALE.DEFAULT,
    },
    'tasks.couldNotLoad',
  );
}
