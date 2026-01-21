import React, { useState, useEffect } from 'react';
import { CheckSquare, Clock, CheckCircle, XCircle, Loader } from 'lucide-react';
import apiClient from '../utils/axios';

export default function TasksPage() {
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
        <h1 className="text-2xl font-bold text-white mb-2">Aufgaben</h1>
        <p className="text-gray-400">Übersicht aller Aufgaben und deren Status</p>
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
                : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
            }`}
          >
            {f === 'all' ? 'Alle' : f}
          </button>
        ))}
      </div>

      {/* Tasks List */}
      <div className="space-y-4">
        {loading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-400 mb-2" />
            <p className="text-gray-400">Lade Aufgaben...</p>
          </div>
        ) : tasks.length === 0 ? (
          <div className="card text-center py-12">
            <CheckSquare className="w-12 h-12 mx-auto text-gray-600 mb-2" />
            <p className="text-gray-400">Keine Aufgaben gefunden</p>
          </div>
        ) : (
          tasks.map((task) => (
            <div key={task.id} className="card">
              <div className="flex items-start space-x-4">
                <div className="mt-1">{getStatusIcon(task.status)}</div>
                <div className="flex-1">
                  <h3 className="text-lg font-semibold text-white mb-1">
                    {task.title}
                  </h3>
                  <p className="text-sm text-gray-400 mb-2">
                    Typ: {task.task_type}
                  </p>
                  <p className="text-xs text-gray-500">
                    Erstellt: {new Date(task.created_at).toLocaleString('de-DE')}
                  </p>
                  {task.completed_at && (
                    <p className="text-xs text-gray-500">
                      Abgeschlossen: {new Date(task.completed_at).toLocaleString('de-DE')}
                    </p>
                  )}
                </div>
                <span className={`px-3 py-1 rounded-full text-xs font-medium ${
                  task.status === 'completed' ? 'bg-green-900 text-green-300' :
                  task.status === 'failed' ? 'bg-red-900 text-red-300' :
                  task.status === 'running' ? 'bg-blue-900 text-blue-300' :
                  'bg-yellow-900 text-yellow-300'
                }`}>
                  {task.status}
                </span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
