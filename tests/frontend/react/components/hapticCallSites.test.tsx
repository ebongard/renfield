/**
 * Haptics at the call sites outside the chat composer (#84): each one fires its
 * pattern from the shared handler when the preference is on, and stays silent
 * when it is off.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { act, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import DeviceControlArtifact from '../../../../src/frontend/src/components/chat/artifacts/DeviceControlArtifact';
import type { DeviceActionFn } from '../../../../src/frontend/src/components/chat/artifacts/DeviceControlArtifact';
import type { DeviceControlData } from '../../../../src/frontend/src/components/chat/artifacts/artifactSchema';
import ScanJobToast from '../../../../src/frontend/src/components/ScanJobToast';
import HomeAssistantPage from '../../../../src/frontend/src/pages/HomeAssistantPage';
import { SCAN_JOB_FINISHED_EVENT } from '../../../../src/frontend/src/hooks/useUserEvents';
import { useNotifications } from '../../../../src/frontend/src/hooks/useNotifications';
import type { NotificationData } from '../../../../src/frontend/src/hooks/useNotifications';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { renderWithProviders } from '../test-utils';
import {
  installVibrate,
  resetHapticsEnvironment,
  setHapticsPreference,
} from '../haptics-test-helpers';

// useNotifications only needs the ack sender from the device socket.
vi.mock('../../../../src/frontend/src/context/DeviceContext', () => ({
  useDevice: () => ({ sendNotificationAck: vi.fn() }),
}));

afterEach(resetHapticsEnvironment);

const DEVICES: DeviceControlData = {
  devices: [
    { entity_id: 'light.wz', domain: 'light', name: 'Licht WZ', state: 'off' },
    { entity_id: 'scene.abend', domain: 'scene', name: 'Abend', state: 'x' },
  ],
};

describe('device widget (DeviceControlArtifact)', () => {
  it('toggling a light taps, and so does running a scene', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    const onAction = vi.fn<DeviceActionFn>().mockResolvedValue({ success: true, state: 'on' });
    renderWithProviders(<DeviceControlArtifact data={DEVICES} onAction={onAction} />);

    await userEvent.click(screen.getByRole('switch', { name: 'Licht WZ' }));
    expect(onAction).toHaveBeenCalledWith('light.wz', 'toggle');
    expect(vibrate.mock.calls).toEqual([[10]]);

    await userEvent.click(screen.getByRole('button', { name: /Abend/ }));
    expect(onAction).toHaveBeenCalledWith('scene.abend', 'activate');
    expect(vibrate.mock.calls).toEqual([[10], [10]]);
  });

  it('stays silent with the preference off — the toggle itself still works', async () => {
    const vibrate = installVibrate();
    const onAction = vi.fn<DeviceActionFn>().mockResolvedValue({ success: true, state: 'on' });
    renderWithProviders(<DeviceControlArtifact data={DEVICES} onAction={onAction} />);
    await userEvent.click(screen.getByRole('switch', { name: 'Licht WZ' }));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(vibrate).not.toHaveBeenCalled();
  });
});

describe('smart-home page (HomeAssistantPage)', () => {
  function serveOneLight(): string[] {
    const toggled: string[] = [];
    server.use(
      http.get(`${BASE_URL}/api/homeassistant/states`, () =>
        HttpResponse.json({
          states: [{ entity_id: 'light.kueche', state: 'off', attributes: { friendly_name: 'Küche' } }],
        }),
      ),
      http.post<{ id: string }>(`${BASE_URL}/api/homeassistant/toggle/:id`, ({ params }) => {
        toggled.push(params.id);
        return HttpResponse.json({ success: true });
      }),
    );
    return toggled;
  }

  it('toggling an entity taps', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    serveOneLight();
    renderWithProviders(<HomeAssistantPage />);
    await userEvent.click(await screen.findByRole('listitem', { name: /Küche/ }));
    expect(vibrate.mock.calls).toEqual([[10]]);
  });

  it('stays silent with the preference off', async () => {
    const vibrate = installVibrate();
    serveOneLight();
    renderWithProviders(<HomeAssistantPage />);
    await userEvent.click(await screen.findByRole('listitem', { name: /Küche/ }));
    expect(vibrate).not.toHaveBeenCalled();
  });
});

describe('proactive notification (useNotifications)', () => {
  const NOTIFICATION: NotificationData = {
    notification_id: 7,
    title: 'Waschmaschine',
    message: 'fertig',
    urgency: 'critical',
    source: 'ha',
    room: null,
    tts_handled: false,
    created_at: '2026-11-11T10:00:00Z',
  };

  function deliver(): void {
    act(() => {
      window.dispatchEvent(new CustomEvent('renfield-notification', { detail: NOTIFICATION }));
    });
  }

  it('an incoming notification plays the notification pattern', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    const { result } = renderHook(() => useNotifications());
    deliver();
    expect(result.current.notifications).toHaveLength(1);
    expect(vibrate.mock.calls).toEqual([[[10, 30, 10, 30, 10]]]);
  });

  it('stays silent with the preference off — the toast still queues', () => {
    const vibrate = installVibrate();
    const { result } = renderHook(() => useNotifications());
    deliver();
    expect(result.current.notifications).toHaveLength(1);
    expect(vibrate).not.toHaveBeenCalled();
  });
});

describe('error toast (ScanJobToast)', () => {
  function announce(reason: string): void {
    act(() => {
      window.dispatchEvent(new CustomEvent(SCAN_JOB_FINISHED_EVENT, { detail: { reason } }));
    });
  }

  it('a failed scan buzzes with the error pattern', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    render(<ScanJobToast />);
    announce('failed');
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(vibrate.mock.calls).toEqual([[200]]);
  });

  it('a successful scan does not buzz', () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    render(<ScanJobToast />);
    announce('done');
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('a failed scan stays silent with the preference off', () => {
    const vibrate = installVibrate();
    render(<ScanJobToast />);
    announce('failed');
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(vibrate).not.toHaveBeenCalled();
  });
});
