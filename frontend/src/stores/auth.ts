import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { authApi, type RegisterInput } from '@/api/auth'
import { configureApiAuth } from '@/api/client'
import type { Membership, OrgRole, Session, User } from '@/types/api'

const ORG_PREFERENCE_KEY = 'qa-agent.organization'

/** Remembering which organization was open is a convenience, never a credential. */
function readPreferredOrg(): string | null {
  try {
    return localStorage.getItem(ORG_PREFERENCE_KEY)
  } catch {
    return null
  }
}

function writePreferredOrg(id: string | null): void {
  try {
    if (id) localStorage.setItem(ORG_PREFERENCE_KEY, id)
    else localStorage.removeItem(ORG_PREFERENCE_KEY)
  } catch {
    // Storage can be unavailable (private mode); the app works without it.
  }
}

/**
 * Session state.
 *
 * The access token lives only in this store's memory: it is never written to
 * localStorage or a readable cookie, so a script injected into the page cannot
 * lift a long-lived credential. Reloading the page gets a new access token from
 * the HttpOnly refresh cookie.
 */
export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(null)
  const user = ref<User | null>(null)
  const memberships = ref<Membership[]>([])
  const organizationId = ref<string | null>(null)
  const status = ref<'unknown' | 'authenticated' | 'anonymous'>('unknown')

  const isAuthenticated = computed(() => status.value === 'authenticated')
  const membership = computed(
    () => memberships.value.find((m) => m.organization_id === organizationId.value) ?? null,
  )
  const role = computed<OrgRole | null>(() => membership.value?.role ?? null)
  /** For showing or hiding controls only. The server enforces every permission. */
  const isAdmin = computed(() => role.value === 'owner' || role.value === 'admin')
  const canContribute = computed(() => role.value !== null && role.value !== 'viewer')

  function setMemberships(list: Membership[]): void {
    memberships.value = list
    const preferred = organizationId.value ?? readPreferredOrg()
    const match = list.find((m) => m.organization_id === preferred) ?? list[0]
    organizationId.value = match?.organization_id ?? null
  }

  function applySession(session: Session): void {
    accessToken.value = session.access_token
    user.value = session.user
    setMemberships(session.memberships)
    status.value = 'authenticated'
  }

  function clear(): void {
    accessToken.value = null
    user.value = null
    memberships.value = []
    organizationId.value = null
    status.value = 'anonymous'
  }

  // All callers share one refresh request: a refresh token works only once, so two
  // parallel refreshes would look like token theft and end the session.
  let refreshing: Promise<boolean> | null = null

  function refresh(): Promise<boolean> {
    refreshing ??= authApi
      .refresh()
      .then((token) => {
        accessToken.value = token.access_token
        return true
      })
      .catch(() => {
        clear()
        return false
      })
      .finally(() => {
        refreshing = null
      })
    return refreshing
  }

  let bootstrapping: Promise<void> | null = null

  /** Restore the session after a page load, if the refresh cookie is still valid. */
  function bootstrap(): Promise<void> {
    if (status.value !== 'unknown') return Promise.resolve()
    bootstrapping ??= (async () => {
      if (!(await refresh())) return
      try {
        const me = await authApi.me()
        user.value = me.user
        setMemberships(me.memberships)
        status.value = 'authenticated'
      } catch {
        clear()
      }
    })()
    return bootstrapping
  }

  async function login(email: string, password: string): Promise<void> {
    applySession(await authApi.login(email, password))
  }

  async function register(input: RegisterInput): Promise<void> {
    applySession(await authApi.register(input))
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } finally {
      clear()
    }
  }

  /** Read the profile and memberships again, e.g. after an organization was renamed. */
  async function reloadProfile(): Promise<void> {
    const me = await authApi.me()
    user.value = me.user
    setMemberships(me.memberships)
  }

  function selectOrganization(id: string): void {
    if (memberships.value.some((m) => m.organization_id === id)) {
      organizationId.value = id
      writePreferredOrg(id)
    }
  }

  configureApiAuth({ getAccessToken: () => accessToken.value, onUnauthorized: refresh })

  return {
    user,
    memberships,
    organizationId,
    status,
    isAuthenticated,
    membership,
    role,
    isAdmin,
    canContribute,
    bootstrap,
    applySession,
    reloadProfile,
    login,
    register,
    logout,
    refresh,
    selectOrganization,
  }
})
