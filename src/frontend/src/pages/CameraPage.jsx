import React, { useState, useEffect } from 'react';
import { Camera, RefreshCw, User, Car, Dog } from 'lucide-react';
import apiClient from '../utils/axios';

export default function CameraPage() {
  const [cameras, setCameras] = useState([]);
  const [events, setEvents] = useState([]);
  const [selectedLabel, setSelectedLabel] = useState('all');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadCameras();
    loadEvents();
  }, [selectedLabel]);

  const loadCameras = async () => {
    try {
      const response = await apiClient.get('/api/camera/cameras');
      setCameras(response.data.cameras);
    } catch (error) {
      console.error('Fehler beim Laden der Kameras:', error);
    }
  };

  const loadEvents = async () => {
    try {
      const params = selectedLabel !== 'all' ? { label: selectedLabel } : {};
      const response = await apiClient.get('/api/camera/events', { params });
      setEvents(response.data.events);
    } catch (error) {
      console.error('Fehler beim Laden der Events:', error);
    } finally {
      setLoading(false);
    }
  };

  const getLabelIcon = (label) => {
    switch (label) {
      case 'person':
        return <User className="w-5 h-5" />;
      case 'car':
        return <Car className="w-5 h-5" />;
      case 'dog':
      case 'cat':
        return <Dog className="w-5 h-5" />;
      default:
        return <Camera className="w-5 h-5" />;
    }
  };

  const labels = ['all', 'person', 'car', 'dog', 'cat'];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">Kamera-Überwachung</h1>
            <p className="text-gray-500 dark:text-gray-400">Überwache deine Kameras und Events</p>
          </div>
          <button
            onClick={() => { loadCameras(); loadEvents(); }}
            className="btn btn-secondary"
            aria-label="Kameras und Events aktualisieren"
          >
            <RefreshCw className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* Cameras Overview */}
      <div className="card">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">Kameras</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {cameras.map((camera) => (
            <div key={camera} className="bg-gray-100 dark:bg-gray-700 rounded-lg p-4">
              <div className="flex items-center space-x-2 mb-2">
                <Camera className="w-5 h-5 text-primary-400" />
                <span className="text-gray-900 dark:text-white font-medium">{camera}</span>
              </div>
              <div className="w-3 h-3 rounded-full bg-green-500" />
            </div>
          ))}
        </div>
      </div>

      {/* Label Filters */}
      <div className="flex space-x-2 overflow-x-auto">
        {labels.map((label) => (
          <button
            key={label}
            onClick={() => setSelectedLabel(label)}
            className={`px-4 py-2 rounded-lg capitalize whitespace-nowrap transition-colors flex items-center space-x-2 ${
              selectedLabel === label
                ? 'bg-primary-600 text-white'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
            }`}
          >
            {label !== 'all' && getLabelIcon(label)}
            <span>{label === 'all' ? 'Alle' : label}</span>
          </button>
        ))}
      </div>

      {/* Events */}
      <div className="space-y-4">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white">Letzte Events</h2>

        {loading ? (
          <div className="card text-center py-12" role="status" aria-label="Events werden geladen">
            <p className="text-gray-500 dark:text-gray-400">Lade Events...</p>
          </div>
        ) : events.length === 0 ? (
          <div className="card text-center py-12">
            <Camera className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">Keine Events gefunden</p>
          </div>
        ) : (
          events.map((event, index) => (
            <div key={index} className="card">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-4">
                  {getLabelIcon(event.label)}
                  <div>
                    <p className="text-gray-900 dark:text-white font-medium">{event.label}</p>
                    <p className="text-sm text-gray-500 dark:text-gray-400">{event.camera}</p>
                  </div>
                </div>
                <div className="text-right">
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    {new Date(event.start_time * 1000).toLocaleString('de-DE')}
                  </p>
                  {event.score && (
                    <p className="text-xs text-gray-400 dark:text-gray-500">
                      Konfidenz: {Math.round(event.score * 100)}%
                    </p>
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
