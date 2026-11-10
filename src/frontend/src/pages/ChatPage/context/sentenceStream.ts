/**
 * Sentence-streaming TTS bookkeeping — the pure state-transition behind
 * ChatContext's `handleStreamTtsSettled`.
 *
 * Extracted so the drain logic is unit-testable without a ChatProvider
 * harness, and so the plan's "finding 8" property is enforced by the
 * type signature itself (see `drainSentenceTts`).
 */

import { stripCites } from '../../../components/wissensbasis/citeMarkup';

/** The mutable sentence-streaming state ChatContext threads per turn. */
export interface SentenceStreamState {
  /** Assistant text accumulated so far this turn. */
  accumulated: string;
  /** Index up to which `accumulated` has been dispatched to TTS. */
  dispatchedIdx: number;
  /** True once the first sentence of this turn has been dispatched. */
  active: boolean;
  /** request_ids of sentence TTS calls still in flight. */
  pending: Set<string>;
  /** True once the chat stream itself has finished (the `done` frame). */
  streamDone: boolean;
}

/**
 * Drain one settled TTS request from the pending set.
 *
 * Deliberately takes NO outcome argument: a request that finished, was
 * cancelled (barge-in), or errored must all drain identically. If a
 * `cancelled` frame failed to drain the set, the counter would sit
 * off-by-N and wake word would never resume (plan finding 8). Omitting
 * the parameter makes that regression structurally impossible — there
 * is no outcome here to branch on.
 *
 * Returns true when the turn is fully settled (the stream is done AND
 * nothing is left in flight); the caller then resets state + resumes
 * wake word.
 */
export function drainSentenceTts(
  stream: Pick<SentenceStreamState, 'pending' | 'streamDone'>,
  requestId: string,
): boolean {
  stream.pending.delete(requestId);
  return stream.streamDone && stream.pending.size === 0;
}

/** A sentence terminator followed by whitespace. */
const SENTENCE_BOUNDARY = /[.!?]\s/g;

/**
 * Take every completed sentence not yet dispatched, advancing
 * `dispatchedIdx` past them, and return what should be SPOKEN.
 *
 * The split runs on the raw text, so a citation label containing a
 * sentence end cuts its `<cite>` tag in two; `stripCites` drops the
 * dangling halves, and a piece that was only markup is not returned.
 */
export function takeSpeakableSentences(
  stream: Pick<SentenceStreamState, 'accumulated' | 'dispatchedIdx'>,
): string[] {
  const tail = stream.accumulated.slice(stream.dispatchedIdx);
  const out: string[] = [];
  let cursor = 0;
  for (const m of tail.matchAll(SENTENCE_BOUNDARY)) {
    const end = (m.index ?? 0) + m[0].length;
    const sentence = stripCites(tail.slice(cursor, end)).trim();
    cursor = end;
    if (sentence) out.push(sentence);
  }
  stream.dispatchedIdx += cursor;
  return out;
}

/** The undispatched rest of the final answer, as it should be spoken. */
export function speakableTail(
  stream: Pick<SentenceStreamState, 'dispatchedIdx'>,
  content: string,
): string {
  return stripCites(content.slice(stream.dispatchedIdx)).trim();
}
