import type { App } from 'vue'
import type { Router } from 'vue-router'

import { isAbortError } from '@/api/client'
import { toReference, toUserMessage } from '@/errors/messages'
import { useNotificationsStore } from '@/stores/notifications'

function report(error: unknown, source: string): void {
  if (isAbortError(error)) return
  console.error(`[${source}]`, error)
  useNotificationsStore().error(toUserMessage(error), toReference(error))
}

/**
 * Last line of defence: anything a component, a promise or the router fails to
 * handle is logged and shown as a notification instead of failing silently.
 * Must be installed after Pinia.
 */
export function installGlobalErrorHandlers(app: App, router: Router): void {
  app.config.errorHandler = (error) => report(error, 'vue')
  router.onError((error) => report(error, 'router'))
  window.addEventListener('unhandledrejection', (event) => {
    event.preventDefault()
    report(event.reason, 'promise')
  })
}
