import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

/** A meeting recording → speaker-attributed transcript (§2).
 *  Mirrors MeetingResponse in api/routes/meetings.py. */
export interface Meeting {
  id: number;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  title: string | null;
  date: string | null; // ISO YYYY-MM-DD
  error: string | null;
  transcript_document_id: number | null;
  created_at: string;
}

/** One diarized + attributed transcript turn (segments[]). */
export interface MeetingSegment {
  speaker: string;      // display pseudonym or human label ("Sprecher 1", "Anna")
  speaker_key: string;  // stable diarization cluster id (relabel targets this)
  start_s: number;
  end_s: number;
  text: string;
}

interface MeetingSegmentsResponse {
  id: number;
  status: string;
  segments: MeetingSegment[];
}

export interface MeetingUploadInput {
  audio: File;
  consentConfirmed: boolean; // REQUIRED — 422 if false
  title?: string;
  date?: string;             // ISO YYYY-MM-DD
  consentNote?: string;
}

export interface RelabelInput {
  meetingId: number;
  speakerKey: string;
  label: string;
}

/** A meeting is still working — the list should keep polling while any row is. */
function isActive(m: Meeting): boolean {
  return m.status === 'pending' || m.status === 'processing';
}

async function fetchMeetings(): Promise<Meeting[]> {
  const response = await apiClient.get<Meeting[]>('/api/meetings');
  return Array.isArray(response.data) ? response.data : [];
}

async function uploadMeetingRequest(input: MeetingUploadInput): Promise<Meeting> {
  const formData = new FormData();
  formData.append('audio', input.audio);
  formData.append('consent_confirmed', String(input.consentConfirmed));
  if (input.title) formData.append('title', input.title);
  if (input.date) formData.append('date', input.date);
  if (input.consentNote) formData.append('consent_note', input.consentNote);
  const response = await apiClient.post<Meeting>('/api/meetings/transcribe', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
}

async function fetchSegments(meetingId: number): Promise<MeetingSegment[]> {
  const response = await apiClient.get<MeetingSegmentsResponse>(
    `/api/meetings/${meetingId}/segments`,
  );
  return response.data?.segments ?? [];
}

async function deleteMeetingRequest(meetingId: number): Promise<void> {
  await apiClient.delete(`/api/meetings/${meetingId}`);
}

async function relabelRequest(input: RelabelInput): Promise<Meeting> {
  const response = await apiClient.post<Meeting>(
    `/api/meetings/${input.meetingId}/relabel`,
    { speaker_key: input.speakerKey, label: input.label },
  );
  return response.data;
}

export function useMeetingsQuery() {
  return useApiQuery(
    {
      queryKey: keys.meetings.list(),
      queryFn: fetchMeetings,
      staleTime: STALE.LIVE,
      // Poll while any meeting is still transcribing (pending/processing); stop
      // once everything is completed/failed so an idle page makes no requests.
      refetchInterval: (query) =>
        (query.state.data ?? []).some(isActive) ? STALE.LIVE : false,
    },
    'meetings.failedToLoad',
  );
}

export function useMeetingSegments(meetingId: number | null, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.meetings.segments(meetingId ?? 0),
      queryFn: () => fetchSegments(meetingId as number),
      staleTime: STALE.DEFAULT,
      enabled: enabled && meetingId != null,
    },
    'meetings.failedToLoadSegments',
  );
}

export function useUploadMeeting() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: uploadMeetingRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.meetings.all });
      },
    },
    'meetings.failedToUpload',
  );
}

export function useDeleteMeeting() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteMeetingRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.meetings.all });
      },
    },
    'meetings.failedToDelete',
  );
}

export function useRelabelSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: relabelRequest,
      onSuccess: (_data, vars) => {
        // Re-render reindexes in place; refresh both the segments and the list.
        queryClient.invalidateQueries({ queryKey: keys.meetings.segments(vars.meetingId) });
        queryClient.invalidateQueries({ queryKey: keys.meetings.list() });
      },
    },
    'meetings.failedToRelabel',
  );
}
