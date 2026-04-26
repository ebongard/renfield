/// <reference types="vite/client" />

// Standard Vite client-types reference — makes `import.meta.env`,
// `import.meta.glob`, etc. resolve under TypeScript. Without this, every
// `.ts`/`.tsx` file that reads VITE_* env vars (axios.ts, debug.ts,
// useDeviceConnection.ts, LoginPage.tsx, …) fails type-checking with
// `Property 'env' does not exist on type 'ImportMeta'`. Vite the bundler
// always knows about these at build time; this file just teaches the
// type-checker what the bundler already does.
//
// Augment the env shape if/when we need typed access to specific
// VITE_* variables. Today the codebase reads them as `string | undefined`,
// which `vite/client` already provides.
