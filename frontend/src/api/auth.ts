import { api } from '@/api/client'
import type { Me, Session, TokenResponse } from '@/types/api'

export interface RegisterInput {
  email: string
  password: string
  full_name: string
  organization_name?: string
  invite_token?: string
}

export const authApi = {
  register: (input: RegisterInput) => api.post<Session>('/auth/register', input),
  login: (email: string, password: string) => api.post<Session>('/auth/login', { email, password }),
  /** Uses the HttpOnly refresh cookie; there is nothing to pass. */
  refresh: () => api.post<TokenResponse>('/auth/refresh'),
  logout: () => api.post<void>('/auth/logout'),
  me: () => api.get<Me>('/auth/me'),
  /** Always answers the same way, whether or not the address has an account. */
  forgotPassword: (email: string) => api.post<unknown>('/auth/password/forgot', { email }),
  resetPassword: (token: string, newPassword: string) =>
    api.post<void>('/auth/password/reset', { token, new_password: newPassword }),
  changePassword: (currentPassword: string, newPassword: string) =>
    api.post<Session>('/auth/password/change', {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  logoutAll: () => api.post<void>('/auth/logout-all'),
}
