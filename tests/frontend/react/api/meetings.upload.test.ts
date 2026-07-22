import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the shared axios instance so we can inspect the request config.
vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: { post: vi.fn().mockResolvedValue({ data: { id: 1, status: 'pending' } }) },
}));

import apiClient from '../../../../src/frontend/src/utils/axios';
import { uploadMeetingRequest } from '../../../../src/frontend/src/api/resources/meetings';

describe('meeting upload request', () => {
  beforeEach(() => vi.mocked(apiClient.post).mockClear());

  it('disables the request timeout so large recordings are not cut off at 30s', async () => {
    const audio = new File([new Uint8Array(10)], 'besprechung.m4a', { type: 'audio/mp4' });
    await uploadMeetingRequest({ audio, consentConfirmed: true, title: 'x' });

    expect(apiClient.post).toHaveBeenCalledTimes(1);
    const [url, body, config] = vi.mocked(apiClient.post).mock.calls[0];
    expect(url).toBe('/api/meetings/transcribe');
    expect(body).toBeInstanceOf(FormData);
    // The load-bearing bit: no 30s cap on the (potentially hundreds-of-MB) upload.
    expect(config?.timeout).toBe(0);
  });

  it('sends the consent flag and audio in the multipart body', async () => {
    const audio = new File([new Uint8Array(10)], 'a.wav', { type: 'audio/wav' });
    await uploadMeetingRequest({ audio, consentConfirmed: true });
    const body = vi.mocked(apiClient.post).mock.calls[0][1] as FormData;
    expect(body.get('consent_confirmed')).toBe('true');
    expect(body.get('audio')).toBeInstanceOf(File);
  });

  it('includes the ASR language when set, omits it otherwise', async () => {
    const audio = new File([new Uint8Array(10)], 'a.wav', { type: 'audio/wav' });
    await uploadMeetingRequest({ audio, consentConfirmed: true, language: 'en' });
    let body = vi.mocked(apiClient.post).mock.calls[0][1] as FormData;
    expect(body.get('language')).toBe('en');

    vi.mocked(apiClient.post).mockClear();
    await uploadMeetingRequest({ audio, consentConfirmed: true });
    body = vi.mocked(apiClient.post).mock.calls[0][1] as FormData;
    expect(body.get('language')).toBeNull();
  });
});
