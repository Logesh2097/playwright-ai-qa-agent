import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authApi } from '@/api/auth'
import { ApiError, createApiClient } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import type { Membership, Session, User } from '@/types/api'

const user: User = {
  id: 'u1',
  email: 'asha@acme.io',
  full_name: 'Asha Rao',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  last_login_at: null,
}
const acme: Membership = {
  organization_id: 'org-a',
  organization_name: 'Acme',
  organization_slug: 'acme',
  role: 'developer',
}
const other: Membership = {
  ...acme,
  organization_id: 'org-b',
  organization_name: 'Other',
  role: 'owner',
}
const session: Session = {
  access_token: 'access-1',
  token_type: 'bearer',
  expires_in: 900,
  user,
  memberships: [acme, other],
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
})

describe('auth store', () => {
  it('signs in and exposes the role of the selected organization', async () => {
    vi.spyOn(authApi, 'login').mockResolvedValue(session)
    const auth = useAuthStore()

    await auth.login('asha@acme.io', 'pw')

    expect(auth.isAuthenticated).toBe(true)
    expect(auth.organizationId).toBe('org-a')
    expect(auth.role).toBe('developer')
    expect(auth.canContribute).toBe(true)
    expect(auth.isAdmin).toBe(false)

    auth.selectOrganization('org-b')
    expect(auth.isAdmin).toBe(true)
    auth.selectOrganization('org-of-someone-else')
    expect(auth.organizationId).toBe('org-b')
  })

  it('never writes the access token or the user to browser storage', async () => {
    vi.spyOn(authApi, 'login').mockResolvedValue(session)
    const auth = useAuthStore()
    await auth.login('asha@acme.io', 'pw')
    auth.selectOrganization('org-b')

    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage })
    expect(stored).not.toContain('access-1')
    expect(stored).not.toContain('asha@acme.io')
    // Only the organization preference is remembered.
    expect(Object.keys(localStorage)).toEqual(['qa-agent.organization'])
    // The token is not reachable through the store's public surface either.
    expect(JSON.stringify(auth.$state)).not.toContain('access-1')
  })

  it('restores the session on page load from the refresh cookie', async () => {
    vi.spyOn(authApi, 'refresh').mockResolvedValue({
      access_token: 'access-2',
      token_type: 'bearer',
      expires_in: 900,
    })
    const me = vi.spyOn(authApi, 'me').mockResolvedValue({ user, memberships: [acme] })
    const auth = useAuthStore()

    await Promise.all([auth.bootstrap(), auth.bootstrap()])

    expect(auth.isAuthenticated).toBe(true)
    expect(auth.user?.email).toBe('asha@acme.io')
    expect(me).toHaveBeenCalledOnce()
  })

  it('is anonymous when there is no valid refresh cookie', async () => {
    vi.spyOn(authApi, 'refresh').mockRejectedValue(
      new ApiError({ status: 401, code: 'invalid_refresh_token', message: 'expired' }),
    )
    const auth = useAuthStore()
    await auth.bootstrap()
    expect(auth.status).toBe('anonymous')
    expect(auth.isAuthenticated).toBe(false)
  })

  it('shares one refresh request between concurrent callers', async () => {
    // A refresh token works once: two parallel refreshes would end the session.
    let resolve!: (value: {
      access_token: string
      token_type: 'bearer'
      expires_in: number
    }) => void
    const refresh = vi
      .spyOn(authApi, 'refresh')
      .mockReturnValue(new Promise((done) => (resolve = done)))
    const auth = useAuthStore()

    const calls = [auth.refresh(), auth.refresh(), auth.refresh()]
    resolve({ access_token: 'access-3', token_type: 'bearer', expires_in: 900 })

    expect(await Promise.all(calls)).toEqual([true, true, true])
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('clears everything on sign-out, even if the request fails', async () => {
    vi.spyOn(authApi, 'login').mockResolvedValue(session)
    vi.spyOn(authApi, 'logout').mockRejectedValue(new Error('offline'))
    const auth = useAuthStore()
    await auth.login('asha@acme.io', 'pw')

    await expect(auth.logout()).rejects.toThrow('offline')

    expect(auth.isAuthenticated).toBe(false)
    expect(auth.user).toBeNull()
    expect(auth.memberships).toEqual([])
  })
})

describe('api client with authentication', () => {
  function json(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })
  }

  it('refreshes once and repeats the request after a 401', async () => {
    let token = 'expired'
    const fetchImpl = vi.fn<typeof fetch>(async (_url, init) => {
      const header = (init?.headers as Record<string, string>).Authorization
      return header === 'Bearer fresh'
        ? json(200, { ok: true })
        : json(401, { code: 'token_expired', title: 'Authentication required' })
    })
    const onUnauthorized = vi.fn(async () => {
      token = 'fresh'
      return true
    })
    const client = createApiClient({
      baseUrl: '',
      fetchImpl,
      getAccessToken: () => token,
      onUnauthorized,
    })

    await expect(client.get('/x')).resolves.toEqual({ ok: true })
    expect(onUnauthorized).toHaveBeenCalledOnce()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it('gives up after one retry and does not refresh for anonymous requests', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      json(401, { code: 'invalid_credentials', title: 'Authentication required' }),
    )
    const onUnauthorized = vi.fn(async () => true)

    const signedIn = createApiClient({
      baseUrl: '',
      fetchImpl,
      getAccessToken: () => 'still-bad',
      onUnauthorized,
    })
    await expect(signedIn.get('/x')).rejects.toMatchObject({ status: 401 })
    expect(fetchImpl).toHaveBeenCalledTimes(2)

    // A failed login (no token yet) must not trigger a refresh attempt.
    const anonymous = createApiClient({ baseUrl: '', fetchImpl, onUnauthorized })
    await expect(anonymous.post('/auth/login', {})).rejects.toMatchObject({
      code: 'invalid_credentials',
    })
    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it('sends the CSRF header the API requires on cookie endpoints', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => json(200, {}))
    await createApiClient({ baseUrl: '', fetchImpl }).post('/auth/refresh')
    const headers = fetchImpl.mock.calls[0]![1]!.headers as Record<string, string>
    expect(headers['X-Requested-With']).toBeTruthy()
  })
})
