import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

// Unit tests run in a plain Node environment. CSS processing is disabled so
// vitest does not try to load the project's Tailwind v4 postcss.config.mjs
// (the tests never import stylesheets).
export default defineConfig({
  // Inline (empty) PostCSS config so vite does not load the project's
  // Tailwind v4 postcss.config.mjs, which fails outside the Next build.
  css: {
    postcss: { plugins: [] },
  },
  test: {
    environment: 'node',
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
});
