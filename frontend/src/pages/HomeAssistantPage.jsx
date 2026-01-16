import React, { useState, useEffect } from 'react';
import { Lightbulb, Power, Search, Loader, Sun, Thermometer } from 'lucide-react';
import apiClient from '../utils/axios';

export default function HomeAssistantPage() {
  const [entities, setEntities] = useState([]);
  const [filteredEntities, setFilteredEntities] = useState([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedDomain, setSelectedDomain] = useState('all');
  const [loading, setLoading] = useState(true);

  const domains = [
    { key: 'all', name: 'Alle', icon: Power },
    { key: 'light', name: 'Lichter', icon: Lightbulb },
    { key: 'switch', name: 'Schalter', icon: Power },
    { key: 'climate', name: 'Klima', icon: Thermometer },
    { key: 'cover', name: 'Rollläden', icon: Sun },
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
        <h1 className="text-2xl font-bold text-white mb-2">Smart Home</h1>
        <p className="text-gray-400">Steuere dein Zuhause mit Home Assistant</p>
      </div>

      {/* Search */}
      <div className="card">
        <div className="relative">
          <Search className="absolute left-3 top-3 w-5 h-5 text-gray-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Geräte suchen..."
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
                  : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
              }`}
            >
              <Icon className="w-4 h-4" />
              <span>{domain.name}</span>
            </button>
          );
        })}
      </div>

      {/* Entities Grid */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-white">
            Geräte ({filteredEntities.length})
          </h2>
        </div>

        {loading ? (
          <div className="card text-center py-12">
            <Loader className="w-8 h-8 animate-spin mx-auto text-gray-400 mb-2" />
            <p className="text-gray-400">Lade Geräte...</p>
          </div>
        ) : filteredEntities.length === 0 ? (
          <div className="card text-center py-12">
            <p className="text-gray-400">Keine Geräte gefunden</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {filteredEntities.map((entity) => (
              <div
                key={entity.entity_id}
                className={`card cursor-pointer transition-all hover:scale-105 ${
                  isEntityOn(entity) ? 'bg-primary-900/30 border-2 border-primary-600' : ''
                }`}
                onClick={() => toggleEntity(entity.entity_id)}
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-center space-x-3">
                    <div className={`p-2 rounded-lg ${
                      isEntityOn(entity) ? 'bg-primary-600' : 'bg-gray-700'
                    }`}>
                      {getEntityIcon(entity)}
                    </div>
                    <div>
                      <p className="text-white font-medium">
                        {entity.attributes?.friendly_name || entity.entity_id}
                      </p>
                      <p className="text-xs text-gray-400">{entity.entity_id}</p>
                    </div>
                  </div>
                  <div className={`w-3 h-3 rounded-full ${
                    isEntityOn(entity) ? 'bg-green-500' : 'bg-gray-600'
                  }`} />
                </div>
                
                {entity.attributes?.brightness && (
                  <div className="mt-3">
                    <div className="flex items-center justify-between text-xs text-gray-400 mb-1">
                      <span>Helligkeit</span>
                      <span>{Math.round((entity.attributes.brightness / 255) * 100)}%</span>
                    </div>
                    <div className="w-full bg-gray-700 rounded-full h-2">
                      <div
                        className="bg-primary-500 h-2 rounded-full"
                        style={{ width: `${(entity.attributes.brightness / 255) * 100}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
