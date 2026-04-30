import { QueryClient } from '@tanstack/react-query';
import type { AxiosError } from 'axios';

const FIVE_MINUTES_MS = 5 * 60 * 1000;
const THIRTY_SECONDS_MS = 30 * 1000;

function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) return false;
  const status = (error as AxiosError | undefined)?.response?.status;
  if (status !== undefined && status >= 400 && status < 500) return false;
  return true;
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: THIRTY_SECONDS_MS,
      gcTime: FIVE_MINUTES_MS,
      refetchOnWindowFocus: false,
      retry: shouldRetryQuery,
    },
    mutations: {
      retry: 0,
    },
  },
});
