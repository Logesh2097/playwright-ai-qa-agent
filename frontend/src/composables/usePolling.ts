import { onBeforeUnmount } from 'vue'

/**
 * Calls `tick` repeatedly while `shouldContinue` is true. Used to follow background
 * work (analysis, generation) until it finishes. Stops when the component goes away.
 */
export function usePolling(
  tick: () => Promise<void>,
  shouldContinue: () => boolean,
  intervalMs = 1500,
) {
  let timer: ReturnType<typeof setTimeout> | null = null
  let stopped = false

  function schedule(): void {
    if (stopped || timer !== null || !shouldContinue()) return
    timer = setTimeout(async () => {
      timer = null
      try {
        await tick()
      } finally {
        schedule()
      }
    }, intervalMs)
  }

  function stop(): void {
    stopped = true
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  onBeforeUnmount(stop)
  return { start: schedule, stop }
}
