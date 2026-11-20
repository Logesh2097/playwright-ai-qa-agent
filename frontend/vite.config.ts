import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  // Where the dev server forwards API calls. Not exposed to the browser bundle.
  const proxyTarget = env.DEV_PROXY_TARGET || 'http://localhost:8000'
  // xfwd passes the browser's address on, so API rate limits apply per client.
  const proxy = { target: proxyTarget, changeOrigin: true, xfwd: true }

  return {
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      host: true,
      port: 5173,
      strictPort: true,
      proxy: { '/api': proxy, '/healthz': proxy, '/readyz': proxy },
    },
    test: {
      environment: 'jsdom',
      include: ['src/**/*.spec.ts'],
      restoreMocks: true,
    },
  }
})
