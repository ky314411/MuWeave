import { defineConfig } from '@playwright/test'
import { resolve } from 'node:path'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8000', trace: 'retain-on-failure' },
  webServer: {
    command:
      'uv run uvicorn app.main:create_app --factory --app-dir ../backend --host 127.0.0.1 --port 8000',
    url: 'http://127.0.0.1:8000/api/health',
    reuseExistingServer: false,
    env: { MUWEAVE_DATA_DIR: resolve('../app-state/e2e') },
    timeout: 60000,
  },
})
