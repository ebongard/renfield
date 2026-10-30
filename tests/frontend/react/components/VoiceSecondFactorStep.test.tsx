/**
 * The capture form for the second factor.
 *
 * 🛑 The two most important tests here check what the form does NOT do:
 *
 * * `does not surface the server message` -- the server answers every failure
 *   identically and deliberately uninformatively. If the interface passes that
 *   message through, it carries an oracle outwards the moment the server ever
 *   becomes more forthcoming. The protection has to stand on BOTH sides.
 * * `offers no way past the factor` -- there must be no "sign in without voice"
 *   button, not even after several failed attempts. A fallback an attacker can
 *   trigger themselves cancels the factor out.
 *
 * Microphone errors, by contrast, are DELIBERATELY distinguishable: they are
 * about the user's device, not about a secret of the server's.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';

// 🛑 With the REAL language pack, not with a stub. Otherwise `t()` returns the
// keys, every text search only checks key names, and a missing translation
// entry never shows up -- exactly the gap a test here is supposed to close.
import i18n from '../../../../src/frontend/src/i18n';

import VoiceSecondFactorStep from '../../../../src/frontend/src/components/auth/VoiceSecondFactorStep';
import apiClient from '../../../../src/frontend/src/utils/axios';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: { post: vi.fn() },
}));

/** A MediaRecorder stand-in that we clock by hand. */
class FakeRecorder {
  static instances: FakeRecorder[] = [];
  state: 'inactive' | 'recording' = 'inactive';
  mimeType = 'audio/webm';
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor(public stream: unknown) {
    FakeRecorder.instances.push(this);
  }

  start() {
    this.state = 'recording';
  }

  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({ data: new Blob(['audio'], { type: 'audio/webm' }) });
    this.onstop?.();
  }
}

function stubMedia({ fail }: { fail?: string } = {}) {
  const track = { stop: vi.fn() };
  const getUserMedia = fail
    ? vi.fn().mockRejectedValue(Object.assign(new Error('nope'), { name: fail }))
    : vi.fn().mockResolvedValue({ getTracks: () => [track] });
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia }, configurable: true, writable: true,
  });
  (globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = FakeRecorder;
  return { getUserMedia, track };
}

/** Start recording, skip past the minimum duration, stop. */
async function recordSomething() {
  fireEvent.click(screen.getByRole('button', { name: /aufnahme starten|start recording/i }));
  await waitFor(() => expect(FakeRecorder.instances.length).toBeGreaterThan(0));
  // The minimum duration is checked against the clock, not a timer -- fast-forward.
  vi.setSystemTime(Date.now() + 5000);
  FakeRecorder.instances.at(-1)!.stop();
  await waitFor(() =>
    expect(screen.getByRole('button', { name: /bestätigen|confirm/i })).toBeInTheDocument()
  );
}

/** Render with the real language pack in German. */
function renderStep(ui: React.ReactElement) {
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>);
}

