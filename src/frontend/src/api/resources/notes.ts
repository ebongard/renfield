import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

/** A hand-authored note (Phase 4B). Mirrors NoteResponse in api/routes/notes.py.
 *  A first-class atom → circle-tiered + searchable in /brain. */
export interface Note {
  id: number;
  title: string;
  body: string;
  circle_tier: number;
  project_id: number | null;
  owner_id: number | null;
  atom_id: string;
  created_at: string;
  updated_at: string;
}

export interface NoteCreateInput {
  title: string;
  body?: string;
  circle_tier?: number;
  project_id?: number | null;
}

export interface NoteUpdateInput {
  id: number;
  title?: string;
  body?: string;
  circle_tier?: number;
}

/** A [[link]] endpoint of a note. note_id null = a dangling link (target not yet
 *  written). Mirrors NoteLink in api/routes/notes.py. */
export interface NoteLink {
  title: string;
  note_id: number | null;
}

export interface NoteLinks {
  outgoing: NoteLink[];   // this note's [[Target]] links
  backlinks: NoteLink[];  // notes that link TO this note
}

async function fetchNotes(): Promise<Note[]> {
  const response = await apiClient.get<Note[]>('/api/notes');
  return Array.isArray(response.data) ? response.data : [];
}

async function fetchNoteLinks(id: number): Promise<NoteLinks> {
  const response = await apiClient.get<NoteLinks>(`/api/notes/${id}/links`);
  return response.data;
}

async function createNoteRequest(input: NoteCreateInput): Promise<Note> {
  const response = await apiClient.post<Note>('/api/notes', input);
  return response.data;
}

async function updateNoteRequest(input: NoteUpdateInput): Promise<Note> {
  const { id, ...body } = input;
  const response = await apiClient.put<Note>(`/api/notes/${id}`, body);
  return response.data;
}

async function deleteNoteRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/notes/${id}`);
}

export function useNotesQuery() {
  return useApiQuery(
    { queryKey: keys.notes.list(), queryFn: fetchNotes, staleTime: STALE.DEFAULT },
    'notes.failedToLoad',
  );
}

export function useNoteLinks(id: number, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.notes.links(id),
      queryFn: () => fetchNoteLinks(id),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'notes.failedToLoadLinks',
  );
}

export function useCreateNote() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createNoteRequest,
      onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.notes.all }),
    },
    'notes.failedToSave',
  );
}

export function useUpdateNote() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateNoteRequest,
      onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.notes.all }),
    },
    'notes.failedToSave',
  );
}

export function useDeleteNote() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteNoteRequest,
      onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.notes.all }),
    },
    'notes.failedToDelete',
  );
}
