/**
 * Security tests for the legacy SSO URL-fragment hand-off guard
 * (utils/ssoFragment.ts). The old #access_token= handler accepted ANY value;
 * the hardened version accepts only a structurally valid, unexpired access JWT
 * and always strips the fragment. These lock that behavior so a regression that
 * re-accepts arbitrary tokens is caught.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  consumeSsoFragmentHandoff,
  looksLikeUnexpiredAccessJwt,
} from '../../../../src/frontend/src/utils/ssoFragment';

const ACCESS_TOKEN_KEY = 'renfield_access_token';

function makeJwt(payload: Record<string, unknown>): string {
  const seg = (o: Record<string, unknown>) => btoa(JSON.stringify(o));
  return `${seg({ alg: 'HS256', typ: 'JWT' })}.${seg(payload)}.sig`;
}

const future = () => Math.floor(Date.now() / 1000) + 3600;
const past = () => Math.floor(Date.now() / 1000) - 3600;

describe('looksLikeUnexpiredAccessJwt', () => {
  it('accepts a valid unexpired access token', () => {
    expect(looksLikeUnexpiredAccessJwt(makeJwt({ type: 'access', exp: future() }))).toBe(true);
  });

  it('rejects a non-access token type', () => {
    expect(looksLikeUnexpiredAccessJwt(makeJwt({ type: 'refresh', exp: future() }))).toBe(false);
  });

  it('rejects an expired token', () => {
    expect(looksLikeUnexpiredAccessJwt(makeJwt({ type: 'access', exp: past() }))).toBe(false);
  });

  it('rejects a token with no exp', () => {
    expect(looksLikeUnexpiredAccessJwt(makeJwt({ type: 'access' }))).toBe(false);
  });

  it('rejects a non-JWT string', () => {
    expect(looksLikeUnexpiredAccessJwt('not-a-jwt')).toBe(false);
    expect(looksLikeUnexpiredAccessJwt('a.b')).toBe(false);
    expect(looksLikeUnexpiredAccessJwt('!!!.@@@.###')).toBe(false);
  });
});

describe('consumeSsoFragmentHandoff', () => {
  beforeEach(() => {
    localStorage.clear();
    history.replaceState(null, '', '/');
  });

  it('stores a valid token and strips the fragment', () => {
    const token = makeJwt({ type: 'access', exp: future() });
    history.replaceState(null, '', `/#access_token=${token}`);
    consumeSsoFragmentHandoff(true);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBe(token);
    expect(window.location.hash).toBe('');
  });

  it('rejects a malformed token but still strips the fragment', () => {
    history.replaceState(null, '', '/#access_token=garbage');
    consumeSsoFragmentHandoff(true);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(window.location.hash).toBe('');
  });

  it('rejects an expired token (injection guard) but strips the fragment', () => {
    const token = makeJwt({ type: 'access', exp: past() });
    history.replaceState(null, '', `/#access_token=${token}`);
    consumeSsoFragmentHandoff(true);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(window.location.hash).toBe('');
  });

  it('is a no-op when disabled (kill switch), leaving the fragment untouched', () => {
    const token = makeJwt({ type: 'access', exp: future() });
    history.replaceState(null, '', `/#access_token=${token}`);
    consumeSsoFragmentHandoff(false);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(window.location.hash).toBe(`#access_token=${token}`);
  });

  it('ignores a hash without an access_token', () => {
    history.replaceState(null, '', '/#something=else');
    consumeSsoFragmentHandoff(true);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });
});
