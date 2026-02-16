import { useState, useCallback } from 'react';
import apiClient from '../../../utils/axios';

export function useQuickActions() {
  const [actionLoading, setActionLoading] = useState({});
  const [actionResult, setActionResult] = useState(null);

  const indexToKb = useCallback(async (uploadId, knowledgeBaseId) => {
    setActionLoading(prev => ({ ...prev, [uploadId]: 'indexing' }));
    try {
      const response = await apiClient.post(`/api/chat/upload/${uploadId}/index`, {
        knowledge_base_id: knowledgeBaseId,
      });
      setActionResult({
        type: 'indexing',
        success: true,
        message: response.data.message,
      });
    } catch (error) {
      const status = error.response?.status;
      const detail = error.response?.data?.detail || 'Unknown error';
      if (status === 409) {
        // Already indexed (e.g. by auto-index) — treat as success
        setActionResult({
          type: 'indexing',
          success: true,
          message: detail,
        });
      } else {
        setActionResult({
          type: 'indexing',
          success: false,
          message: detail,
        });
      }
    } finally {
      setActionLoading(prev => {
        const next = { ...prev };
        delete next[uploadId];
        return next;
      });
    }
  }, []);

  const sendToPaperless = useCallback(async (uploadId) => {
    setActionLoading(prev => ({ ...prev, [uploadId]: 'paperless' }));
    try {
      const response = await apiClient.post(`/api/chat/upload/${uploadId}/paperless`);
      setActionResult({
        type: 'paperless',
        success: true,
        message: response.data.message,
      });
    } catch (error) {
      const detail = error.response?.data?.detail || 'Unknown error';
      setActionResult({
        type: 'paperless',
        success: false,
        message: detail,
      });
    } finally {
      setActionLoading(prev => {
        const next = { ...prev };
        delete next[uploadId];
        return next;
      });
    }
  }, []);

  const sendViaEmail = useCallback(async (uploadId, to, subject, body) => {
    setActionLoading(prev => ({ ...prev, [uploadId]: 'email' }));
    try {
      const response = await apiClient.post(`/api/chat/upload/${uploadId}/email`, {
        to,
        subject,
        body,
      });
      setActionResult({
        type: 'email',
        success: true,
        message: response.data.message,
      });
    } catch (error) {
      const detail = error.response?.data?.detail || 'Unknown error';
      setActionResult({
        type: 'email',
        success: false,
        message: detail,
      });
    } finally {
      setActionLoading(prev => {
        const next = { ...prev };
        delete next[uploadId];
        return next;
      });
    }
  }, []);

  const clearResult = useCallback(() => {
    setActionResult(null);
  }, []);

  return { actionLoading, actionResult, clearResult, indexToKb, sendToPaperless, sendViaEmail };
}
