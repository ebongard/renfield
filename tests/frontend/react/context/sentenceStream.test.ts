/**
 * drainSentenceTts — sentence-streaming TTS drain logic (plan finding 8).
 *
 * ChatContext's handleStreamTtsSettled fires for every terminal TTS
 * outcome (done / cancelled / error). If a `cancelled` frame failed to
 * drain the pending counter, it would sit off-by-N and wake word would
 * never resume. drainSentenceTts has NO outcome parameter, so that
 * regression is structurally impossible — these tests lock the drain +
 * fully-settled reporting.
 */
import { describe, it, expect } from 'vitest';
import {
  drainSentenceTts,
  speakableTail,
  takeSpeakableSentences,
} from '../../../../src/frontend/src/pages/ChatPage/context/sentenceStream';

describe('drainSentenceTts', () => {
  it('removes the settled request id from the pending set', () => {
    const stream = { pending: new Set(['a', 'b']), streamDone: false };
    drainSentenceTts(stream, 'a');
    expect([...stream.pending]).toEqual(['b']);
  });

  it('reports not-fully-settled while other requests are still in flight', () => {
    const stream = { pending: new Set(['a', 'b']), streamDone: true };
    expect(drainSentenceTts(stream, 'a')).toBe(false); // 'b' still pending
  });

  it('reports not-fully-settled when pending empties but the stream is not done', () => {
    const stream = { pending: new Set(['a']), streamDone: false };
    expect(drainSentenceTts(stream, 'a')).toBe(false); // chat stream still open
  });

  it('reports fully-settled once the stream is done and pending is empty', () => {
    const stream = { pending: new Set(['a']), streamDone: true };
    expect(drainSentenceTts(stream, 'a')).toBe(true);
  });

  it('drains identically regardless of a request\'s outcome (finding 8)', () => {
    // The function takes no outcome — a cancelled or errored request
    // drains exactly like a finished one. A 3-sentence turn whose
    // sentences settle one by one (any mix of done/cancelled/error)
    // fully drains, and the last settle reports the turn complete.
    const stream = { pending: new Set(['s1', 's2', 's3']), streamDone: true };
    expect(drainSentenceTts(stream, 's2')).toBe(false); // cancelled
    expect(drainSentenceTts(stream, 's1')).toBe(false); // errored
    expect(drainSentenceTts(stream, 's3')).toBe(true); // done — turn settled
    expect(stream.pending.size).toBe(0);
  });

  it('is a harmless no-op for a request id not in the set', () => {
    const stream = { pending: new Set(['a']), streamDone: false };
    expect(drainSentenceTts(stream, 'unknown')).toBe(false);
    expect([...stream.pending]).toEqual(['a']);
  });
});

describe('takeSpeakableSentences', () => {
  it('returns completed sentences and advances past them, leaving the tail', () => {
    const stream = { accumulated: 'Erster Satz. Zweiter Satz! Und ein Rest', dispatchedIdx: 0 };
    expect(takeSpeakableSentences(stream)).toEqual(['Erster Satz.', 'Zweiter Satz!']);
    expect(stream.accumulated.slice(stream.dispatchedIdx)).toBe('Und ein Rest');
    expect(takeSpeakableSentences(stream)).toEqual([]);
  });

  it('speaks citation labels, never the markup', () => {
    const stream = {
      accumulated: 'Offen ist <cite entity="JIRA-1" type="ticket">Login kaputt</cite>. Sonst nichts. ',
      dispatchedIdx: 0,
    };
    expect(takeSpeakableSentences(stream)).toEqual(['Offen ist Login kaputt.', 'Sonst nichts.']);
  });

  it('drops both halves of a tag that a sentence end inside the label cut in two', () => {
    const stream = {
      accumulated: 'Siehe <cite entity="REL-1" type="release">Version 1. Final</cite> dazu. ',
      dispatchedIdx: 0,
    };
    const spoken = takeSpeakableSentences(stream);
    expect(spoken).toEqual(['Siehe Version 1.', 'Final dazu.']);
    expect(spoken.join(' ')).not.toMatch(/cite|entity=/);
  });
});

describe('speakableTail', () => {
  it('returns the undispatched rest without markup', () => {
    const content = 'Erster Satz. Dann <cite entity="JIRA-1">Login kaputt</cite>';
    expect(speakableTail({ dispatchedIdx: 13 }, content)).toBe('Dann Login kaputt');
  });

  it('strips a closing half left over from a tag cut at the last sentence end', () => {
    const content = 'Siehe <cite entity="REL-1">Version 1. Final</cite>';
    expect(speakableTail({ dispatchedIdx: 'Siehe <cite entity="REL-1">Version 1. '.length }, content))
      .toBe('Final');
  });
});
