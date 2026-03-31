/**
 * Platform detection for Capacitor native vs PWA.
 *
 * Usage:
 *   import { isNative, isIOS } from '@utils/platform';
 *   if (isNative) { // Use native plugins }
 */

let _isNative = false;
let _platform = 'web';

try {
  // Dynamic import to avoid build errors when Capacitor is not installed
  const { Capacitor } = await import('@capacitor/core');
  _isNative = Capacitor.isNativePlatform();
  _platform = Capacitor.getPlatform();
} catch {
  // Capacitor not available (running as PWA)
}

export const isNative = _isNative;
export const isIOS = _platform === 'ios';
export const isAndroid = _platform === 'android';
export const platform = _platform;
