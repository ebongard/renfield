/**
 * Intents Overview Page
 *
 * Admin page showing all available intents and integration status.
 * Useful for debugging and understanding system capabilities.
 */
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import {
  Zap, Loader, RefreshCw, CheckCircle, XCircle,
  ChevronDown, ChevronRight, Home, Brain, Camera, Workflow,
  MessageSquare, Puzzle, Server, Code,
} from 'lucide-react';

const INTEGRATION_ICONS: Record<string, LucideIcon> = {
  homeassistant: Home,
  knowledge: Brain,
  camera: Camera,
  n8n: Workflow,
  general: MessageSquare,
};

interface IntentParameter {
  name: string;
  required?: boolean;
}

interface IntentDescriptor {
  name: string;
  description: string;
  parameters: IntentParameter[];
}

interface Integration {
  name: string;
  title: string;
  enabled: boolean;
  intent_count: number;
  intents: IntentDescriptor[];
}

interface PluginIntent {
  name: string;
  description: string;
  plugin: string;
}

interface McpToolIntent {
  intent: string;
  description: string;
  server?: string;
}

interface IntentsStatus {
  total_intents: number;
  enabled_integrations: number;
  integrations: Integration[];
  plugins?: PluginIntent[];
  mcp_tools?: McpToolIntent[];
}

interface PromptData {
  language: string;
  intent_types: string;
  examples?: string;
}

export default function IntentsPage() {
  const { t, i18n } = useTranslation();
  const { getAccessToken } = useAuth();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<IntentsStatus | null>(null);
  const [expandedIntegrations, setExpandedIntegrations] = useState<Set<string>>(new Set());
  const [showPrompt, setShowPrompt] = useState(false);
  const [promptData, setPromptData] = useState<PromptData | null>(null);

  // Load intent status
  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const token = await getAccessToken();
      const lang = i18n.language || 'de';
      const response = await apiClient.get<IntentsStatus>(`/api/intents/status?lang=${lang}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
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
      const response = await apiClient.get<PromptData>(`/api/intents/prompt?lang=${lang}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      setPromptData(response.data);
      setShowPrompt(true);
    } catch (err) {
      console.error('Failed to load prompt:', err);
    }
  };

  // Toggle integration expansion
  const toggleIntegration = (name: string) => {
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
        <Alert variant="error">
          <span className="flex items-center gap-3">
            <span className="flex-1">{error}</span>
            <button
              onClick={loadStatus}
              className="btn-icon btn-icon-ghost ml-auto"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
          </span>
        </Alert>
      </div>
    );
  }

  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="mb-6">
        <PageHeader icon={Zap} title={t('intents.title')} subtitle={t('intents.subtitle')}>
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
        </PageHeader>
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
                                  <Badge
                                    key={param.name}
                                    color={param.required ? 'red' : 'gray'}
                                  >
                                    {param.name}{param.required && '*'}
                                  </Badge>
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
              <h3 className="text-base font-semibold text-gray-900 dark:text-white">
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
