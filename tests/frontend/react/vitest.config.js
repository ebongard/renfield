import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '../../..');
const frontendSrc = path.join(projectRoot, 'src/frontend/src');
const testNodeModules = path.join(__dirname, 'node_modules');

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Map source imports to the frontend src directory
      '@': frontendSrc,
      // Use single React version from test node_modules to avoid hook errors
      'react': path.join(testNodeModules, 'react'),
      'react-dom': path.join(testNodeModules, 'react-dom'),
      'react-router-dom': path.join(testNodeModules, 'react-router-dom'),
      'react-router': path.join(testNodeModules, 'react-router'),
      // i18next packages - use test node_modules versions
      'i18next': path.join(testNodeModules, 'i18next'),
      'react-i18next': path.join(testNodeModules, 'react-i18next'),
      'i18next-browser-languagedetector': path.join(testNodeModules, 'i18next-browser-languagedetector'),
      // lucide icons
      'lucide-react': path.join(testNodeModules, 'lucide-react'),
      // qrcode.react's real package resolves React from src/frontend/node_modules,
      // which is a different copy than the aliased test React → duplicate
      // React dispatchers → null useMemo. Point tests at a no-op stub.
      'qrcode.react': path.join(__dirname, 'stubs/qrcode.react.js'),
      // Same React-duplicate problem hits @tanstack/react-query (its
      // QueryClientProvider context closes over a React copy). Pin to the test
      // tree so providers and hooks share one React.
      '@tanstack/react-query': path.join(testNodeModules, '@tanstack/react-query'),
    },
  },
  server: {
    fs: {
      // Allow serving files from project root (for frontend source files)
      allow: [projectRoot],
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./setup.js'],
    include: ['./**/*.{test,spec}.{js,jsx,ts,tsx}'],
    testTimeout: 10000,
    pool: 'forks',
    isolate: false, // Run tests sequentially to avoid MSW handler conflicts
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      include: ['../../../src/frontend/src/**/*.{js,jsx}'],
      exclude: ['../../../src/frontend/src/main.jsx'],
    },
  },
});
