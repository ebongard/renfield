/**
 * Integrations Page
 * Admin page for managing MCP server integrations
 */
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import Modal from '../components/Modal';
import {
  Server,
  RefreshCw,
  AlertCircle,
  CheckCircle,
  Loader,
  Wrench,
  Wifi,
  WifiOff,
  ChevronDown,
  ChevronRight,
  Info,
} from 'lucide-react';

export default function IntegrationsPage() {
  const { t } = useTranslation();
  const { getAccessToken } = useAuth();

  // State
  const [mcpStatus, setMcpStatus] = useState(null);
  const [mcpTools, setMcpTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // UI State
  const [expandedServers, setExpandedServers] = useState({});
  const [selectedTool, setSelectedTool] = useState(null);
  const [togglingTools, setTogglingTools] = useState({});

  // Load data on mount
  useEffect(() => {
    loadData();
  }, []);

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

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const token = getAccessToken();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};

      // Load MCP status and tools in parallel
      const [statusRes, toolsRes] = await Promise.all([
        apiClient.get('/api/mcp/status', { headers }).catch(() => ({ data: { enabled: false, servers: [], total_tools: 0 } })),
        apiClient.get('/api/mcp/tools', { headers }).catch(() => ({ data: { tools: [] } })),
      ]);

      setMcpStatus(statusRes.data);
      setMcpTools(toolsRes.data.tools || []);
    } catch (err) {
      setError(err.response?.data?.detail || t('integrations.loadError'));
    } finally {
      setLoading(false);
    }
  }, [getAccessToken, t]);

  const handleRefresh = async () => {
    try {
      setRefreshing(true);
      const token = getAccessToken();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};

      // Refresh MCP connections
      await apiClient.post('/api/mcp/refresh', {}, { headers });
      setSuccess(t('integrations.refreshSuccess'));
      await loadData();
    } catch (err) {
      setError(err.response?.data?.detail || t('integrations.refreshError'));
    } finally {
      setRefreshing(false);
    }
  };

  const toggleServerExpand = (serverName) => {
    setExpandedServers(prev => ({
      ...prev,
      [serverName]: !prev[serverName]
    }));
  };

  const getToolsForServer = (serverName) => {
    return mcpTools.filter(tool => tool.server === serverName);
  };

  const toggleTool = async (serverName, toolOriginalName, currentlyActive) => {
    const serverTools = getToolsForServer(serverName);
    const toggleKey = `${serverName}.${toolOriginalName}`;
    setTogglingTools(prev => ({ ...prev, [toggleKey]: true }));

    // Build new active list
    let newActiveTools;
    if (currentlyActive) {
      // Deactivate: keep all currently active except this one
      newActiveTools = serverTools
        .filter(t => t.active && t.original_name !== toolOriginalName)
        .map(t => t.original_name);
    } else {
      // Activate: add this one to currently active
      newActiveTools = [
        ...serverTools.filter(t => t.active).map(t => t.original_name),
        toolOriginalName,
      ];
    }

    // Optimistic update
    setMcpTools(prev => prev.map(t =>
      t.server === serverName && t.original_name === toolOriginalName
        ? { ...t, active: !currentlyActive }
        : t
    ));

    try {
      const token = getAccessToken();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await apiClient.patch(
        `/api/mcp/servers/${encodeURIComponent(serverName)}/tools`,
        { active_tools: newActiveTools },
        { headers }
      );
      setMcpStatus(res.data);
    } catch (err) {
      // Revert optimistic update
      setMcpTools(prev => prev.map(t =>
        t.server === serverName && t.original_name === toolOriginalName
          ? { ...t, active: currentlyActive }
          : t
      ));
      setError(err.response?.data?.detail || t('integrations.toolToggleError'));
    } finally {
      setTogglingTools(prev => ({ ...prev, [toggleKey]: false }));
    }
  };

  const resetServerTools = async (serverName) => {
    setTogglingTools(prev => ({ ...prev, [serverName]: true }));
    try {
      const token = getAccessToken();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await apiClient.patch(
        `/api/mcp/servers/${encodeURIComponent(serverName)}/tools`,
        { active_tools: null },
        { headers }
      );
      setMcpStatus(res.data);
      await loadData();
      setSuccess(t('integrations.resetDefaults'));
    } catch (err) {
      setError(err.response?.data?.detail || t('integrations.toolToggleError'));
    } finally {
      setTogglingTools(prev => ({ ...prev, [serverName]: false }));
    }
  };

  const getTransportBadge = (transport) => {
    const colors = {
      stdio: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
      streamable_http: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
      sse: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
    };
    return colors[transport] || 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
  };

  // Calculate stats
  const mcpServerCount = mcpStatus?.servers?.length || 0;
  const mcpConnectedCount = mcpStatus?.servers?.filter(s => s.connected).length || 0;
  const mcpToolCount = mcpStatus?.total_tools || 0;

  // Loading state
  if (loading) {
    return (
      <div className="space-y-6">
        <div className="card">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">
            {t('integrations.title')}
          </h1>
          <p className="text-gray-500 dark:text-gray-400">{t('integrations.subtitle')}</p>
        </div>
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
          <p className="text-gray-500 dark:text-gray-400">{t('integrations.loading')}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">
              {t('integrations.title')}
            </h1>
            <p className="text-gray-500 dark:text-gray-400">{t('integrations.subtitle')}</p>
          </div>
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className="btn btn-secondary flex items-center space-x-2"
          >
            <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
            <span>{t('integrations.refresh')}</span>
          </button>
        </div>
      </div>

      {/* Alerts */}
      {error && (
        <div className="card bg-red-100 dark:bg-red-900/20 border-red-300 dark:border-red-700">
          <div className="flex items-center space-x-3">
            <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0" />
            <p className="text-red-700 dark:text-red-400">{error}</p>
          </div>
        </div>
      )}

      {success && (
        <div className="card bg-green-100 dark:bg-green-900/20 border-green-300 dark:border-green-700">
          <div className="flex items-center space-x-3">
            <CheckCircle className="w-5 h-5 text-green-500 flex-shrink-0" />
            <p className="text-green-700 dark:text-green-400">{success}</p>
          </div>
        </div>
      )}

      {/* Overall Stats */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <div className="card text-center py-4">
          <p className="text-2xl font-bold text-gray-900 dark:text-white">{mcpServerCount}</p>
          <p className="text-gray-500 dark:text-gray-400 text-sm">{t('integrations.mcpServers')}</p>
        </div>
        <div className="card text-center py-4">
          <p className="text-2xl font-bold text-green-600 dark:text-green-400">{mcpConnectedCount}</p>
          <p className="text-gray-500 dark:text-gray-400 text-sm">{t('integrations.connected')}</p>
        </div>
        <div className="card text-center py-4">
          <p className="text-2xl font-bold text-indigo-600 dark:text-indigo-400">{mcpToolCount}</p>
          <p className="text-gray-500 dark:text-gray-400 text-sm">{t('integrations.mcpTools')}</p>
        </div>
      </div>

      {/* MCP Servers Section */}
      <div className="card">
        <div className="flex items-center space-x-3 mb-4">
          <Server className="w-6 h-6 text-indigo-500" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('integrations.mcpServers')}
          </h2>
          {mcpStatus?.enabled ? (
            <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300">
              {t('integrations.enabled')}
            </span>
          ) : (
            <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300">
              {t('integrations.disabled')}
            </span>
          )}
        </div>

        {/* Server List */}
        {mcpStatus?.servers?.length > 0 ? (
          <div className="space-y-3">
            {mcpStatus.servers.map((server) => {
              const isExpanded = expandedServers[server.name];
              const serverTools = getToolsForServer(server.name);

              return (
                <div
                  key={server.name}
                  className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden"
                >
                  {/* Server Header */}
                  <div
                    className="flex items-center justify-between p-4 bg-gray-50 dark:bg-gray-800 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-750 transition-colors"
                    onClick={() => toggleServerExpand(server.name)}
                  >
                    <div className="flex items-center space-x-3">
                      {isExpanded ? (
                        <ChevronDown className="w-5 h-5 text-gray-500" />
                      ) : (
                        <ChevronRight className="w-5 h-5 text-gray-500" />
                      )}
                      {server.connected ? (
                        <Wifi className="w-5 h-5 text-green-500" />
                      ) : (
                        <WifiOff className="w-5 h-5 text-red-500" />
                      )}
                      <span className="font-medium text-gray-900 dark:text-white">
                        {server.name}
                      </span>
                      <span className={`px-2 py-0.5 text-xs font-medium rounded-full ${getTransportBadge(server.transport)}`}>
                        {server.transport}
                      </span>
                    </div>
                    <div className="flex items-center space-x-4">
                      <span className="text-sm text-gray-500 dark:text-gray-400">
                        {server.tool_count}/{server.total_tool_count || server.tool_count} {t('integrations.tools')}
                      </span>
                      {server.connected ? (
                        <span className="px-2 py-1 text-xs font-medium rounded-full bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300">
                          {t('integrations.online')}
                        </span>
                      ) : (
                        <span className="px-2 py-1 text-xs font-medium rounded-full bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300">
                          {t('integrations.offline')}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Server Details */}
                  {isExpanded && (
                    <div className="p-4 bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-700">
                      {/* Error if any */}
                      {server.last_error && (
                        <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 rounded-lg border border-red-200 dark:border-red-800">
                          <div className="flex items-start space-x-2">
                            <AlertCircle className="w-4 h-4 text-red-500 mt-0.5 flex-shrink-0" />
                            <div>
                              <p className="text-sm font-medium text-red-800 dark:text-red-300">
                                {t('integrations.lastError')}
                              </p>
                              <p className="text-sm text-red-700 dark:text-red-400 mt-1 font-mono">
                                {server.last_error}
                              </p>
                            </div>
                          </div>
                        </div>
                      )}

                      {/* Tools List */}
                      {serverTools.length > 0 ? (
                        <div>
                          <div className="flex items-center justify-between mb-3">
                            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">
                              {t('integrations.availableTools')} ({serverTools.filter(t => t.active).length}/{serverTools.length} {t('integrations.activeTools')})
                            </h4>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                resetServerTools(server.name);
                              }}
                              disabled={togglingTools[server.name]}
                              className="text-xs text-gray-500 hover:text-primary-500 dark:text-gray-400 dark:hover:text-primary-400 transition-colors"
                            >
                              {togglingTools[server.name] ? (
                                <Loader className="w-3 h-3 animate-spin inline mr-1" />
                              ) : null}
                              {t('integrations.resetDefaults')}
                            </button>
                          </div>
                          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
                            {serverTools.map((tool) => {
                              const toggleKey = `${server.name}.${tool.original_name}`;
                              const isToggling = togglingTools[toggleKey];
                              return (
                                <div
                                  key={tool.name}
                                  className={`flex items-center p-2 rounded-lg transition-colors ${
                                    tool.active
                                      ? 'bg-gray-50 dark:bg-gray-800 hover:bg-gray-100 dark:hover:bg-gray-750'
                                      : 'bg-gray-50/50 dark:bg-gray-800/50 opacity-50'
                                  }`}
                                >
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      toggleTool(server.name, tool.original_name, tool.active);
                                    }}
                                    disabled={isToggling}
                                    className={`mr-2 relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                                      tool.active ? 'bg-indigo-500' : 'bg-gray-300 dark:bg-gray-600'
                                    }`}
                                    title={tool.active ? t('integrations.toolActive') : t('integrations.toolInactive')}
                                  >
                                    <span
                                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                                        tool.active ? 'translate-x-4' : 'translate-x-0'
                                      }`}
                                    />
                                  </button>
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setSelectedTool(tool);
                                    }}
                                    className="flex items-center space-x-2 text-left flex-1 min-w-0"
                                  >
                                    <Wrench className={`w-4 h-4 flex-shrink-0 ${
                                      tool.active ? 'text-indigo-500' : 'text-gray-400 dark:text-gray-600'
                                    }`} />
                                    <span className={`text-sm truncate ${
                                      tool.active
                                        ? 'text-gray-700 dark:text-gray-300'
                                        : 'text-gray-400 dark:text-gray-500'
                                    }`}>
                                      {tool.original_name}
                                    </span>
                                  </button>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      ) : (
                        <p className="text-sm text-gray-500 dark:text-gray-400 italic">
                          {t('integrations.noTools')}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="text-center py-8">
            <Server className="w-12 h-12 mx-auto text-gray-400 dark:text-gray-600 mb-4" />
            <p className="text-gray-500 dark:text-gray-400">{t('integrations.noServers')}</p>
            <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
              {t('integrations.mcpDisabledHint')}
            </p>
          </div>
        )}
      </div>

      {/* MCP Tool Detail Modal */}
      <Modal
        isOpen={!!selectedTool}
        onClose={() => setSelectedTool(null)}
        title={selectedTool?.original_name || t('integrations.toolDetails')}
        maxWidth="max-w-2xl"
      >
        {selectedTool && (
          <div className="space-y-4">
            {/* Tool Name */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('integrations.toolName')}
              </label>
              <code className="block p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm font-mono text-gray-800 dark:text-gray-200">
                {selectedTool.name}
              </code>
            </div>

            {/* Server */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('integrations.server')}
              </label>
              <p className="text-gray-900 dark:text-white">{selectedTool.server}</p>
            </div>

            {/* Description */}
            {selectedTool.description && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t('integrations.description')}
                </label>
                <p className="text-gray-700 dark:text-gray-300">{selectedTool.description}</p>
              </div>
            )}

            {/* Input Schema */}
            {selectedTool.input_schema && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t('integrations.inputSchema')}
                </label>
                <pre className="p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-xs font-mono text-gray-800 dark:text-gray-200 overflow-x-auto max-h-64">
                  {JSON.stringify(selectedTool.input_schema, null, 2)}
                </pre>
              </div>
            )}

            {/* Close Button */}
            <div className="pt-4 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => setSelectedTool(null)}
                className="w-full btn btn-secondary"
              >
                {t('common.close')}
              </button>
            </div>
          </div>
        )}
      </Modal>

    </div>
  );
}
