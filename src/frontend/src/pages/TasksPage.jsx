import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { CheckSquare, Clock, CheckCircle, XCircle, Loader } from 'lucide-react';
import apiClient from '../utils/axios';

export default function TasksPage() {
  const { t } = useTranslation();
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('all');

  useEffect(() => {
    loadTasks();
  }, [filter]);

  const loadTasks = async () => {
    try {
      const params = filter !== 'all' ? { status: filter } : {};
      const response = await apiClient.get('/api/tasks/list', { params });
      setTasks(response.data.tasks);
    } catch (error) {
      console.error('Fehler beim Laden der Tasks:', error);
    } finally {
      setLoading(false);
    }
  };

  const getStatusIcon = (status) => {
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

  const filters = ['all', 'pending', 'running', 'completed', 'failed'];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('tasks.title')}</h1>
        <p className="text-gray-500 dark:text-gray-400">{t('tasks.subtitle')}</p>
      </div>

      {/* Filters */}
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

      {/* Tasks List */}
      <div className="space-y-4">
        {loading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('tasks.loadingTasks')}</p>
          </div>
        ) : tasks.length === 0 ? (
          <div className="card text-center py-12">
            <CheckSquare className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('tasks.noTasks')}</p>
          </div>
        ) : (
          tasks.map((task) => (
            <div key={task.id} className="card">
              <div className="flex items-start space-x-4">
                <div className="mt-1">{getStatusIcon(task.status)}</div>
                <div className="flex-1">
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-1">
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
                <span className={`px-3 py-1 rounded-full text-xs font-medium ${
                  task.status === 'completed' ? 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300' :
                  task.status === 'failed' ? 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300' :
                  task.status === 'running' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300' :
                  'bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-300'
                }`}>
                  {t(`tasks.${task.status}`)}
                </span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
