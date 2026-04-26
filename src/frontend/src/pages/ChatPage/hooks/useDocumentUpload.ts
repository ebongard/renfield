import { useState, useCallback, useRef } from 'react';
import type { AxiosProgressEvent } from 'axios';
import apiClient from '../../../utils/axios';
import { extractApiError } from '../../../utils/axios';

export interface UploadState {
  progress: number;
  uploading: boolean;
  error: string | null;
  name: string;
}

export type UploadStates = Record<string, UploadState>;

export interface UploadedDocument {
  upload_id?: string;
  message?: string;
  [key: string]: unknown;
}

export function useDocumentUpload() {
  const [uploadStates, setUploadStates] = useState<UploadStates>({});
  const [uploadError, setUploadError] = useState<string | null>(null);
  const keyCounter = useRef(0);

  const uploading = Object.values(uploadStates).some((s) => s.uploading);

  const uploadDocument = useCallback(async (file: File, sessionId: string): Promise<UploadedDocument | null> => {
    const fileKey = `upload-${keyCounter.current++}`;

    setUploadStates((prev) => ({
      ...prev,
      [fileKey]: { progress: 0, uploading: true, error: null, name: file.name },
    }));
    setUploadError(null);

    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('session_id', sessionId);

      const response = await apiClient.post<UploadedDocument>('/api/chat/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (progressEvent: AxiosProgressEvent) => {
          const percent = progressEvent.total
            ? Math.round((progressEvent.loaded * 100) / progressEvent.total)
            : 0;
          setUploadStates((prev) => {
            const existing = prev[fileKey];
            if (!existing) return prev;
            return { ...prev, [fileKey]: { ...existing, progress: percent } };
          });
        },
      });

      setUploadStates((prev) => {
        const next = { ...prev };
        delete next[fileKey];
        return next;
      });

      return response.data;
    } catch (error) {
      const message = extractApiError(error, error instanceof Error ? error.message : 'Upload failed');
      setUploadError(message);
      setUploadStates((prev) => {
        const existing = prev[fileKey];
        if (!existing) return prev;
        return { ...prev, [fileKey]: { ...existing, uploading: false, error: message } };
      });
      return null;
    }
  }, []);

  const uploadDocuments = useCallback(async (files: File[], sessionId: string): Promise<Array<UploadedDocument | null>> => {
    const results: Array<UploadedDocument | null> = [];
    for (const file of files) {
      const result = await uploadDocument(file, sessionId);
      results.push(result);
    }
    return results;
  }, [uploadDocument]);

  const clearError = useCallback((fileKey?: string) => {
    if (fileKey) {
      setUploadStates((prev) => {
        const next = { ...prev };
        delete next[fileKey];
        return next;
      });
    }
    setUploadError(null);
  }, []);

  return { uploading, uploadError, uploadDocument, uploadDocuments, uploadStates, clearError };
}
