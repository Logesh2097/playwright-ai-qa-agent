import { onBeforeUnmount, ref } from 'vue'

import { executionsApi } from '@/api/admin'
import { api, ApiError, isAbortError } from '@/api/client'
import type { LiveEvent, LiveState } from '@/types/admin'

export type Connection = 'connecting' | 'live' | 'reconnecting' | 'polling' | 'closed'

const MAX_EVENTS = 400
const RETRY_MS = [1000, 2000, 5000, 10000]

/** Splits a server-sent-event stream into `{event, data, id}` records. Exported for tests. */
export function parseSseChunk(buffer: string): { records: Record<string, string>[]; rest: string } {
  const blocks = buffer.split('\n\n')
  const rest = blocks.pop() ?? ''
  const records = blocks
    .map((block) => {
      const record: Record<string, string> = {}
      for (const line of block.split('\n')) {
        if (!line || line.startsWith(':')) continue
        const at = line.indexOf(':')
        const field = at === -1 ? line : line.slice(0, at)
        const value = at === -1 ? '' : line.slice(at + 1).replace(/^ /, '')
        record[field] = field === 'data' && record.data ? `${record.data}\n${value}` : value
      }
      return record
    })
    .filter((record) => record.event || record.data)
  return { records, rest }
}

/**
 * Follows one execution live.
 *
 * Everything shown comes from the server: the state it sends and the entries the
 * engine reported. Nothing is interpolated or guessed while waiting. If the
 * stream breaks it is opened again from the last entry received; if that keeps
 * failing, the state is polled instead, and the page says which of the two it is.
 */
export function useExecutionStream(orgId: string, projectId: string, executionId: string) {
  const state = ref<LiveState | null>(null)
  const events = ref<LiveEvent[]>([])
  const connection = ref<Connection>('connecting')
  const error = ref<string | null>(null)

  let controller: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let lastSeq = 0
  let failures = 0
  let stopped = false

  function accept(record: Record<string, string>): boolean {
    if (!record.data) return false
    const data = JSON.parse(record.data) as unknown
    if (record.event === 'state') state.value = data as LiveState
    if (record.event === 'step') {
      const entry = data as LiveEvent
      if (entry.seq > lastSeq) {
        lastSeq = entry.seq
        events.value = [...events.value, entry].slice(-MAX_EVENTS)
      }
    }
    return record.event === 'end'
  }

  async function poll(): Promise<void> {
    if (stopped) return
    try {
      state.value = await executionsApi.live(orgId, projectId, executionId)
      error.value = null
      if (state.value.finished) {
        connection.value = 'closed'
        return
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : 'The status could not be read.'
    }
    timer = setTimeout(() => void connect(), 3000)
  }

  async function connect(): Promise<void> {
    if (stopped) return
    controller = new AbortController()
    try {
      const response = await api.open(executionsApi.eventsPath(orgId, projectId, executionId), {
        signal: controller.signal,
        headers: lastSeq ? { 'Last-Event-ID': String(lastSeq) } : {},
      })
      const reader = response.body?.getReader()
      if (!reader) throw new Error('This browser cannot read a live stream.')
      connection.value = 'live'
      error.value = null
      failures = 0
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parsed = parseSseChunk(buffer)
        buffer = parsed.rest
        for (const record of parsed.records) {
          if (accept(record)) {
            connection.value = 'closed'
            return
          }
        }
      }
      // The server closes a stream after a while; an execution still running is followed again.
      if (state.value?.finished) connection.value = 'closed'
      else schedule(0)
    } catch (cause) {
      if (stopped || isAbortError(cause)) return
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        // Not there, or not for this person: trying again would not change that.
        error.value = cause.message
        connection.value = 'closed'
        return
      }
      failures += 1
      error.value = cause instanceof Error ? cause.message : 'The live connection was lost.'
      if (failures > RETRY_MS.length) {
        connection.value = 'polling'
        await poll()
      } else {
        schedule(RETRY_MS[failures - 1] ?? 10000)
      }
    }
  }

  function schedule(delay: number): void {
    if (stopped) return
    connection.value = delay === 0 ? 'connecting' : 'reconnecting'
    timer = setTimeout(() => void connect(), delay)
  }

  function stop(): void {
    stopped = true
    controller?.abort()
    if (timer !== null) clearTimeout(timer)
  }

  onBeforeUnmount(stop)
  return { state, events, connection, error, start: connect, stop }
}
