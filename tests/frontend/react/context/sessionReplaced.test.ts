/**
 * messagesAfterSessionReplaced — what stays on screen when the server swaps
 * the tab's session id (#1485).
 *
 * A conversation deleted elsewhere is still on screen in this tab; after the
 * swap none of it exists and the new conversation starts empty. Only the turn
 * in flight belongs to the new conversation.
 */
import { describe, it, expect } from 'vitest';
import { messagesAfterSessionReplaced } from '../../../../src/frontend/src/pages/ChatPage/context/sessionReplaced';

const old = [
  { role: 'user', content: 'alte Frage' },
  { role: 'assistant', content: 'alte Antwort' },
];

describe('messagesAfterSessionReplaced', () => {
  it('keeps only the turn in flight', () => {
    const screen = [...old, { role: 'user', content: 'neue Frage' }];
    expect(messagesAfterSessionReplaced(screen, true)).toEqual([
      { role: 'user', content: 'neue Frage' },
    ]);
  });

  it('keeps an answer that already started streaming for that turn', () => {
    const screen = [
      ...old,
      { role: 'user', content: 'neue Frage' },
      { role: 'assistant', content: 'Antw' },
    ];
    expect(messagesAfterSessionReplaced(screen, true)).toEqual(screen.slice(2));
  });

  it('clears the deleted transcript when no turn is in flight', () => {
    expect(messagesAfterSessionReplaced(old, false)).toEqual([]);
  });

  it('leaves the single turn of a refused foreign session as it is', () => {
    const screen = [{ role: 'user', content: 'Frage' }];
    expect(messagesAfterSessionReplaced(screen, true)).toEqual(screen);
  });

  it('keeps nothing without a user message', () => {
    expect(messagesAfterSessionReplaced([{ role: 'assistant', content: 'x' }], true)).toEqual([]);
  });
});
