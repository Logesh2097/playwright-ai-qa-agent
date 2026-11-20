import { defineStore } from 'pinia'
import { ref } from 'vue'

export type NotificationKind = 'info' | 'success' | 'error'

export interface AppNotification {
  id: number
  kind: NotificationKind
  message: string
  /** Shown in small print, e.g. a request ID to quote to support. */
  reference?: string
}

const AUTO_DISMISS_MS = 6000
const MAX_VISIBLE = 4

export const useNotificationsStore = defineStore('notifications', () => {
  const items = ref<AppNotification[]>([])
  let nextId = 1

  function dismiss(id: number): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  function push(kind: NotificationKind, message: string, reference?: string): number {
    // The same error repeated (e.g. from a polling loop) is shown once.
    const existing = items.value.find((item) => item.kind === kind && item.message === message)
    if (existing) return existing.id

    const id = nextId++
    items.value = [...items.value, { id, kind, message, reference }].slice(-MAX_VISIBLE)
    if (kind !== 'error') setTimeout(() => dismiss(id), AUTO_DISMISS_MS)
    return id
  }

  return {
    items,
    dismiss,
    push,
    info: (message: string) => push('info', message),
    success: (message: string) => push('success', message),
    error: (message: string, reference?: string) => push('error', message, reference),
  }
})
