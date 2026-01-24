import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Lightbulb, Power, Search, Loader, Sun, Thermometer } from 'lucide-react';
import apiClient from '../utils/axios';

export default function HomeAssistantPage() {
  const { t } = useTranslation();
  const [entities, setEntities] = useState([]);
  const [filteredEntities, setFilteredEntities] = useState([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedDomain, setSelectedDomain] = useState('all');
  const [loading, setLoading] = useState(true);

  const domains = [
    { key: 'all', nameKey: 'common.all', icon: Power },
    { key: 'light', nameKey: 'homeassistant.lights', icon: Lightbulb },
    { key: 'switch', nameKey: 'homeassistant.switches', icon: Power },
    { key: 'climate', nameKey: 'homeassistant.climate', icon: Thermometer },
    { key: 'cover', nameKey: 'homeassistant.covers', icon: Sun },
  ];

  useEffect(() => {
    loadEntities();
  }, []);

  useEffect(() => {
    filterEntities();
  }, [searchQuery, selectedDomain, entities]);

  const loadEntities = async () => {
    try {
      const response = await apiClient.get('/api/homeassistant/states');
      setEntities(response.data.states);
    } catch (error) {
      console.error('Fehler beim Laden der Entities:', error);
    } finally {
      setLoading(false);
    }
  };

  const filterEntities = () => {
    let filtered = entities;

    // Domain Filter
    if (selectedDomain !== 'all') {
      filtered = filtered.filter(e => e.entity_id.startsWith(`${selectedDomain}.`));
    }

    // Search Filter
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(e => {
        const entityId = e.entity_id.toLowerCase();
        const friendlyName = e.attributes?.friendly_name?.toLowerCase() || '';
        return entityId.includes(query) || friendlyName.includes(query);
      });
    }

    setFilteredEntities(filtered);
  };

  const toggleEntity = async (entityId) => {
    try {
      await apiClient.post(`/api/homeassistant/toggle/${entityId}`);
      // Reload entities to get updated state
      await loadEntities();
    } catch (error) {
      console.error('Fehler beim Umschalten:', error);
    }
  };

  const getEntityIcon = (entity) => {
    const domain = entity.entity_id.split('.')[0];
    switch (domain) {
      case 'light':
        return <Lightbulb className="w-5 h-5" />;
      case 'switch':
        return <Power className="w-5 h-5" />;
      case 'climate':
        return <Thermometer className="w-5 h-5" />;
      default:
        return <Power className="w-5 h-5" />;
    }
  };

  const isEntityOn = (entity) => {
    return entity.state === 'on' || entity.state === 'open';
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">{t('homeassistant.title')}</h1>
        <p className="text-gray-500 dark:text-gray-400">{t('homeassistant.subtitle')}</p>
      </div>

      {/* Search */}
      <div className="card">
        <div className="relative">
          <label htmlFor="device-search" className="sr-only">{t('homeassistant.searchDevices')}</label>
          <Search className="absolute left-3 top-3 w-5 h-5 text-gray-400" aria-hidden="true" />
          <input
            id="device-search"
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t('homeassistant.searchDevicesPlaceholder')}
            className="input pl-10"
          />
        </div>
      </div>

      {/* Domain Filters */}
      <div className="flex space-x-2 overflow-x-auto">
        {domains.map((domain) => {
          const Icon = domain.icon;
          return (
            <button
              key={domain.key}
              onClick={() => setSelectedDomain(domain.key)}
              className={`px-4 py-2 rounded-lg whitespace-nowrap transition-colors flex items-center space-x-2 ${
                selectedDomain === domain.key
                  ? 'bg-primary-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
              }`}
            >
              <Icon className="w-4 h-4" />
              <span>{t(domain.nameKey)}</span>
            </button>
          );
        })}
      </div>

      {/* Entities Grid */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
            {t('homeassistant.devicesCount', { count: filteredEntities.length })}
          </h2>
        </div>

        {loading ? (
          <div className="card text-center py-12" role="status" aria-label={t('homeassistant.loadingDevices')}>
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" aria-hidden="true" />
            <p className="text-gray-500 dark:text-gray-400">{t('homeassistant.loadingDevices')}</p>
          </div>
        ) : filteredEntities.length === 0 ? (
          <div className="card text-center py-12">
            <p className="text-gray-500 dark:text-gray-400">{t('homeassistant.noDevices')}</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" role="list">
            {filteredEntities.map((entity) => (
              <button
                key={entity.entity_id}
                type="button"
                className={`card text-left cursor-pointer transition-all hover:scale-105 w-full ${
                  isEntityOn(entity) ? 'bg-primary-100 border-2 border-primary-600 dark:bg-primary-900/30' : ''
                }`}
                onClick={() => toggleEntity(entity.entity_id)}
                aria-pressed={isEntityOn(entity)}
                aria-label={`${entity.attributes?.friendly_name || entity.entity_id} ${isEntityOn(entity) ? t('homeassistant.on') : t('homeassistant.off')}`}
                role="listitem"
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-center space-x-3">
                    <div className={`p-2 rounded-lg ${
                      isEntityOn(entity) ? 'bg-primary-600' : 'bg-gray-200 dark:bg-gray-700'
                    }`} aria-hidden="true">
                      {getEntityIcon(entity)}
                    </div>
                    <div>
                      <p className="text-gray-900 dark:text-white font-medium">
                        {entity.attributes?.friendly_name || entity.entity_id}
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-400">{entity.entity_id}</p>
                    </div>
                  </div>
                  <div
                    className={`w-3 h-3 rounded-full ${
                      isEntityOn(entity) ? 'bg-green-500' : 'bg-gray-400 dark:bg-gray-600'
                    }`}
                    aria-hidden="true"
                  />
                </div>

                {entity.attributes?.brightness && (
                  <div className="mt-3">
                    <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
                      <span>{t('homeassistant.brightness')}</span>
                      <span aria-label={t('homeassistant.brightnessPercent', { percent: Math.round((entity.attributes.brightness / 255) * 100) })}>
                        {Math.round((entity.attributes.brightness / 255) * 100)}%
                      </span>
                    </div>
                    <div
                      className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2"
                      role="progressbar"
                      aria-valuenow={Math.round((entity.attributes.brightness / 255) * 100)}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label={t('homeassistant.brightness')}
                    >
                      <div
                        className="bg-primary-500 h-2 rounded-full"
                        style={{ width: `${(entity.attributes.brightness / 255) * 100}%` }}
                      />
                    </div>
                  </div>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
