import {
  useQuery,
  useMutation,
  type UseQueryOptions,
  type UseMutationOptions,
  type QueryKey,
} from '@tanstack/react-query';
import type { AxiosError } from 'axios';
import { useTranslation } from 'react-i18next';

import { extractApiError, extractFieldErrors } from '../utils/axios';

type ApiError = AxiosError<{ detail?: unknown }>;

interface UseApiQueryResult<T> {
  data: T | undefined;
  isLoading: boolean;
  isFetching: boolean;
  isError: boolean;
  error: ApiError | null;
  errorMessage: string | null;
  refetch: () => Promise<unknown>;
}

interface UseApiMutationResult<TVars, TData> {
  mutate: (vars: TVars) => void;
  mutateAsync: (vars: TVars) => Promise<TData>;
  isPending: boolean;
  isError: boolean;
  error: ApiError | null;
  errorMessage: string | null;
  fieldErrors: Record<string, string>;
  reset: () => void;
}

/**
 * Thin wrapper over `useQuery` that resolves the raw `AxiosError` to an
 * i18n-aware `errorMessage` string via `extractApiError`. Pages drop their
 * try/catch + setError + t() boilerplate and render `errorMessage` directly.
 */
export function useApiQuery<T, TKey extends QueryKey = QueryKey>(
  options: UseQueryOptions<T, ApiError, T, TKey>,
  fallbackI18nKey: string,
): UseApiQueryResult<T> {
  const { t } = useTranslation();
  const result = useQuery<T, ApiError, T, TKey>(options);
  const errorMessage = result.error
    ? extractApiError(result.error, t(fallbackI18nKey))
    : null;
  return {
    data: result.data,
    isLoading: result.isLoading,
    isFetching: result.isFetching,
    isError: result.isError,
    error: result.error ?? null,
    errorMessage,
    refetch: result.refetch,
  };
}

/**
 * Thin wrapper over `useMutation`. Exposes `errorMessage` (formatted), and
 * `fieldErrors` (per-field 422 detail map from `extractFieldErrors`) so form
 * pages don't need to bypass the wrapper to read raw errors. Raw `AxiosError`
 * is still on `error` for edge cases.
 */
export function useApiMutation<TData, TVars>(
  options: UseMutationOptions<TData, ApiError, TVars>,
  fallbackI18nKey: string,
): UseApiMutationResult<TVars, TData> {
  const { t } = useTranslation();
  const result = useMutation<TData, ApiError, TVars>(options);
  const errorMessage = result.error
    ? extractApiError(result.error, t(fallbackI18nKey))
    : null;
  const fieldErrors = result.error ? extractFieldErrors(result.error) : {};
  return {
    mutate: result.mutate,
    mutateAsync: result.mutateAsync,
    isPending: result.isPending,
    isError: result.isError,
    error: result.error ?? null,
    errorMessage,
    fieldErrors,
    reset: result.reset,
  };
}
