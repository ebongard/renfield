/**
 * The per-device haptics switch: off by default, persisted in localStorage,
 * honest about browsers that cannot vibrate and about a storage that refuses
 * the write.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import HapticFeedbackSetting from '../../../../src/frontend/src/components/HapticFeedbackSetting';
import { HAPTICS_STORAGE_KEY } from '../../../../src/frontend/src/utils/haptics';
import { renderWithProviders } from '../test-utils';
import {
  blockStorage,
  installVibrate,
  removeVibrate,
  resetHapticsEnvironment,
  setHapticsPreference,
  setReducedMotion,
} from '../haptics-test-helpers';

const UNSUPPORTED = /Dieser Browser kann nicht vibrieren/;
const REDUCED_MOTION = /„Bewegung reduzieren“ aktiv/;
const NOT_SAVED = /konnte in diesem Browser nicht gespeichert werden/;

function theSwitch(): HTMLElement {
  return screen.getByRole('switch', { name: 'Haptisches Feedback' });
}

afterEach(resetHapticsEnvironment);

describe('HapticFeedbackSetting', () => {
  it('is off by default', () => {
    installVibrate();
    renderWithProviders(<HapticFeedbackSetting />);
    expect(theSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(theSwitch()).toBeEnabled();
    expect(screen.getByText('Aus')).toBeInTheDocument();
    expect(screen.queryByText(UNSUPPORTED)).not.toBeInTheDocument();
  });

  it('switching on persists the preference and confirms with a tap', async () => {
    const vibrate = installVibrate();
    renderWithProviders(<HapticFeedbackSetting />);
    await userEvent.click(theSwitch());
    expect(theSwitch()).toHaveAttribute('aria-checked', 'true');
    expect(window.localStorage.getItem(HAPTICS_STORAGE_KEY)).toBe('true');
    expect(screen.getByText('Ein — dieses Gerät vibriert zur Rückmeldung')).toBeInTheDocument();
    expect(vibrate).toHaveBeenCalledWith(10);
  });

  it('switching off persists and stays silent', async () => {
    const vibrate = installVibrate();
    setHapticsPreference(true);
    renderWithProviders(<HapticFeedbackSetting />);
    expect(theSwitch()).toHaveAttribute('aria-checked', 'true');
    await userEvent.click(theSwitch());
    expect(theSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(window.localStorage.getItem(HAPTICS_STORAGE_KEY)).toBe('false');
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('survives a remount (the stored preference is the source)', async () => {
    installVibrate();
    const first = renderWithProviders(<HapticFeedbackSetting />);
    await userEvent.click(theSwitch());
    first.unmount();
    renderWithProviders(<HapticFeedbackSetting />);
    expect(theSwitch()).toHaveAttribute('aria-checked', 'true');
  });

  it('is disabled with an explanation where the Vibration API does not exist', async () => {
    removeVibrate();
    // Even a preference carried over from elsewhere must not read as "on" here.
    setHapticsPreference(true);
    renderWithProviders(<HapticFeedbackSetting />);
    expect(theSwitch()).toBeDisabled();
    expect(theSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(UNSUPPORTED)).toBeInTheDocument();
    // A click on the disabled switch changes nothing.
    await userEvent.click(theSwitch());
    expect(window.localStorage.getItem(HAPTICS_STORAGE_KEY)).toBe('true');
  });

  it('explains that reduced motion keeps the vibration off', async () => {
    const vibrate = installVibrate();
    setReducedMotion(true);
    renderWithProviders(<HapticFeedbackSetting />);
    expect(screen.getByText(REDUCED_MOTION)).toBeInTheDocument();
    await userEvent.click(theSwitch());
    // The preference itself is still the person's to set ...
    expect(theSwitch()).toHaveAttribute('aria-checked', 'true');
    // ... but nothing vibrates while the OS asks for reduced motion.
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('shows no reduced-motion note without the OS setting', () => {
    installVibrate();
    renderWithProviders(<HapticFeedbackSetting />);
    expect(screen.queryByText(REDUCED_MOTION)).not.toBeInTheDocument();
  });

  it('a blocked storage leaves the switch off and says so', async () => {
    const vibrate = installVibrate();
    blockStorage('setItem');
    renderWithProviders(<HapticFeedbackSetting />);
    await userEvent.click(theSwitch());
    expect(theSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(NOT_SAVED)).toBeInTheDocument();
    expect(vibrate).not.toHaveBeenCalled();
  });

  it('a throwing storage read renders as off instead of crashing', () => {
    installVibrate();
    setHapticsPreference(true);
    blockStorage('getItem');
    renderWithProviders(<HapticFeedbackSetting />);
    expect(theSwitch()).toHaveAttribute('aria-checked', 'false');
  });
});
