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
  id?: number;
  upload_id?: string;
  message?: string;
  status?: string;
  text_preview?: string | null;
  error_message?: string | null;
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

      // Text extraction (OCR) runs server-side in the background: the POST
      // returns immediately with status "processing" instead of blocking on
      // slow OCR (which tripped the 30s axios timeout for large docs). The
      // attachment chip is added in "processing" state; the backend PUSHES an
      // `upload_processed` event over the chat WebSocket when extraction
      // finishes, which flips the attachment to completed (or failed). No
      // polling — see ChatContext.handleUploadProcessed.
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
    // Parallel uploads. Each upload has its own progress slot via
    // `fileKey` (counter-based, see uploadDocument), so the concurrent
    // progress bars don't collide. The earlier sequential `for-await`
    // implementation cost ~N × single-upload time when picking N files
    // at once — for a typical 3-5 doc batch this matters. Backend is
    // async FastAPI on multipart, no concurrency issue.
    return Promise.all(files.map((file) => uploadDocument(file, sessionId)));
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
