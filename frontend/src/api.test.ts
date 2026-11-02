import { afterEach, expect, test, vi } from 'vitest'
import { api, ApiError } from './api'

afterEach(() => vi.unstubAllGlobals())

test('version conflicts preserve the server status for editor recovery', async () => {
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: 'Version conflict' }), { status: 409 }))
  await expect(api('/test', { content: 'draft' })).rejects.toMatchObject({ status: 409, message: 'Version conflict' })
})

test('non JSON server errors produce an actionable error instead of a JSON parse failure', async () => {
  vi.stubGlobal('fetch', async () => new Response('proxy unavailable', { status: 502 }))
  await expect(api('/test')).rejects.toBeInstanceOf(ApiError)
})
