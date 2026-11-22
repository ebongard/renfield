/**
 * The voice-only fields of a chat-WS `text` message.
 *
 * A voice turn carries the speaker embedding from the voice-server (B.4.a) and,
 * since #1613, content-free turn-end metadata: why the turn ended, how long the
 * recording was, and how soon after the previous answer it started. The backend
 * keeps the latter as a turn trace when `VOICE_TURN_TRACES_ENABLED` is on. A
 * typed message carries neither -- the result is then empty.
 */
import type { VoiceTurnMetadata } from '../hooks';

export interface VoiceMeta {
  speakerEmbedding?: number[] | null;
  voiceTurn?: VoiceTurnMetadata;
}

export interface VoiceEnvelopeFields {
  speaker_embedding?: number[];
  voice_turn?: VoiceTurnMetadata;
}

export function voiceEnvelopeFields(voiceMeta?: VoiceMeta): VoiceEnvelopeFields {
  const fields: VoiceEnvelopeFields = {};
  if (voiceMeta?.speakerEmbedding) fields.speaker_embedding = voiceMeta.speakerEmbedding;
  if (voiceMeta?.voiceTurn) fields.voice_turn = voiceMeta.voiceTurn;
  return fields;
}
