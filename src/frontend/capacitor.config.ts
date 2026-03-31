import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'de.renfield.app',
  appName: 'Renfield',
  webDir: 'dist',
  server: {
    iosScheme: 'https',
  },
};

export default config;
