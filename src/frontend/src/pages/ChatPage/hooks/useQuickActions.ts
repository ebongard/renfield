import { useState, useCallback } from 'react';
import type { AxiosError } from 'axios';
import apiClient from '../../../utils/axios';

type ActionKind = 'indexing' | 'paperless' | 'email';

interface ActionResult {
  type: ActionKind;
  success: boolean;
  message: string;
}

type ActionLoading = Record<string, ActionKind>;

interface ActionResponse {
  message: string;
}

const detailFromError = (error: unknown, fallback: string): string => {
  const axiosErr = error as AxiosError<{ detail?: string }> | undefined;
  return axiosErr?.response?.data?.detail ?? fallback;
};

export function useQuickActions() {
  const [actionLoading, setActionLoading] = useState<ActionLoading>({});
  const [actionResult, setActionResult] = useState<ActionResult | null>(null);

  const indexToKb = useCallback(async (uploadId: string, knowledgeBaseId: string | number) => {
    setActionLoading((prev) => ({ ...prev, [uploadId]: 'indexing' }));
    try {
      const response = await apiClient.post<ActionResponse>(`/api/chat/upload/${uploadId}/index`, {
        knowledge_base_id: knowledgeBaseId,
      });
      setActionResult({ type: 'indexing', success: true, message: response.data.message });
    } catch (error) {
      const axiosErr = error as AxiosError<{ detail?: string }> | undefined;
      const status = axiosErr?.response?.status;
      const detail = detailFromError(error, 'Unknown error');
      if (status === 409) {
        // Already indexed (e.g. by auto-index) — treat as success
        setActionResult({ type: 'indexing', success: true, message: detail });
      } else {
        setActionResult({ type: 'indexing', success: false, message: detail });
      }
    } finally {
      setActionLoading((prev) => {
        const next = { ...prev };
        delete next[uploadId];
        return next;
      });
    }
  }, []);

  const sendToPaperless = useCallback(async (uploadId: string) => {
    setActionLoading((prev) => ({ ...prev, [uploadId]: 'paperless' }));
    try {
      const response = await apiClient.post<ActionResponse>(`/api/chat/upload/${uploadId}/paperless`);
      setActionResult({ type: 'paperless', success: true, message: response.data.message });
    } catch (error) {
      setActionResult({ type: 'paperless', success: false, message: detailFromError(error, 'Unknown error') });
    } finally {
      setActionLoading((prev) => {
        const next = { ...prev };
        delete next[uploadId];
        return next;
      });
    }
  }, []);

  const sendViaEmail = useCallback(
    async (uploadId: string, to: string, subject: string, body: string) => {
      setActionLoading((prev) => ({ ...prev, [uploadId]: 'email' }));
      try {
        const response = await apiClient.post<ActionResponse>(`/api/chat/upload/${uploadId}/email`, {
          to,
          subject,
          body,
        });
        setActionResult({ type: 'email', success: true, message: response.data.message });
      } catch (error) {
        setActionResult({ type: 'email', success: false, message: detailFromError(error, 'Unknown error') });
      } finally {
        setActionLoading((prev) => {
          const next = { ...prev };
          delete next[uploadId];
          return next;
        });
      }
    },
    [],
  );

  const clearResult = useCallback(() => {
    setActionResult(null);
  }, []);

  return { actionLoading, actionResult, clearResult, indexToKb, sendToPaperless, sendViaEmail };
}
