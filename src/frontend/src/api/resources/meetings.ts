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

// --------------------------------------------------------------------------- //
// §2 Phase 3 — minutes (summary / decisions / action-items with human confirm)
// --------------------------------------------------------------------------- //

/** One decision recorded in the minutes (mirrors _Decision in meetings.py). */
export interface MinutesDecision {
  text: string;
  made_by: string; // person named in the transcript, or "" (unknown)
}

/** One agreed action-item (mirrors _ActionItem). due_hint is a VERBATIM hint
 *  ("bis Freitag"), NOT a computed date — action-items are meeting-scoped and
 *  deliberately NOT obligations for the Fristen agenda. */
export interface MinutesActionItem {
  text: string;
  owner: string;
  due_hint: string;
}

/** The minutes body — the editable draft shape (mirrors MinutesBody). */
export interface MinutesBody {
  summary: string;
  decisions: MinutesDecision[];
  action_items: MinutesActionItem[];
}

/** none → not generated; draft → editable/unconfirmed; confirmed → rendered into
 *  the transcript document. Mirrors Meeting.minutes_status. */
export type MinutesStatus = 'none' | 'draft' | 'confirmed';

/** Mirrors MinutesResponse in meetings.py. `minutes` is null until generated. */
export interface Minutes {
  id: number;
  minutes_status: MinutesStatus;
  minutes: MinutesBody | null;
}

async function fetchMinutes(meetingId: number): Promise<Minutes> {
  const response = await apiClient.get<Minutes>(`/api/meetings/${meetingId}/minutes`);
  return response.data;
}

async function generateMinutesRequest(meetingId: number): Promise<Minutes> {
  const response = await apiClient.post<Minutes>(`/api/meetings/${meetingId}/minutes/generate`);
  return response.data;
}

async function updateMinutesRequest(input: { meetingId: number; body: MinutesBody }): Promise<Minutes> {
  const response = await apiClient.put<Minutes>(`/api/meetings/${input.meetingId}/minutes`, input.body);
  return response.data;
}

async function confirmMinutesRequest(meetingId: number): Promise<Minutes> {
  const response = await apiClient.post<Minutes>(`/api/meetings/${meetingId}/minutes/confirm`);
  return response.data;
}

async function deleteMinutesRequest(meetingId: number): Promise<Minutes> {
  const response = await apiClient.delete<Minutes>(`/api/meetings/${meetingId}/minutes`);
  return response.data;
}

/** Load current minutes + status. Enabled-gated (only fetched for a completed,
 *  expanded meeting when the minutes feature flag is on). */
export function useMinutes(meetingId: number | null, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.meetings.minutes(meetingId ?? 0),
      queryFn: () => fetchMinutes(meetingId as number),
      staleTime: STALE.DEFAULT,
      enabled: enabled && meetingId != null,
    },
    'meetings.minutes.failedToLoad',
  );
}

/** Confirming re-renders + reindexes the transcript document in place, so the
 *  minutes AND the list (transcript_document_id is stable, but status/reindex
 *  ripple) get invalidated on every write. */
function invalidateMinutes(queryClient: ReturnType<typeof useQueryClient>, meetingId: number) {
  queryClient.invalidateQueries({ queryKey: keys.meetings.minutes(meetingId) });
  queryClient.invalidateQueries({ queryKey: keys.meetings.list() });
}

export function useGenerateMinutes() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: generateMinutesRequest,
      onSuccess: (data) => invalidateMinutes(queryClient, data.id),
    },
    'meetings.minutes.failedToGenerate',
  );
}

export function useUpdateMinutes() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateMinutesRequest,
      onSuccess: (data) => invalidateMinutes(queryClient, data.id),
    },
    'meetings.minutes.failedToSave',
  );
}

export function useConfirmMinutes() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: confirmMinutesRequest,
      onSuccess: (data) => {
        invalidateMinutes(queryClient, data.id);
        // Confirm folds the minutes into the transcript doc → refresh segments too.
        queryClient.invalidateQueries({ queryKey: keys.meetings.segments(data.id) });
      },
    },
    'meetings.minutes.failedToConfirm',
  );
}

export function useDeleteMinutes() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteMinutesRequest,
      onSuccess: (data) => invalidateMinutes(queryClient, data.id),
    },
    'meetings.minutes.failedToDelete',
  );
}
