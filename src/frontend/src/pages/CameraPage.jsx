import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Camera, RefreshCw, User, Car, Dog } from 'lucide-react';
import apiClient from '../utils/axios';
import PageHeader from '../components/PageHeader';
import Badge from '../components/Badge';
import Alert from '../components/Alert';

export default function CameraPage() {
  const { t, i18n } = useTranslation();
  const [cameras, setCameras] = useState([]);
  const [events, setEvents] = useState([]);
  const [selectedLabel, setSelectedLabel] = useState('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    loadCameras();
    loadEvents();
  }, [selectedLabel]);

  const loadCameras = async () => {
    try {
      const response = await apiClient.get('/api/camera/cameras');
      setCameras(response.data.cameras);
      setError('');
    } catch (err) {
      console.error('Fehler beim Laden der Kameras:', err);
      setError(t('cameras.loadError') || 'Error loading cameras');
    }
  };

  const loadEvents = async () => {
    try {
      const params = selectedLabel !== 'all' ? { label: selectedLabel } : {};
      const response = await apiClient.get('/api/camera/events', { params });
      setEvents(response.data.events);
      setError('');
    } catch (err) {
      console.error('Fehler beim Laden der Events:', err);
      // Only set error if not already set by loadCameras
      if (!error) setError(t('cameras.loadEventsError') || 'Error loading events');
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
      <PageHeader icon={Camera} title={t('cameras.title')} subtitle={t('cameras.subtitle')}>
        <button
          onClick={() => { loadCameras(); loadEvents(); }}
          className="btn-icon btn-icon-ghost"
          aria-label={t('cameras.refreshCameras')}
        >
          <RefreshCw className="w-5 h-5" aria-hidden="true" />
        </button>
      </PageHeader>

      {error && <Alert variant="error">{error}</Alert>}

      {/* Cameras Overview */}
      <div className="card">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">{t('cameras.cameras')}</h2>
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
            className={`btn ${selectedLabel === label
              ? 'btn-primary'
              : 'btn-ghost bg-gray-200 dark:bg-gray-800'
              } capitalize flex items-center space-x-2`}
          >
            {label !== 'all' && getLabelIcon(label)}
            <span>{label === 'all' ? t('common.all') : t(`cameras.${label}`)}</span>
          </button>
        ))}
      </div>

      {/* Events */}
      <div className="space-y-4">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white">{t('cameras.latestEvents')}</h2>

        {loading ? (
          <div className="card text-center py-12" role="status" aria-label={t('cameras.loadingEvents')}>
            <p className="text-gray-500 dark:text-gray-400">{t('cameras.loadingEvents')}</p>
          </div>
        ) : events.length === 0 ? (
          <div className="card text-center py-12">
            <Camera className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-2" />
            <p className="text-gray-500 dark:text-gray-400">{t('cameras.noEvents')}</p>
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
                  <p className="text-sm text-gray-500 dark:text-gray-400 mb-1">
                    {new Date(event.start_time * 1000).toLocaleString(i18n.language === 'de' ? 'de-DE' : 'en-US')}
                  </p>
                  {event.score && (
                    <Badge color={event.score > 0.8 ? 'green' : 'amber'}>
                      {t('cameras.confidence')}: {Math.round(event.score * 100)}%
                    </Badge>
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
