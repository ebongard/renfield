/**
 * Intents Overview Page
 *
 * Admin page showing all available intents and integration status.
 * Useful for debugging and understanding system capabilities.
 */
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import {
  Zap, Loader, AlertCircle, RefreshCw, CheckCircle, XCircle,
  ChevronDown, ChevronRight, Home, Brain, Camera, Workflow,
  MessageSquare, Puzzle, Server, Code
} from 'lucide-react';

// Icon mapping for integrations
const INTEGRATION_ICONS = {
  homeassistant: Home,
  knowledge: Brain,
  camera: Camera,
  n8n: Workflow,
  general: MessageSquare,
};

export default function IntentsPage() {
  const { t, i18n } = useTranslation();
  const { getAccessToken } = useAuth();

  // State
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState(null);
  const [expandedIntegrations, setExpandedIntegrations] = useState(new Set());
  const [showPrompt, setShowPrompt] = useState(false);
  const [promptData, setPromptData] = useState(null);

  // Load intent status
  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const token = await getAccessToken();
      const lang = i18n.language || 'de';
      const response = await apiClient.get(`/api/intents/status?lang=${lang}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      });

      setStatus(response.data);

      // Auto-expand enabled integrations
      const enabled = new Set(
        response.data.integrations
          .filter(i => i.enabled)
          .map(i => i.name)
      );
      setExpandedIntegrations(enabled);
    } catch (err) {
      console.error('Failed to load intent status:', err);
      setError(t('intents.failedToLoad'));
    } finally {
      setLoading(false);
    }
  }, [getAccessToken, i18n.language, t]);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  // Load prompt for debugging
  const loadPrompt = async () => {
    try {
      const token = await getAccessToken();
      const lang = i18n.language || 'de';
      const response = await apiClient.get(`/api/intents/prompt?lang=${lang}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      });
      setPromptData(response.data);
      setShowPrompt(true);
    } catch (err) {
      console.error('Failed to load prompt:', err);
    }
  };

  // Toggle integration expansion
  const toggleIntegration = (name) => {
    setExpandedIntegrations(prev => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  };

  // Render loading state
  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  // Render error state
  if (error) {
    return (
      <div className="p-4">
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 flex items-center gap-3">
          <AlertCircle className="w-5 h-5 text-red-500" />
          <span className="text-red-700 dark:text-red-300">{error}</span>
          <button
            onClick={loadStatus}
            className="ml-auto text-red-600 hover:text-red-800 dark:text-red-400"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Zap className="w-8 h-8 text-yellow-500" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
              {t('intents.title')}
            </h1>
            <p className="text-gray-500 dark:text-gray-400">
              {t('intents.subtitle')}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={loadPrompt}
            className="btn btn-secondary flex items-center gap-2"
          >
            <Code className="w-4 h-4" />
            {t('intents.viewPrompt')}
          </button>
          <button
            onClick={loadStatus}
            className="btn btn-secondary flex items-center gap-2"
          >
            <RefreshCw className="w-4 h-4" />
            {t('common.refresh')}
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <div className="card p-4">
          <div className="text-3xl font-bold text-blue-600 dark:text-blue-400">
            {status?.total_intents || 0}
          </div>
          <div className="text-sm text-gray-500 dark:text-gray-400">
            {t('intents.totalIntents')}
          </div>
        </div>
        <div className="card p-4">
          <div className="text-3xl font-bold text-green-600 dark:text-green-400">
            {status?.enabled_integrations || 0}
          </div>
          <div className="text-sm text-gray-500 dark:text-gray-400">
            {t('intents.enabledIntegrations')}
          </div>
        </div>
        <div className="card p-4">
          <div className="text-3xl font-bold text-purple-600 dark:text-purple-400">
            {status?.plugins?.length || 0}
          </div>
          <div className="text-sm text-gray-500 dark:text-gray-400">
            {t('intents.pluginIntents')}
          </div>
        </div>
        <div className="card p-4">
          <div className="text-3xl font-bold text-orange-600 dark:text-orange-400">
            {status?.mcp_tools?.length || 0}
          </div>
          <div className="text-sm text-gray-500 dark:text-gray-400">
            {t('intents.mcpTools')}
          </div>
        </div>
      </div>

      {/* Integrations List */}
      <div className="space-y-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
          {t('intents.coreIntegrations')}
        </h2>

        {status?.integrations?.map((integration) => {
          const Icon = INTEGRATION_ICONS[integration.name] || Zap;
          const isExpanded = expandedIntegrations.has(integration.name);

          return (
            <div
              key={integration.name}
              className={`card overflow-hidden ${
                !integration.enabled ? 'opacity-60' : ''
              }`}
            >
              {/* Integration Header */}
              <button
                onClick={() => toggleIntegration(integration.name)}
                className="w-full p-4 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <Icon className={`w-5 h-5 ${
                    integration.enabled ? 'text-blue-500' : 'text-gray-400'
                  }`} />
                  <div className="text-left">
                    <div className="font-medium text-gray-900 dark:text-white">
                      {integration.title}
                    </div>
                    <div className="text-sm text-gray-500 dark:text-gray-400">
                      {integration.intent_count} {t('intents.intentsAvailable')}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  {integration.enabled ? (
                    <span className="flex items-center gap-1 text-green-600 dark:text-green-400 text-sm">
                      <CheckCircle className="w-4 h-4" />
                      {t('intents.enabled')}
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 text-gray-400 text-sm">
                      <XCircle className="w-4 h-4" />
                      {t('intents.disabled')}
                    </span>
                  )}
                  {isExpanded ? (
                    <ChevronDown className="w-5 h-5 text-gray-400" />
                  ) : (
                    <ChevronRight className="w-5 h-5 text-gray-400" />
                  )}
                </div>
              </button>

              {/* Intent List */}
              {isExpanded && (
                <div className="border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-gray-200 dark:border-gray-700">
                        <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                          {t('intents.intentName')}
                        </th>
                        <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                          {t('intents.description')}
                        </th>
                        <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                          {t('intents.parameters')}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {integration.intents.map((intent) => (
                        <tr
                          key={intent.name}
                          className="border-b border-gray-200 dark:border-gray-700 last:border-0"
                        >
                          <td className="p-3 font-mono text-blue-600 dark:text-blue-400">
                            {intent.name}
                          </td>
                          <td className="p-3 text-gray-700 dark:text-gray-300">
                            {intent.description}
                          </td>
                          <td className="p-3">
                            {intent.parameters.length > 0 ? (
                              <div className="flex flex-wrap gap-1">
                                {intent.parameters.map((param) => (
                                  <span
                                    key={param.name}
                                    className={`px-2 py-0.5 rounded text-xs ${
                                      param.required
                                        ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300'
                                        : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
                                    }`}
                                    title={param.description}
                                  >
                                    {param.name}{param.required && '*'}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              <span className="text-gray-400">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}

        {/* Plugins Section */}
        {status?.plugins?.length > 0 && (
          <>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white mt-8">
              <Puzzle className="w-5 h-5 inline-block mr-2 text-purple-500" />
              {t('intents.plugins')}
            </h2>
            <div className="card">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-200 dark:border-gray-700">
                    <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                      {t('intents.intentName')}
                    </th>
                    <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                      {t('intents.description')}
                    </th>
                    <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                      {t('intents.plugin')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {status.plugins.map((plugin) => (
                    <tr
                      key={plugin.name}
                      className="border-b border-gray-200 dark:border-gray-700 last:border-0"
                    >
                      <td className="p-3 font-mono text-purple-600 dark:text-purple-400">
                        {plugin.name}
                      </td>
                      <td className="p-3 text-gray-700 dark:text-gray-300">
                        {plugin.description}
                      </td>
                      <td className="p-3 text-gray-500 dark:text-gray-400">
                        {plugin.plugin}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        {/* MCP Tools Section */}
        {status?.mcp_tools?.length > 0 && (
          <>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white mt-8">
              <Server className="w-5 h-5 inline-block mr-2 text-orange-500" />
              {t('intents.mcpTools')}
            </h2>
            <div className="card">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-200 dark:border-gray-700">
                    <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                      {t('intents.intentName')}
                    </th>
                    <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                      {t('intents.description')}
                    </th>
                    <th className="text-left p-3 font-medium text-gray-600 dark:text-gray-300">
                      {t('intents.server')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {status.mcp_tools.map((tool, idx) => (
                    <tr
                      key={idx}
                      className="border-b border-gray-200 dark:border-gray-700 last:border-0"
                    >
                      <td className="p-3 font-mono text-orange-600 dark:text-orange-400">
                        {tool.intent}
                      </td>
                      <td className="p-3 text-gray-700 dark:text-gray-300">
                        {tool.description}
                      </td>
                      <td className="p-3 text-gray-500 dark:text-gray-400">
                        {tool.server || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {/* Prompt Modal */}
      {showPrompt && promptData && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-4xl w-full max-h-[80vh] overflow-hidden flex flex-col">
            <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                {t('intents.generatedPrompt')} ({promptData.language.toUpperCase()})
              </h3>
              <button
                onClick={() => setShowPrompt(false)}
                className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
              >
                <XCircle className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4 overflow-auto flex-1">
              <h4 className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-2">
                {t('intents.intentTypes')}
              </h4>
              <pre className="bg-gray-100 dark:bg-gray-900 p-4 rounded-lg text-xs overflow-x-auto whitespace-pre-wrap font-mono text-gray-800 dark:text-gray-200 mb-4">
                {promptData.intent_types}
              </pre>
              {promptData.examples && (
                <>
                  <h4 className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-2">
                    {t('intents.examples')}
                  </h4>
                  <pre className="bg-gray-100 dark:bg-gray-900 p-4 rounded-lg text-xs overflow-x-auto whitespace-pre-wrap font-mono text-gray-800 dark:text-gray-200">
                    {promptData.examples}
                  </pre>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