describe('VoiceSecondFactorStep', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('de');
    FakeRecorder.instances = [];
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(apiClient.post).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sends the ticket and the recording to /api/auth/voice', async () => {
    stubMedia();
    vi.mocked(apiClient.post).mockResolvedValue({ data: { success: true } });
    const onVerified = vi.fn();

    renderStep(
      <VoiceSecondFactorStep ticket="TICKET-9" onVerified={onVerified} onCancel={vi.fn()} />
    );
    await recordSomething();
    fireEvent.click(screen.getByRole('button', { name: /bestätigen|confirm/i }));

    await waitFor(() => expect(apiClient.post).toHaveBeenCalled());
    const [url, form, config] = vi.mocked(apiClient.post).mock.calls[0];
    expect(url).toBe('/api/auth/voice');
    expect((form as FormData).get('ticket')).toBe('TICKET-9');
    expect((form as FormData).get('audio_file')).toBeInstanceOf(Blob);

    // 🛑 THIS LINE WAS MISSING, and it cost the first real login attempt
    // (2026-09-28): `POST /api/auth/voice` came back as a **422**, the voice
    // was never checked.
    //
    // `apiClient` carries `Content-Type: application/json` as its default
    // (`utils/axios.ts`). Axios only removes it for FormData when it is NOT set
    // explicitly -- here it stayed: body multipart, header claiming JSON,
    // FastAPI cannot read it.
    //
    // The test still ran green, because it only looked at `[url, form]` and
    // never checked the third argument -- the one the headers are in. All four
    // other uploads in the project set the header; this one was the outlier.
    expect((config as { headers?: Record<string, string> })?.headers?.['Content-Type'])
      .toBe('multipart/form-data');

    await waitFor(() => expect(onVerified).toHaveBeenCalled());
  });

  it('does not surface the server message on a refusal', async () => {
    stubMedia();
    // 🛑 The server deliberately sends the same meaningless text on every
    // failure. If the interface shows it, it carries the oracle outwards the
    // moment the server ever becomes more forthcoming.
    vi.mocked(apiClient.post).mockResolvedValue({
      data: { success: false, message: 'Voice authentication failed' },
    });

    renderStep(
      <VoiceSecondFactorStep ticket="T" onVerified={vi.fn()} onCancel={vi.fn()} />
    );
    await recordSomething();
    fireEvent.click(screen.getByRole('button', { name: /bestätigen|confirm/i }));

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.queryByText(/Voice authentication failed/)).toBeNull();
  });

  it('offers no way past the factor, even after repeated failures', async () => {
    stubMedia();
    vi.mocked(apiClient.post).mockResolvedValue({ data: { success: false } });
    const onVerified = vi.fn();

    renderStep(
      <VoiceSecondFactorStep ticket="T" onVerified={onVerified} onCancel={vi.fn()} />
    );

    for (let i = 0; i < 4; i += 1) {
      await recordSomething();
      fireEvent.click(screen.getByRole('button', { name: /bestätigen|confirm/i }));
      await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    }

    // After four failed attempts: no button that skips the voice.
    const labels = screen.getAllByRole('button').map((b) => b.textContent ?? '');
    expect(labels.join(' ')).not.toMatch(/ohne|skip|überspringen|without/i);
    expect(onVerified).not.toHaveBeenCalled();
  });

  it('names the recovery path after three failures', async () => {
    stubMedia();
    vi.mocked(apiClient.post).mockResolvedValue({ data: { success: false } });

    renderStep(
      <VoiceSecondFactorStep ticket="T" onVerified={vi.fn()} onCancel={vi.fn()} />
    );
    for (let i = 0; i < 3; i += 1) {
      await recordSomething();
      fireEvent.click(screen.getByRole('button', { name: /bestätigen|confirm/i }));
      await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    }
    // With a broken microphone the user should learn how to proceed -- without
    // the form itself offering them a way past it.
    expect(screen.getByRole('alert').textContent).toMatch(/administrativ|administrator/i);
  });

  it('distinguishes a denied microphone — that is the user device, not a secret', async () => {
    stubMedia({ fail: 'NotAllowedError' });

    renderStep(
      <VoiceSecondFactorStep ticket="T" onVerified={vi.fn()} onCancel={vi.fn()} />
    );
    fireEvent.click(screen.getByRole('button', { name: /aufnahme starten|start recording/i }));

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByRole('alert').textContent).toMatch(/mikrofon|microphone/i);
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('refuses to stop before the minimum duration', async () => {
    stubMedia();
    renderStep(
      <VoiceSecondFactorStep ticket="T" onVerified={vi.fn()} onCancel={vi.fn()} />
    );
    fireEvent.click(screen.getByRole('button', { name: /aufnahme starten|start recording/i }));

    // Immediately afterwards: the stop button is disabled and says why. Half a
    // second of audio makes for a poor comparison, and the user should notice
    // that before submitting instead of collecting a refusal.
    await waitFor(() => {
      const stop = screen.getByRole('button', { name: /weitersprechen|keep talking/i });
      expect(stop).toBeDisabled();
    });
  });

  it('releases the microphone when it unmounts', async () => {
    const { track } = stubMedia();
    const { unmount } = renderStep(
      <VoiceSecondFactorStep ticket="T" onVerified={vi.fn()} onCancel={vi.fn()} />
    );
    fireEvent.click(screen.getByRole('button', { name: /aufnahme starten|start recording/i }));
    await waitFor(() => expect(FakeRecorder.instances.length).toBeGreaterThan(0));

    unmount();
    // 🛑 On a LOGIN PAGE a secretly open microphone is the last thing one
    // wants -- the browser indicator would stay on.
    expect(track.stop).toHaveBeenCalled();
  });

  it('cancel hands control back without calling the API', () => {
    stubMedia();
    const onCancel = vi.fn();
    renderStep(
      <VoiceSecondFactorStep ticket="T" onVerified={vi.fn()} onCancel={onCancel} />
    );
    fireEvent.click(screen.getByRole('button', { name: /abbrechen|cancel/i }));
    expect(onCancel).toHaveBeenCalled();
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
