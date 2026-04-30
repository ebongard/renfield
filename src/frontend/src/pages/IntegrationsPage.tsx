/**
 * Integrations Page
 * Admin page for managing MCP server integrations
 */
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { extractApiError } from '../utils/axios';
import Modal from '../components/Modal';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import Badge from '../components/Badge';
import type { BadgeColor } from '../components/Badge';
import {
  Server,
  RefreshCw,
  AlertCircle,
  Loader,
  Wrench,
  Wifi,
  WifiOff,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import {
  useMcpStatusQuery,
  useMcpToolsQuery,
  useRefreshMcp,
  usePatchActiveTools,
  type Transport,
  type McpStatus,
  type McpTool,
} from '../api/resources/integrations';
import { keys } from '../api/keys';

export default function IntegrationsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const statusQuery = useMcpStatusQuery();
  const toolsQuery = useMcpToolsQuery();
  const mcpStatus: McpStatus | null = statusQuery.data ?? null;
  const mcpTools: McpTool[] = toolsQuery.data ?? [];
  const loading = statusQuery.isLoading || toolsQuery.isLoading;

  const refreshMcp = useRefreshMcp();
  const patchActiveTools = usePatchActiveTools();

  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [expandedServers, setExpandedServers] = useState<Record<string, boolean>>({});
  const [selectedTool, setSelectedTool] = useState<McpTool | null>(null);
  const [togglingTools, setTogglingTools] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (error || success) {
      const timer = setTimeout(() => {
        setError(null);
        setSuccess(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [error, success]);

  const handleRefresh = async () => {
    try {
      await refreshMcp.mutateAsync(undefined);
      setSuccess(t('integrations.refreshSuccess'));
    } catch (err) {
      setError(extractApiError(err, t('integrations.refreshError')));
    }
  };

  const toggleServerExpand = (serverName: string) => {
    setExpandedServers((prev) => ({
      ...prev,
      [serverName]: !prev[serverName],
    }));
  };

  const getToolsForServer = (serverName: string): McpTool[] => {
    return mcpTools.filter((tool) => tool.server === serverName);
  };

  const toggleTool = async (
    serverName: string,
    toolOriginalName: string,
    currentlyActive: boolean,
  ) => {
    const serverTools = getToolsForServer(serverName);
    const toggleKey = `${serverName}.${toolOriginalName}`;
    setTogglingTools((prev) => ({ ...prev, [toggleKey]: true }));

    let newActiveTools: string[];
    if (currentlyActive) {
      newActiveTools = serverTools
        .filter((tt) => tt.active && tt.original_name !== toolOriginalName)
        .map((tt) => tt.original_name);
    } else {
      newActiveTools = [
        ...serverTools.filter((tt) => tt.active).map((tt) => tt.original_name),
        toolOriginalName,
      ];
    }

    // Optimistic update on the tools query
    const toolsQueryKey = [...keys.integrations.list(), 'tools'] as const;
    const previousTools = queryClient.getQueryData<McpTool[]>(toolsQueryKey);
    queryClient.setQueryData<McpTool[]>(toolsQueryKey, (prev) =>
      (prev ?? []).map((tt) =>
        tt.server === serverName && tt.original_name === toolOriginalName
          ? { ...tt, active: !currentlyActive }
          : tt,
      ),
    );

    try {
      await patchActiveTools.mutateAsync({ serverName, activeTools: newActiveTools });
    } catch (err) {
      if (previousTools !== undefined) {
        queryClient.setQueryData(toolsQueryKey, previousTools);
      }
      setError(extractApiError(err, t('integrations.toolToggleError')));
    } finally {
      setTogglingTools((prev) => ({ ...prev, [toggleKey]: false }));
    }
  };

  const resetServerTools = async (serverName: string) => {
    setTogglingTools((prev) => ({ ...prev, [serverName]: true }));
    try {
      await patchActiveTools.mutateAsync({ serverName, activeTools: null });
      // Reload tools after reset
      await toolsQuery.refetch();
      setSuccess(t('integrations.resetDefaults'));
    } catch (err) {
      setError(extractApiError(err, t('integrations.toolToggleError')));
    } finally {
      setTogglingTools((prev) => ({ ...prev, [serverName]: false }));
    }
  };

  const transportBadgeColor = (transport: Transport): BadgeColor => {
    const map: Record<string, BadgeColor> = { stdio: 'blue', streamable_http: 'purple', sse: 'amber' };
    return map[transport] || 'gray';
  };

  const mcpServerCount = mcpStatus?.servers?.length || 0;
  const mcpConnectedCount = mcpStatus?.servers?.filter((s) => s.connected).length || 0;
  const mcpToolCount = mcpStatus?.total_tools || 0;

  if (loading) {
    return (
      <div className="space-y-6">
        <PageHeader icon={Server} title={t('integrations.title')} subtitle={t('integrations.subtitle')} />
        <div className="card text-center py-12">
          <Loader className="w-8 h-8 animate-spin mx-auto text-gray-500 dark:text-gray-400 mb-2" />
          <p className="text-gray-500 dark:text-gray-400">{t('integrations.loading')}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader icon={Server} title={t('integrations.title')} subtitle={t('integrations.subtitle')}>
        <button
          onClick={handleRefresh}
          disabled={refreshMcp.isPending}
          className="btn btn-secondary flex items-center space-x-2"
        >
          <RefreshCw className={`w-4 h-4 ${refreshMcp.isPending ? 'animate-spin' : ''}`} />
          <span>{t('integrations.refresh')}</span>
        </button>
      </PageHeader>

      {error && <Alert variant="error">{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

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

      <div className="card">
        <div className="flex items-center space-x-3 mb-4">
          <Server className="w-6 h-6 text-indigo-500" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('integrations.mcpServers')}
          </h2>
          <Badge color={mcpStatus?.enabled ? 'green' : 'gray'}>
            {mcpStatus?.enabled ? t('integrations.enabled') : t('integrations.disabled')}
          </Badge>
        </div>

        {mcpStatus?.servers && mcpStatus.servers.length > 0 ? (
          <div className="space-y-3">
            {mcpStatus.servers.map((server) => {
              const isExpanded = expandedServers[server.name];
              const serverTools = getToolsForServer(server.name);

              return (
                <div
                  key={server.name}
                  className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden"
                >
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
                      <Badge color={transportBadgeColor(server.transport)}>
                        {server.transport}
                      </Badge>
                    </div>
                    <div className="flex items-center space-x-4">
                      <span className="text-sm text-gray-500 dark:text-gray-400">
                        {server.tool_count}/{server.total_tool_count || server.tool_count} {t('integrations.tools')}
                      </span>
                      <Badge color={server.connected ? 'green' : 'red'}>
                        {server.connected ? t('integrations.online') : t('integrations.offline')}
                      </Badge>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="p-4 bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-700">
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

                      {serverTools.length > 0 ? (
                        <div>
                          <div className="flex items-center justify-between mb-3">
                            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">
                              {t('integrations.availableTools')} ({serverTools.filter((tt) => tt.active).length}/{serverTools.length} {t('integrations.activeTools')})
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

      <Modal
        isOpen={!!selectedTool}
        onClose={() => setSelectedTool(null)}
        title={selectedTool?.original_name || t('integrations.toolDetails')}
        maxWidth="max-w-2xl"
      >
        {selectedTool && (
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('integrations.toolName')}
              </label>
              <code className="block p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm font-mono text-gray-800 dark:text-gray-200">
                {selectedTool.name}
              </code>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('integrations.server')}
              </label>
              <p className="text-gray-900 dark:text-white">{selectedTool.server}</p>
            </div>

            {selectedTool.description && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t('integrations.description')}
                </label>
                <p className="text-gray-700 dark:text-gray-300">{selectedTool.description}</p>
              </div>
            )}

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
