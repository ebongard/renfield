import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// Custom plugin to serve /ort/*.mjs files as static assets without import analysis
const ortStaticPlugin = () => ({
  name: 'ort-static',
  configureServer(server) {
    server.middlewares.use((req, res, next) => {
      // Strip ?import query from /ort/ requests so Vite serves them as static files
      if (req.url && req.url.startsWith('/ort/') && req.url.includes('?')) {
        req.url = req.url.split('?')[0];
      }
      next();
    });
  },
});

export default defineConfig({
  plugins: [
    ortStaticPlugin(),
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.ico', 'robots.txt', 'apple-touch-icon.png'],
      manifest: {
        name: 'Renfield AI Assistant',
        short_name: 'Renfield',
        description: 'Persönlicher KI-Assistent für Smart Home',
        theme_color: '#1f2937',
        background_color: '#111827',
        display: 'standalone',
        orientation: 'portrait',
        icons: [
          {
            src: '/icon-192.png',
            sizes: '192x192',
            type: 'image/png'
          },
          {
            src: '/icon-512.png',
            sizes: '512x512',
            type: 'image/png'
          }
        ]
      },
      workbox: {
        runtimeCaching: [
          {
            urlPattern: /^https:\/\/api\./,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-cache',
              expiration: {
                maxEntries: 50,
                maxAgeSeconds: 300
              }
            }
          }
        ]
      }
    })
  ],
  server: {
    host: true,
    port: 3000,
    watch: {
      usePolling: true
    },
    fs: {
      // Allow serving files from the public/ort directory
      allow: ['..']
    }
  },
  optimizeDeps: {
    exclude: ['onnxruntime-web']
  },
  assetsInclude: ['**/*.wasm'],
  preview: {
    port: 3000
  }
});
