import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type PersonalityStyle = 'freundlich' | 'direkt' | 'formell' | 'casual';

export interface AdminUser {
  id: number;
  username: string;
  first_name?: string | null;
  last_name?: string | null;
  email?: string | null;
  role_id: number;
  role_name?: string;
  is_active: boolean;
  personality_style?: PersonalityStyle;
  personality_prompt?: string | null;
  speaker_id?: number | null;
  last_login?: string | null;
  /** A login lockout (per-IP or username-wide) is currently held for this user. */
  locked_out?: boolean;
  /**
   * A DEVICE account, not a person (auth-on cutover D-4b): the identity an
   * unrecognised satellite voice runs as. It reads and acts, but collects no
   * memories and books no presence. Set through create/update; no UI yet.
   */
  is_device_account?: boolean;
  /**
   * For this account the voice is a SECOND login factor: the password alone no
   * longer yields tokens, `POST /auth/voice` checks 1:1 against the linked
   * speaker profile. Only the account itself may switch it on (it is a consent
   * to biometric processing); anyone holding `users.manage` may switch it off
   * -- that is the only way back when the microphone is broken.
   */
  voice_second_factor_enabled?: boolean;
}

export interface RoleSummary {
  id: number;
  name: string;
  description?: string;
}

export interface SpeakerSummary {
  id: number;
  name: string;
  embedding_count: number;
}

export interface CreateUserInput {
  username: string;
  first_name?: string | null;
  last_name?: string | null;
  email?: string | null;
  password: string;
  role_id: number;
  is_active: boolean;
  personality_style: PersonalityStyle;
  personality_prompt?: string | null;
}

export interface UpdateUserInput {
  id: number;
  patch: {
    username: string;
    first_name?: string | null;
    last_name?: string | null;
    email?: string | null;
    role_id: number;
    is_active: boolean;
    personality_style: PersonalityStyle;
    personality_prompt?: string | null;
  };
}

async function fetchUsers(): Promise<AdminUser[]> {
  const response = await apiClient.get<AdminUser[] | { users?: AdminUser[] }>('/api/users');
  const data = response.data;
  return Array.isArray(data) ? data : (data.users ?? []);
}

async function fetchRoles(): Promise<RoleSummary[]> {
  const response = await apiClient.get<RoleSummary[] | { roles?: RoleSummary[] }>('/api/roles');
  const data = response.data;
  return Array.isArray(data) ? data : (data.roles ?? []);
}

async function fetchSpeakers(): Promise<SpeakerSummary[]> {
  try {
    const response = await apiClient.get<SpeakerSummary[]>('/api/speakers');
    return response.data ?? [];
  } catch {
    return [];
  }
}

async function createUserRequest(input: CreateUserInput): Promise<AdminUser> {
  const response = await apiClient.post<AdminUser>('/api/users', input);
  return response.data;
}

async function updateUserRequest(input: UpdateUserInput): Promise<AdminUser> {
  const response = await apiClient.patch<AdminUser>(`/api/users/${input.id}`, input.patch);
  return response.data;
}

async function resetPasswordRequest(args: { id: number; password: string }): Promise<void> {
  await apiClient.post(`/api/users/${args.id}/reset-password`, { new_password: args.password });
}

