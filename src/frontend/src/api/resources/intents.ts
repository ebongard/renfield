import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export interface IntentParameter {
  name: string;
  required?: boolean;
}

export interface IntentDescriptor {
  name: string;
  description: string;
  parameters: IntentParameter[];
}

export interface Integration {
  name: string;
  title: string;
  enabled: boolean;
  intent_count: number;
  intents: IntentDescriptor[];
}

export interface PluginIntent {
  name: string;
  description: string;
  plugin: string;
}

export interface McpToolIntent {
  intent: string;
  description: string;
  server?: string;
}

export interface IntentsStatus {
  total_intents: number;
  enabled_integrations: number;
  integrations: Integration[];
  plugins?: PluginIntent[];
  mcp_tools?: McpToolIntent[];
}

export interface PromptData {
  language: string;
  intent_types: string;
  examples?: string;
}

async function fetchIntentStatus(lang: string): Promise<IntentsStatus> {
  const response = await apiClient.get<IntentsStatus>(`/api/intents/status?lang=${lang}`);
  return response.data;
}

async function fetchIntentPrompt(lang: string): Promise<PromptData> {
  const response = await apiClient.get<PromptData>(`/api/intents/prompt?lang=${lang}`);
  return response.data;
}

export function useIntentsQuery(lang: string) {
  return useApiQuery(
    {
      queryKey: ['intents', 'status', lang] as const,
      queryFn: () => fetchIntentStatus(lang),
      staleTime: STALE.DEFAULT,
    },
    'intents.failedToLoad',
  );
}

export function useIntentPromptQuery(lang: string, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: ['intents', 'prompt', lang] as const,
      queryFn: () => fetchIntentPrompt(lang),
      staleTime: STALE.CONFIG,
      enabled,
    },
    'intents.failedToLoad',
  );
}

// Re-export for invalidation symmetry with the keys factory
export { keys as intentKeys } from '../keys';
