import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type Transport = 'stdio' | 'streamable_http' | 'sse' | string;

export interface McpServer {
  name: string;
  connected: boolean;
  transport: Transport;
  tool_count: number;
  total_tool_count?: number;
  last_error?: string;
}

export interface McpStatus {
  enabled: boolean;
  servers: McpServer[];
  total_tools: number;
}

export interface McpTool {
  name: string;
  original_name: string;
  server: string;
  active: boolean;
  description?: string;
  input_schema?: Record<string, unknown>;
}

const EMPTY_STATUS: McpStatus = { enabled: false, servers: [], total_tools: 0 };

async function fetchMcpStatus(): Promise<McpStatus> {
  try {
    const response = await apiClient.get<McpStatus>('/api/mcp/status');
    return response.data;
  } catch {
    return EMPTY_STATUS;
  }
}

async function fetchMcpTools(): Promise<McpTool[]> {
  try {
    const response = await apiClient.get<{ tools: McpTool[] }>('/api/mcp/tools');
    return response.data.tools ?? [];
  } catch {
    return [];
  }
}

async function refreshMcpRequest(): Promise<void> {
  await apiClient.post('/api/mcp/refresh', {});
}

interface PatchActiveToolsInput {
  serverName: string;
  activeTools: string[] | null;
}

async function patchActiveToolsRequest({
  serverName,
  activeTools,
}: PatchActiveToolsInput): Promise<McpStatus> {
  const response = await apiClient.patch<McpStatus>(
    `/api/mcp/servers/${encodeURIComponent(serverName)}/tools`,
    { active_tools: activeTools },
  );
  return response.data;
}

export function useMcpStatusQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.integrations.list(), 'status'] as const,
      queryFn: fetchMcpStatus,
      staleTime: STALE.DEFAULT,
    },
    'integrations.loadError',
  );
}

export function useMcpToolsQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.integrations.list(), 'tools'] as const,
      queryFn: fetchMcpTools,
      staleTime: STALE.DEFAULT,
    },
    'integrations.loadError',
  );
}

export function useRefreshMcp() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: refreshMcpRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.integrations.all });
      },
    },
    'integrations.refreshError',
  );
}

export function usePatchActiveTools() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: patchActiveToolsRequest,
      onSuccess: (data) => {
        queryClient.setQueryData([...keys.integrations.list(), 'status'], data);
        queryClient.invalidateQueries({ queryKey: [...keys.integrations.list(), 'tools'] });
      },
    },
    'integrations.toolToggleError',
  );
}
