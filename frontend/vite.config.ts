import { defineConfig } from 'vitest/config'
import { loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'MUWEAVE_')
  return {
    plugins: [vue()],
    server: {
      port: 5173,
      strictPort: true,
      watch: { usePolling: env.MUWEAVE_POLLING === 'true' },
      proxy: {
        '/api': {
          target: env.MUWEAVE_API_PROXY || 'http://127.0.0.1:8000',
          changeOrigin: false,
        },
      },
    },
    test: { include: ['src/**/*.test.ts'] },
  }
})
