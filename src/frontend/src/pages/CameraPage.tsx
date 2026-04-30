import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Camera, RefreshCw, User, Car, Dog } from 'lucide-react';
import PageHeader from '../components/PageHeader';
import { useCamerasQuery, useCameraEventsQuery } from '../api/resources/cameras';

type CameraLabel = 'person' | 'car' | 'dog' | 'cat';
type LabelFilter = CameraLabel | 'all';

export default function CameraPage() {
  const { t, i18n } = useTranslation();
  const [selectedLabel, setSelectedLabel] = useState<LabelFilter>('all');

  const camerasQuery = useCamerasQuery();
  const eventsQuery = useCameraEventsQuery(selectedLabel === 'all' ? null : selectedLabel);

  const cameras = camerasQuery.data ?? [];
  const events = eventsQuery.data ?? [];
  const loading = eventsQuery.isLoading;

  const refresh = () => {
    camerasQuery.refetch();
    eventsQuery.refetch();
  };

  const getLabelIcon = (label: string) => {
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

  const labels: LabelFilter[] = ['all', 'person', 'car', 'dog', 'cat'];

  return (
    <div className="space-y-6">
      <PageHeader icon={Camera} title={t('cameras.title')} subtitle={t('cameras.subtitle')}>
        <button
          onClick={refresh}
          className="btn btn-secondary"
          aria-label={t('cameras.refreshCameras')}
        >
          <RefreshCw className="w-5 h-5" aria-hidden="true" />
        </button>
      </PageHeader>

      <div className="card">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">{t('cameras.cameras')}</h2>
        {cameras.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">{t('cameras.noCameras')}</p>
        ) : (
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
        )}
      </div>

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
            <span>{label === 'all' ? t('common.all') : t(`cameras.${label}`)}</span>
          </button>
        ))}
      </div>

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
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    {new Date(event.start_time * 1000).toLocaleString(i18n.language === 'de' ? 'de-DE' : 'en-US')}
                  </p>
                  {event.score && (
                    <p className="text-xs text-gray-400 dark:text-gray-500">
                      {t('cameras.confidence')}: {Math.round(event.score * 100)}%
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
