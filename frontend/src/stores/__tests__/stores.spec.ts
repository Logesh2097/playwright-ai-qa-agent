import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import * as systemApi from '@/api/system'
import { useNotificationsStore } from '@/stores/notifications'
import { useSystemStore } from '@/stores/system'
import type { Meta, Readiness } from '@/types/api'

const meta: Meta = { name: 'qa-agent', version: '0.1.0', environment: 'test', api_version: 'v1' }
const ready: Readiness = {
  status: 'ready',
  checks: { postgres: { status: 'ok', latency_ms: 1.2, error: null } },
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('system store', () => {
  it('is ready when the readiness probe reports ready', async () => {
    vi.spyOn(systemApi, 'fetchMeta').mockResolvedValue(meta)
    vi.spyOn(systemApi, 'fetchReadiness').mockResolvedValue(ready)
    const store = useSystemStore()

    expect(store.state).toBe('unknown')
    await store.refresh()

    expect(store.state).toBe('ready')
    expect(store.meta).toEqual(meta)
    expect(store.loading).toBe(false)
    expect(store.checkedAt).toBeInstanceOf(Date)
  })

  it('is degraded when a dependency is down', async () => {
    vi.spyOn(systemApi, 'fetchMeta').mockResolvedValue(meta)
    vi.spyOn(systemApi, 'fetchReadiness').mockResolvedValue({ ...ready, status: 'not_ready' })
    const store = useSystemStore()

    await store.refresh()

    expect(store.state).toBe('degraded')
    expect(store.error).toBeNull()
  })

  it('is unreachable, with a readable message, when the API cannot be reached', async () => {
    vi.spyOn(systemApi, 'fetchMeta').mockRejectedValue(
      new ApiError({ status: 0, code: 'network_error', message: 'Could not reach the server.' }),
    )
    vi.spyOn(systemApi, 'fetchReadiness').mockResolvedValue(ready)
    const store = useSystemStore()

    await store.refresh()

    expect(store.state).toBe('unreachable')
    expect(store.error).toBe('Could not reach the server.')
    expect(store.readiness).toBeNull()
  })
})

describe('fetchReadiness', () => {
  it('treats a 503 carrying a readiness report as an answer, not an error', async () => {
    const report: Readiness = { ...ready, status: 'not_ready' }
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify(report), {
            status: 503,
            headers: { 'content-type': 'application/json' },
          }),
      ),
    )

    await expect(systemApi.fetchReadiness()).resolves.toEqual(report)
    vi.unstubAllGlobals()
  })
})

describe('notifications store', () => {
  it('adds, de-duplicates and dismisses notifications', () => {
    const store = useNotificationsStore()

    const first = store.error('Request failed', 'Request abc')
    const duplicate = store.error('Request failed')
    store.error('Another problem')

    expect(duplicate).toBe(first)
    expect(store.items).toHaveLength(2)
    expect(store.items[0]?.reference).toBe('Request abc')

    store.dismiss(first)
    expect(store.items.map((item) => item.message)).toEqual(['Another problem'])
  })

  it('auto-dismisses non-error notifications only', () => {
    vi.useFakeTimers()
    const store = useNotificationsStore()

    store.success('Saved')
    store.error('Failed')
    vi.advanceTimersByTime(10_000)

    expect(store.items.map((item) => item.kind)).toEqual(['error'])
    vi.useRealTimers()
  })
})
