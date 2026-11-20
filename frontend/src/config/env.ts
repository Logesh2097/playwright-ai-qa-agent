/** Typed access to build-time environment configuration. */

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '')
}

export const env = {
  apiBaseUrl: trimTrailingSlash(import.meta.env.VITE_API_BASE_URL || '/api/v1'),
  appName: import.meta.env.VITE_APP_NAME || 'QA Agent',
  isDev: import.meta.env.DEV,
} as const
