/**
 * Where to go after signing in. Only paths inside this app are accepted, so a
 * crafted link such as /login?redirect=https://evil.example cannot send a
 * freshly signed-in user to another site.
 */
export function safeRedirect(target: unknown, fallback = '/'): string {
  if (typeof target !== 'string') return fallback
  if (!target.startsWith('/') || target.startsWith('//') || target.includes('\\')) return fallback
  return target
}