async function deleteUserRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/users/${id}`);
}

async function linkSpeakerRequest(args: { userId: number; speakerId: number }): Promise<void> {
  await apiClient.post(`/api/users/${args.userId}/link-speaker`, { speaker_id: args.speakerId });
}

async function unlinkSpeakerRequest(userId: number): Promise<void> {
  // Backend uses REST verb-on-noun: POST /link-speaker creates the link,
  // DELETE /link-speaker removes it. There is no `/unlink-speaker` route
  // — it returned 404 silently and the unlink button looked broken.
  await apiClient.delete(`/api/users/${userId}/link-speaker`);
}

export function useUsersQuery() {
  return useApiQuery(
    {
      queryKey: keys.users.list(),
      queryFn: fetchUsers,
      staleTime: STALE.DEFAULT,
    },
    'users.failedToLoad',
  );
}

export function useRolesListQuery() {
  return useApiQuery(
    {
      queryKey: keys.roles.list(),
      queryFn: fetchRoles,
      staleTime: STALE.CONFIG,
    },
    'users.failedToLoad',
  );
}

export function useSpeakersListQuery() {
  return useApiQuery(
    {
      queryKey: keys.speakers.list(),
      queryFn: fetchSpeakers,
      staleTime: STALE.DEFAULT,
    },
    'users.failedToLoad',
  );
}

export function useCreateUser() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createUserRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToSave',
  );
}

export function useUpdateUser() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateUserRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToSave',
  );
}

export function useResetUserPassword() {
  return useApiMutation(
    {
      mutationFn: resetPasswordRequest,
    },
    'users.failedToSave',
  );
}

async function setVoiceSecondFactorRequest(
  args: { id: number; enabled: boolean; currentPassword?: string },
): Promise<AdminUser> {
  const response = await apiClient.post<AdminUser>(
    `/api/users/${args.id}/voice-second-factor`,
    // 🛑 The server demands the password ONLY when switching off ONE'S OWN
    // factor: a token issued before the consent outlives it and would
    // otherwise be able to withdraw it. For somebody else's account `admin`
    // is the guard.
    { enabled: args.enabled, current_password: args.currentPassword ?? null },
  );
  return response.data;
}

/**
 * The four machine-readable reasons from the backend's
 * `services/voice_factor_preconditions`. They arrive as codes -- from
 * `GET /{id}/voice-second-factor` as well as from the 409 answer when
 * switching on -- and are translated HERE. Before that the server sent three
 * hard-coded English sentences that a German-speaking household member got to
 * read untranslated.
 */
export type VoiceFactorBlocker =
  | 'voice_path_off'
  | 'recognition_off'
  | 'no_profile'
  | 'no_embeddings'
  // 🛑 Comes ONLY from the 409 answer, never from the read route: a device
  // account does not log in and never reads its own state. The code belongs
  // here regardless -- in the 2026-09-29 rework it was left standing as a
  // hard-coded English sentence, four lines from the three that were replaced.
  | 'device_account';

export const VOICE_FACTOR_BLOCKERS: readonly VoiceFactorBlocker[] = [
  'voice_path_off',
  'recognition_off',
  'no_profile',
  'no_embeddings',
  'device_account',
] as const;

export function isVoiceFactorBlocker(value: unknown): value is VoiceFactorBlocker {
  return typeof value === 'string'
    && (VOICE_FACTOR_BLOCKERS as readonly string[]).includes(value);
}

export interface VoiceSecondFactorState {
  enabled: boolean;
  /** `null` = nothing is in the way. */
  blocker: VoiceFactorBlocker | null;
}

export function useVoiceSecondFactorState(userId: number | undefined) {
  return useApiQuery<VoiceSecondFactorState>(
    {
      queryKey: keys.users.voiceFactor(userId ?? 0),
      queryFn: async () => {
        const response = await apiClient.get<VoiceSecondFactorState>(
          `/api/users/${userId}/voice-second-factor`,
        );
        return response.data;
      },
      enabled: userId !== undefined,
      staleTime: STALE.DEFAULT,
    },
    'account.voiceStateFailed',
  );
}

export function useSetVoiceSecondFactor() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: setVoiceSecondFactorRequest,
      onSuccess: () => {
        // `keys.users.all` covers `voiceFactor` too (same prefix) -- the
        // blocker may have changed along with the toggle.
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToSave',
  );
}

async function unlockUserRequest(id: number): Promise<void> {
  await apiClient.post(`/api/users/${id}/unlock`);
}

export function useUnlockUser() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: unlockUserRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToUnlock',
  );
}

export function useDeleteUser() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteUserRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToDelete',
  );
}

export function useLinkSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: linkSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToLink',
  );
}

export function useUnlinkSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: unlinkSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToUnlink',
  );
}
