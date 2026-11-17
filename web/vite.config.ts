import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';
import { mockApiPlugin } from './dev/middleware.ts';

export default defineConfig(({ command, mode }) => ({
  appType: 'spa',
  plugins: [
    svelte(),
    ...(command === 'serve' && mode === 'development' ? [mockApiPlugin()] : []),
  ],
  build: { license: { fileName: 'licenses/npm.md' } },
  server: {
    host: '127.0.0.1',
    proxy: mode === 'server'
      ? { '/api/v1': process.env.LOCALAI_API_URL ?? 'http://127.0.0.1:8730' }
      : undefined,
  },
  preview: { host: '127.0.0.1' },
}));
