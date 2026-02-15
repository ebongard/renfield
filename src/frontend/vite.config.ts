import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';
import path from 'path';

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
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@components': path.resolve(__dirname, './src/components'),
      '@pages': path.resolve(__dirname, './src/pages'),
      '@hooks': path.resolve(__dirname, './src/hooks'),
      '@context': path.resolve(__dirname, './src/context'),
      '@utils': path.resolve(__dirname, './src/utils'),
      '@types': path.resolve(__dirname, './src/types'),
      // Force WASM-only bundle (no JSEP/WebGPU) — JSEP crashes Safari
      'onnxruntime-web': 'onnxruntime-web/wasm',
    },
  },
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
        // Exclude large WASM files from precaching (ONNX Runtime)
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff,woff2}'],
        // Skip large files silently instead of erroring
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024, // 5MB
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
    // Allow access from local network hostnames (Vite 5.4+ security feature)
    allowedHosts: ['renfield.local', 'localhost', '127.0.0.1', '.local'],
    watch: {
      usePolling: true
    },
    fs: {
      // Allow serving files from the public/ort directory
      allow: ['..']
    },
    // Security headers for development (OWASP recommendations)
    headers: {
      'X-Content-Type-Options': 'nosniff',
      'X-Frame-Options': 'DENY',
      'X-XSS-Protection': '1; mode=block',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
      'Permissions-Policy': 'accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(self), payment=(), usb=()',
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
      'Cross-Origin-Resource-Policy': 'same-origin',
      // CSP for development - more permissive to allow HMR
      'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss: http://localhost:* ws://localhost:*; media-src 'self' blob:; worker-src 'self' blob:; frame-ancestors 'none';"
    }
  },
  optimizeDeps: {
    exclude: ['onnxruntime-web']
  },
  assetsInclude: ['**/*.wasm'],
  preview: {
    port: 3000,
    // Security headers for preview/production (OWASP recommendations)
    headers: {
      'X-Content-Type-Options': 'nosniff',
      'X-Frame-Options': 'DENY',
      'X-XSS-Protection': '1; mode=block',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
      'Permissions-Policy': 'accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(self), payment=(), usb=()',
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
      'Cross-Origin-Resource-Policy': 'same-origin',
      'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:; media-src 'self' blob:; worker-src 'self' blob:; frame-ancestors 'none';"
    }
  }
});
