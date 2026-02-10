import { useState, useCallback, useRef } from 'react';
import apiClient from '../../../utils/axios';

export function useDocumentUpload() {
  const [uploadStates, setUploadStates] = useState({});
  const [uploadError, setUploadError] = useState(null);
  const keyCounter = useRef(0);

  const uploading = Object.values(uploadStates).some(s => s.uploading);

  const uploadDocument = useCallback(async (file, sessionId) => {
    const fileKey = `upload-${keyCounter.current++}`;

    setUploadStates(prev => ({
      ...prev,
      [fileKey]: { progress: 0, uploading: true, error: null, name: file.name },
    }));
    setUploadError(null);

    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('session_id', sessionId);

      const response = await apiClient.post('/api/chat/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (progressEvent) => {
          const percent = progressEvent.total
            ? Math.round((progressEvent.loaded * 100) / progressEvent.total)
            : 0;
          setUploadStates(prev => ({
            ...prev,
            [fileKey]: { ...prev[fileKey], progress: percent },
          }));
        },
      });

      // Remove from upload states on success
      setUploadStates(prev => {
        const next = { ...prev };
        delete next[fileKey];
        return next;
      });

      return response.data;
    } catch (error) {
      const message = error.response?.data?.detail || error.message;
      setUploadError(message);
      setUploadStates(prev => ({
        ...prev,
        [fileKey]: { ...prev[fileKey], uploading: false, error: message },
      }));
      return null;
    }
  }, []);

  const uploadDocuments = useCallback(async (files, sessionId) => {
    const results = [];
    for (const file of files) {
      const result = await uploadDocument(file, sessionId);
      results.push(result);
    }
    return results;
  }, [uploadDocument]);

  const clearError = useCallback((fileKey) => {
    if (fileKey) {
      setUploadStates(prev => {
        const next = { ...prev };
        delete next[fileKey];
        return next;
      });
    }
    setUploadError(null);
  }, []);

  return { uploading, uploadError, uploadDocument, uploadDocuments, uploadStates, clearError };
}
