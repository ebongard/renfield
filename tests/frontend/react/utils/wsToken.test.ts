/**
 * Tests for fetchWsToken (utils/wsToken.ts) — the WS-scoped-token fetch that
 * replaced putting the full 24h JWT in the WebSocket URL (security audit M2).
 * It must return the short-lived token on success and null (never the long-lived
 * localStorage JWT) on a null response or any error.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: { post: vi.fn() },
}));

import apiClient from '../../../../src/frontend/src/utils/axios';
import { fetchVoiceToken, fetchWsToken } from '../../../../src/frontend/src/utils/wsToken';

const post = apiClient.post as unknown as ReturnType<typeof vi.fn>;

describe('fetchWsToken', () => {
  beforeEach(() => {
    post.mockReset();
  });

  it('returns the scoped token from the response', async () => {
    post.mockResolvedValueOnce({ data: { token: 'ws-scoped-token' } });
    await expect(fetchWsToken()).resolves.toBe('ws-scoped-token');
    expect(post).toHaveBeenCalledWith('/api/ws/token', null, { params: { purpose: 'ws' } });
  });

  it('fetchVoiceToken requests the voice purpose', async () => {
    post.mockResolvedValueOnce({ data: { token: 'voice-scoped-token' } });
    await expect(fetchVoiceToken()).resolves.toBe('voice-scoped-token');
    expect(post).toHaveBeenCalledWith('/api/ws/token', null, { params: { purpose: 'voice' } });
  });

  it('returns null when the backend reports WS auth disabled (token: null)', async () => {
    post.mockResolvedValueOnce({ data: { token: null } });
    await expect(fetchWsToken()).resolves.toBeNull();
  });

  it('returns null on a request error (never falls back to the long-lived JWT)', async () => {
    post.mockRejectedValueOnce(new Error('network'));
    await expect(fetchWsToken()).resolves.toBeNull();
  });

  it('returns null when the response has no data', async () => {
    post.mockResolvedValueOnce({});
    await expect(fetchWsToken()).resolves.toBeNull();
  });
});
