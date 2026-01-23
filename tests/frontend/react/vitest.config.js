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
