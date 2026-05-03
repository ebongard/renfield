/**
 * Ambient type declaration for `@capacitor/core`.
 *
 * Capacitor is an OPTIONAL runtime dependency. `src/utils/platform.ts`
 * imports it via `await import('@capacitor/core')` inside a try/catch so
 * that PWA builds (which don't ship Capacitor) still work. We don't list
 * `@capacitor/core` in `package.json` because pulling it in would force
 * every Vite build to include it.
 *
 * This declaration mirrors only the surface that `platform.ts` actually
 * touches: `Capacitor.isNativePlatform()` and `Capacitor.getPlatform()`.
 *
 * If/when we adopt more of the Capacitor API (plugins, listeners, etc.),
 * extend this declaration — or, if Capacitor becomes a real dependency,
 * delete this file and rely on the package's bundled types.
 */

declare module '@capacitor/core' {
  export type CapacitorPlatform = 'web' | 'ios' | 'android';

  interface CapacitorGlobal {
    /** True when running inside a Capacitor native shell (iOS or Android). */
    isNativePlatform(): boolean;
    /** Returns the current runtime platform identifier. */
    getPlatform(): CapacitorPlatform;
  }

  export const Capacitor: CapacitorGlobal;
}
