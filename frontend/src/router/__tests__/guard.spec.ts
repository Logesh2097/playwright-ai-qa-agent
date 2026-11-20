import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authApi } from '@/api/auth'
import { ApiError } from '@/api/client'
import { createAppRouter } from '@/router'
import { safeRedirect } from '@/router/redirect'

beforeEach(() => {
  setActivePinia(createPinia())
})

function signedOut(): void {
  vi.spyOn(authApi, 'refresh').mockRejectedValue(
    new ApiError({ status: 401, code: 'invalid_refresh_token', message: 'no session' }),
  )
}

function signedIn(): void {
  vi.spyOn(authApi, 'refresh').mockResolvedValue({
    access_token: 't',
    token_type: 'bearer',
    expires_in: 900,
  })
  vi.spyOn(authApi, 'me').mockResolvedValue({
    user: {
      id: 'u',
      email: 'a@b.io',
      full_name: 'A',
      is_active: true,
      created_at: '',
      last_login_at: null,
    },
    memberships: [],
  })
}

describe('route guard', () => {
  it('sends signed-out visitors to the login page and remembers where they were going', async () => {
    signedOut()
    const router = createAppRouter()
    await router.push('/projects/p1/applications/a1')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/projects/p1/applications/a1')
  })

  it('lets signed-out visitors open the public pages', async () => {
    signedOut()
    const router = createAppRouter()
    await router.push('/register')
    expect(router.currentRoute.value.name).toBe('register')
    await router.push('/accept-invite?token=abc')
    expect(router.currentRoute.value.name).toBe('register')
    expect(router.currentRoute.value.query.token).toBe('abc')
  })

  it('keeps signed-in users out of the login page and lets them into the app', async () => {
    signedIn()
    const router = createAppRouter()
    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('home')
    await router.push('/projects/p1')
    expect(router.currentRoute.value.name).toBe('project')
    expect(router.currentRoute.value.params.projectId).toBe('p1')
  })
})

describe('safeRedirect', () => {
  it('accepts paths inside the app', () => {
    expect(safeRedirect('/projects/p1?tab=members')).toBe('/projects/p1?tab=members')
  })

  it.each([
    'https://evil.example',
    '//evil.example/path',
    '/\\evil.example',
    'javascript:alert(1)',
    'projects',
    '',
    undefined,
    ['/a', '/b'],
  ])('falls back for %j', (target) => {
    expect(safeRedirect(target)).toBe('/')
  })
})
