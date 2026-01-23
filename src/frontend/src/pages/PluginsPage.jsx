/**
 * Plugins Management Page
 *
 * Admin page for viewing and managing plugins.
 */
import { useState, useEffect, useCallback } from 'react';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import {
  Puzzle, RefreshCw, Loader, AlertCircle, CheckCircle,
  Power, PowerOff, ChevronDown, ChevronRight, Info, Settings, Code
} from 'lucide-react';

export default function PluginsPage() {
  const { getAccessToken, hasPermission } = useAuth();

  const [plugins, setPlugins] = useState([]);
  const [pluginsEnabled, setPluginsEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [togglingPlugin, setTogglingPlugin] = useState(null);

  // Detail modal
  const [selectedPlugin, setSelectedPlugin] = useState(null);
  const [expandedIntents, setExpandedIntents] = useState({});

  const canManage = hasPermission('plugins.manage');

  // Load plugins
  const loadPlugins = useCallback(async () => {
    try {
      setLoading(true);
      const token = getAccessToken();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};

      const response = await apiClient.get('/api/plugins', { headers });
      setPlugins(response.data?.plugins || []);
      setPluginsEnabled(response.data?.plugins_enabled ?? true);
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to load plugins');
    } finally {
      setLoading(false);
    }
  }, [getAccessToken]);

  useEffect(() => {
    loadPlugins();
  }, [loadPlugins]);

  // Auto-clear alerts
  useEffect(() => {
    if (error || success) {
      const timer = setTimeout(() => {
        setError(null);
        setSuccess(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [error, success]);

  // Toggle plugin
  const togglePlugin = async (plugin) => {
    if (!canManage) return;

    setTogglingPlugin(plugin.name);
    try {
      const token = getAccessToken();
      const response = await apiClient.post(
        `/api/plugins/${encodeURIComponent(plugin.name)}/toggle`,
        { enabled: !plugin.enabled },
        { headers: { Authorization: `Bearer ${token}` } }
      );

      setSuccess(response.data.message);
      loadPlugins();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to toggle plugin');
    } finally {
      setTogglingPlugin(null);
    }
  };

  // Toggle intent expansion
  const toggleIntent = (intentName) => {
    setExpandedIntents(prev => ({
      ...prev,
      [intentName]: !prev[intentName]
    }));
  };

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="card">
          <h1 className="text-2xl font-bold text-white mb-2">Plugins</h1>
          <p className="text-gray-400">Manage available plugins and integrations</p>
        </div>
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-400 mb-2" />
          <p className="text-gray-400">Loading plugins...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <h1 className="text-2xl font-bold text-white mb-2">Plugins</h1>
        <p className="text-gray-400">Manage available plugins and integrations</p>
      </div>

      {/* Alerts */}
      {error && (
        <div className="card bg-red-900/20 border-red-700">
          <div className="flex items-center space-x-3">
            <AlertCircle className="w-5 h-5 text-red-500" />
            <p className="text-red-400">{error}</p>
          </div>
        </div>
      )}

      {success && (
        <div className="card bg-green-900/20 border-green-700">
          <div className="flex items-center space-x-3">
            <CheckCircle className="w-5 h-5 text-green-500" />
            <p className="text-green-400">{success}</p>
          </div>
        </div>
      )}

      {/* Status Banner */}
      {!pluginsEnabled && (
        <div className="card bg-yellow-900/20 border-yellow-700">
          <div className="flex items-center space-x-3">
            <Info className="w-5 h-5 text-yellow-500" />
            <p className="text-yellow-400">
              Plugin system is disabled. Set <code className="bg-gray-800 px-1 rounded">PLUGINS_ENABLED=true</code> to enable plugins.
            </p>
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-wrap gap-3">
        <button onClick={loadPlugins} className="btn bg-gray-700 hover:bg-gray-600 flex items-center space-x-2">
          <RefreshCw className="w-4 h-4" />
          <span>Refresh</span>
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="card text-center">
          <p className="text-3xl font-bold text-white">{plugins.length}</p>
          <p className="text-gray-400 text-sm">Total Plugins</p>
        </div>
        <div className="card text-center">
          <p className="text-3xl font-bold text-green-400">{plugins.filter(p => p.enabled).length}</p>
          <p className="text-gray-400 text-sm">Enabled</p>
        </div>
        <div className="card text-center">
          <p className="text-3xl font-bold text-gray-500">{plugins.filter(p => !p.enabled).length}</p>
          <p className="text-gray-400 text-sm">Disabled</p>
        </div>
        <div className="card text-center">
          <p className="text-3xl font-bold text-primary-400">
            {plugins.reduce((sum, p) => sum + (p.intents?.length || 0), 0)}
          </p>
          <p className="text-gray-400 text-sm">Total Intents</p>
        </div>
      </div>

      {/* Plugins List */}
      <div className="space-y-3">
        {plugins.length === 0 ? (
          <div className="card text-center py-12">
            <Puzzle className="w-12 h-12 mx-auto text-gray-600 mb-4" />
            <p className="text-gray-400">No plugins found</p>
            <p className="text-gray-500 text-sm mt-1">
              Add plugin YAML files to the plugins directory
            </p>
          </div>
        ) : (
          plugins.map((plugin) => (
            <div key={plugin.name} className="card hover:bg-gray-750 transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-4">
                  {/* Icon */}
                  <div className={`w-12 h-12 rounded-full flex items-center justify-center ${
                    plugin.enabled ? 'bg-green-900/50' : 'bg-gray-700'
                  }`}>
                    <Puzzle className={`w-6 h-6 ${
                      plugin.enabled ? 'text-green-400' : 'text-gray-500'
                    }`} />
                  </div>

                  {/* Info */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center space-x-2">
                      <h3 className="text-lg font-semibold text-white">{plugin.name}</h3>
                      <span className="text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded">
                        v{plugin.version}
                      </span>
                      {plugin.enabled ? (
                        <span className="flex items-center space-x-1 text-xs bg-green-900/50 text-green-400 px-2 py-0.5 rounded">
                          <Power className="w-3 h-3" />
                          <span>Enabled</span>
                        </span>
                      ) : (
                        <span className="flex items-center space-x-1 text-xs bg-gray-700 text-gray-500 px-2 py-0.5 rounded">
                          <PowerOff className="w-3 h-3" />
                          <span>Disabled</span>
                        </span>
                      )}
                    </div>
                    <p className="text-gray-400 text-sm truncate">{plugin.description}</p>
                    {plugin.author && (
                      <p className="text-gray-500 text-xs mt-1">by {plugin.author}</p>
                    )}
                    <div className="flex flex-wrap gap-1 mt-2">
                      {plugin.intents?.slice(0, 3).map((intent) => (
                        <span
                          key={intent.name}
                          className="text-xs bg-gray-700 text-gray-300 px-2 py-0.5 rounded"
                        >
                          {intent.name}
                        </span>
                      ))}
                      {plugin.intents?.length > 3 && (
                        <span className="text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded">
                          +{plugin.intents.length - 3} more
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {/* Actions */}
                <div className="flex items-center space-x-2">
                  <button
                    onClick={() => setSelectedPlugin(plugin)}
                    className="p-2 text-gray-400 hover:text-primary-400 hover:bg-gray-700 rounded-lg transition-colors"
                    title="View details"
                  >
                    <Info className="w-5 h-5" />
                  </button>
                  {canManage && (
                    <button
                      onClick={() => togglePlugin(plugin)}
                      disabled={togglingPlugin === plugin.name}
                      className={`p-2 rounded-lg transition-colors ${
                        plugin.enabled
                          ? 'text-green-400 hover:text-green-300 hover:bg-green-900/20'
                          : 'text-gray-400 hover:text-green-400 hover:bg-gray-700'
                      }`}
                      title={plugin.enabled ? 'Disable plugin' : 'Enable plugin'}
                    >
                      {togglingPlugin === plugin.name ? (
                        <Loader className="w-5 h-5 animate-spin" />
                      ) : plugin.enabled ? (
                        <Power className="w-5 h-5" />
                      ) : (
                        <PowerOff className="w-5 h-5" />
                      )}
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Plugin Detail Modal */}
      <Modal
        isOpen={!!selectedPlugin}
        onClose={() => setSelectedPlugin(null)}
        title={selectedPlugin?.name || 'Plugin Details'}
        maxWidth="max-w-2xl"
      >
        {selectedPlugin && (
          <div className="space-y-6">
            {/* Basic Info */}
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-gray-500 text-sm">Version</p>
                <p className="text-white">{selectedPlugin.version}</p>
              </div>
              <div>
                <p className="text-gray-500 text-sm">Status</p>
                <p className={selectedPlugin.enabled ? 'text-green-400' : 'text-gray-500'}>
                  {selectedPlugin.enabled ? 'Enabled' : 'Disabled'}
                </p>
              </div>
              {selectedPlugin.author && (
                <div>
                  <p className="text-gray-500 text-sm">Author</p>
                  <p className="text-white">{selectedPlugin.author}</p>
                </div>
              )}
              {selectedPlugin.rate_limit && (
                <div>
                  <p className="text-gray-500 text-sm">Rate Limit</p>
                  <p className="text-white">{selectedPlugin.rate_limit} req/min</p>
                </div>
              )}
            </div>

            {/* Description */}
            <div>
              <p className="text-gray-500 text-sm mb-1">Description</p>
              <p className="text-gray-300">{selectedPlugin.description}</p>
            </div>

            {/* Configuration */}
            {selectedPlugin.has_config && selectedPlugin.config_vars?.length > 0 && (
              <div>
                <div className="flex items-center space-x-2 mb-2">
                  <Settings className="w-4 h-4 text-gray-500" />
                  <p className="text-gray-500 text-sm">Configuration Variables</p>
                </div>
                <div className="bg-gray-850 p-3 rounded-lg">
                  <div className="flex flex-wrap gap-2">
                    {selectedPlugin.config_vars.map((varName) => (
                      <code key={varName} className="text-xs bg-gray-700 text-primary-400 px-2 py-1 rounded">
                        {varName}
                      </code>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* Enable Variable */}
            <div>
              <p className="text-gray-500 text-sm mb-1">Enable Variable</p>
              <code className="text-sm bg-gray-850 text-primary-400 px-2 py-1 rounded">
                {selectedPlugin.enabled_var}=true
              </code>
            </div>

            {/* Intents */}
            {selectedPlugin.intents?.length > 0 && (
              <div>
                <div className="flex items-center space-x-2 mb-2">
                  <Code className="w-4 h-4 text-gray-500" />
                  <p className="text-gray-500 text-sm">Intents ({selectedPlugin.intents.length})</p>
                </div>
                <div className="space-y-2 max-h-64 overflow-y-auto">
                  {selectedPlugin.intents.map((intent) => (
                    <div key={intent.name} className="border border-gray-700 rounded-lg overflow-hidden">
                      <button
                        type="button"
                        onClick={() => toggleIntent(intent.name)}
                        className="w-full flex items-center justify-between p-3 bg-gray-850 hover:bg-gray-800 transition-colors"
                      >
                        <div className="flex items-center space-x-2">
                          {expandedIntents[intent.name] ? (
                            <ChevronDown className="w-4 h-4 text-gray-400" />
                          ) : (
                            <ChevronRight className="w-4 h-4 text-gray-400" />
                          )}
                          <span className="text-white font-medium">{intent.name}</span>
                        </div>
                        {intent.parameters?.length > 0 && (
                          <span className="text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded">
                            {intent.parameters.length} params
                          </span>
                        )}
                      </button>

                      {expandedIntents[intent.name] && (
                        <div className="p-3 bg-gray-900 border-t border-gray-700">
                          <p className="text-gray-400 text-sm mb-3">{intent.description}</p>

                          {intent.parameters?.length > 0 && (
                            <div className="space-y-2">
                              <p className="text-gray-500 text-xs uppercase tracking-wider">Parameters</p>
                              {intent.parameters.map((param) => (
                                <div key={param.name} className="bg-gray-850 p-2 rounded">
                                  <div className="flex items-center space-x-2">
                                    <code className="text-primary-400 text-sm">{param.name}</code>
                                    <span className="text-gray-500 text-xs">({param.type})</span>
                                    {param.required && (
                                      <span className="text-xs bg-red-900/50 text-red-400 px-1 rounded">
                                        required
                                      </span>
                                    )}
                                  </div>
                                  {param.description && (
                                    <p className="text-gray-500 text-xs mt-1">{param.description}</p>
                                  )}
                                  {param.enum && (
                                    <div className="flex flex-wrap gap-1 mt-1">
                                      {param.enum.map((v) => (
                                        <span key={v} className="text-xs bg-gray-700 text-gray-400 px-1 rounded">
                                          {v}
                                        </span>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Close button */}
            <div className="pt-4 border-t border-gray-700">
              <button
                onClick={() => setSelectedPlugin(null)}
                className="w-full btn bg-gray-700 hover:bg-gray-600"
              >
                Close
              </button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
