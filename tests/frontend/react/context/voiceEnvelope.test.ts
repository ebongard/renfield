/**
 * voiceEnvelopeFields — the voice-only fields of a chat-WS message (#1613).
 *
 * A voice turn carries the content-free turn-end metadata (and the speaker
 * embedding); a typed message carries neither, so its envelope is unchanged.
 */
import { describe, it, expect } from 'vitest';
import { voiceEnvelopeFields } from '../../../../src/frontend/src/pages/ChatPage/context/voiceEnvelope';

const turn = {
  end_reason: 'silence' as const, audio_duration_s: 3.2, gap_since_answer_s: 1.5, turn_key: 'k-1',
};

describe('voiceEnvelopeFields', () => {
  it('attaches the turn metadata for a voice turn', () => {
    expect(voiceEnvelopeFields({ speakerEmbedding: [0.1, 0.2], voiceTurn: turn })).toEqual({
      speaker_embedding: [0.1, 0.2],
      voice_turn: turn,
    });
  });

  it('keeps a voice turn without an embedding', () => {
    expect(voiceEnvelopeFields({ speakerEmbedding: null, voiceTurn: turn })).toEqual({ voice_turn: turn });
  });

  it('adds nothing for a typed message', () => {
    expect(voiceEnvelopeFields(undefined)).toEqual({});
    expect(voiceEnvelopeFields({})).toEqual({});
  });

  it('carries no text, audio or identity', () => {
    const fields = voiceEnvelopeFields({ voiceTurn: turn });
    expect(Object.keys(fields.voice_turn ?? {}).sort()).toEqual(
      ['audio_duration_s', 'end_reason', 'gap_since_answer_s', 'turn_key'],
    );
  });
});
