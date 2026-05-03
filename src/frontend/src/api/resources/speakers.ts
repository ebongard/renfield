import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export interface Speaker {
  id: number;
  name: string;
  alias: string;
  is_admin: boolean;
  embedding_count?: number;
  user_id?: number | null;
  user_name?: string | null;
  is_pinned?: boolean;
}

export interface SpeakerServiceStatus {
  available: boolean;
  message?: string;
}

export interface IdentifyResult {
  is_identified: boolean;
  speaker_id?: number | null;
  speaker_name?: string | null;
  speaker_alias?: string | null;
  confidence?: number;
  message?: string;
}

async function fetchSpeakers(): Promise<Speaker[]> {
  const response = await apiClient.get<Speaker[]>('/api/speakers');
  return response.data ?? [];
}

async function fetchSpeakerStatus(): Promise<SpeakerServiceStatus> {
  const response = await apiClient.get<SpeakerServiceStatus>('/api/speakers/status');
  return response.data;
}

export interface CreateSpeakerInput {
  name: string;
  alias: string;
  is_admin: boolean;
}

interface UpdateSpeakerInput {
  id: number;
  patch: { name: string; alias: string; is_admin: boolean };
}

async function createSpeakerRequest(input: CreateSpeakerInput): Promise<Speaker> {
  const response = await apiClient.post<Speaker>('/api/speakers', input);
  return response.data;
}

async function updateSpeakerRequest({ id, patch }: UpdateSpeakerInput): Promise<Speaker> {
  const response = await apiClient.patch<Speaker>(`/api/speakers/${id}`, patch);
  return response.data;
}

async function deleteSpeakerRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/speakers/${id}`);
}

interface MergeInput {
  source_speaker_id: number;
  target_speaker_id: number;
}

async function mergeSpeakersRequest(input: MergeInput): Promise<{ message: string }> {
  const response = await apiClient.post<{ message: string }>('/api/speakers/merge', input);
  return response.data;
}

interface EnrollInput {
  speakerId: number;
  audio: Blob;
}

async function enrollSpeakerRequest({ speakerId, audio }: EnrollInput): Promise<{ message: string }> {
  const formData = new FormData();
  formData.append('audio', audio, 'voice_sample.webm');
  const response = await apiClient.post<{ message: string }>(
    `/api/speakers/${speakerId}/enroll`,
    formData,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  );
  return response.data;
}

async function identifySpeakerRequest(audio: Blob): Promise<IdentifyResult> {
  const formData = new FormData();
  formData.append('audio', audio, 'identify.webm');
  const response = await apiClient.post<IdentifyResult>(
    '/api/speakers/identify',
    formData,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  );
  return response.data;
}

export function useSpeakersQuery() {
  return useApiQuery(
    {
      queryKey: keys.speakers.list(),
      queryFn: fetchSpeakers,
      staleTime: STALE.DEFAULT,
    },
    'speakers.couldNotLoad',
  );
}

export function useSpeakerStatusQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.speakers.all, 'status'] as const,
      queryFn: fetchSpeakerStatus,
      staleTime: STALE.CONFIG,
    },
    'speakers.serviceNotAvailable',
  );
}

export function useCreateSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.speakers.all });
      },
    },
    'common.error',
  );
}

export function useUpdateSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.speakers.all });
      },
    },
    'common.error',
  );
}

export function useDeleteSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.speakers.all });
      },
    },
    'common.error',
  );
}

export function useMergeSpeakers() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: mergeSpeakersRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.speakers.all });
      },
    },
    'speakers.mergeFailed',
  );
}

export function useEnrollSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: enrollSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.speakers.all });
      },
    },
    'speakers.voiceSampleFailed',
  );
}

export function useIdentifySpeaker() {
  return useApiMutation(
    {
      mutationFn: identifySpeakerRequest,
    },
    'speakers.identificationFailed',
  );
}
