import { useState, useCallback } from 'react';
import apiClient from '../../../utils/axios';

export function useDocumentUpload() {
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);

  const uploadDocument = useCallback(async (file, sessionId) => {
    setUploading(true);
    setUploadError(null);

    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('session_id', sessionId);

      const response = await apiClient.post('/api/chat/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });

      return response.data;
    } catch (error) {
      const message = error.response?.data?.detail || error.message;
      setUploadError(message);
      return null;
    } finally {
      setUploading(false);
    }
  }, []);

  return { uploading, uploadError, uploadDocument };
}
