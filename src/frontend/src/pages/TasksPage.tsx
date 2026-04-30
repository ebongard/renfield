import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CheckSquare, Clock, CheckCircle, XCircle, Loader } from 'lucide-react';
import PageHeader from '../components/PageHeader';
import Badge from '../components/Badge';
import { useTasksQuery, type TaskStatus, type TaskFilter } from '../api/resources/tasks';

export default function TasksPage() {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<TaskFilter>('all');
  const tasksQuery = useTasksQuery(filter);
  const tasks = tasksQuery.data?.tasks ?? [];

  const getStatusIcon = (status: TaskStatus) => {
    switch (status) {
      case 'pending':
        return <Clock className="w-5 h-5 text-yellow-500" />;
      case 'running':
        return <Loader className="w-5 h-5 text-blue-500 animate-spin" />;
      case 'completed':
        return <CheckCircle className="w-5 h-5 text-green-500" />;
      case 'failed':
        return <XCircle className="w-5 h-5 text-red-500" />;
      default:
        return null;
    }
  };

  const filters: TaskFilter[] = ['all', 'pending', 'running', 'completed', 'failed'];

  return (
    <div className="space-y-6">
      <PageHeader
        icon={CheckSquare}
        title={t('tasks.title')}
        subtitle={t('tasks.subtitle')}
      />

      <div className="flex space-x-2 overflow-x-auto">
        {filters.map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-4 py-2 rounded-lg capitalize whitespace-nowrap transition-colors ${
              filter === f
                ? 'bg-primary-600 text-white'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
            }`}
          >
            {f === 'all' ? t('common.all') : t(`tasks.${f}`)}
          </button>
        ))}
      </div>

      <div className="space-y-4">
        {tasksQuery.isLoading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('tasks.loadingTasks')}</p>
          </div>
        ) : tasksQuery.errorMessage ? (
          <div className="card text-center py-12">
            <XCircle className="w-12 h-12 mx-auto text-red-500 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{tasksQuery.errorMessage}</p>
          </div>
        ) : tasks.length === 0 ? (
          <div className="card text-center py-12">
            <CheckSquare className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-3" />
            <p className="font-medium text-gray-700 dark:text-gray-300">{t('tasks.noTasks')}</p>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('tasks.noTasksDesc')}</p>
          </div>
        ) : (
          tasks.map((task) => (
            <div key={task.id} className="card">
              <div className="flex items-start space-x-4">
                <div className="mt-1">{getStatusIcon(task.status)}</div>
                <div className="flex-1">
                  <h3 className="text-base font-semibold text-gray-900 dark:text-white mb-1">
                    {task.title}
                  </h3>
                  <p className="text-sm text-gray-500 dark:text-gray-400 mb-2">
                    {t('tasks.taskType', { type: task.task_type })}
                  </p>
                  <p className="text-xs text-gray-500">
                    {t('tasks.created')}: {new Date(task.created_at).toLocaleString()}
                  </p>
                  {task.completed_at && (
                    <p className="text-xs text-gray-500">
                      {t('tasks.completed')}: {new Date(task.completed_at).toLocaleString()}
                    </p>
                  )}
                </div>
                <Badge color={
                  task.status === 'completed' ? 'green' :
                  task.status === 'failed' ? 'red' :
                  task.status === 'running' ? 'blue' : 'yellow'
                }>
                  {t(`tasks.${task.status}`)}
                </Badge>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
