import { env } from '@/config/env'
import type { ErrorItem, ProblemDetail } from '@/types/api'

export type QueryValue = string | number | boolean | null | undefined

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: Record<string, QueryValue>
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
}

export interface ApiClientConfig {
  baseUrl: string
  fetchImpl?: typeof fetch
  /** Supplies the bearer token for each request. */
  getAccessToken?: () => string | null
  /**
   * Called when a request is rejected with 401. Returns true if a new access
   * token was obtained, in which case the request is sent once more.
   */
  onUnauthorized?: () => Promise<boolean>
}

/** Every failed request is surfaced as this one error type. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: string | null
  readonly errors: ErrorItem[]
  readonly requestId: string | null
  /** Parsed response body, when there was one. */
  readonly body: unknown

  constructor(init: {
    status: number
    code: string
    message: string
    detail?: string | null
    errors?: ErrorItem[]
    requestId?: string | null
    body?: unknown
  }) {
    super(init.message)
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code
    this.detail = init.detail ?? null
    this.errors = init.errors ?? []
    this.requestId = init.requestId ?? null
    this.body = init.body
  }

  get isNetworkError(): boolean {
    return this.status === 0
  }
}

/** Name check rather than `instanceof`: DOMException differs between realms. */
export function isAbortError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as Error).name === 'AbortError'
}

function isProblemDetail(value: unknown): value is ProblemDetail {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as ProblemDetail).code === 'string' &&
    typeof (value as ProblemDetail).title === 'string'
  )
}

function buildUrl(baseUrl: string, path: string, query?: Record<string, QueryValue>): string {
  const url = `${baseUrl}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined) params.set(key, String(value))
  }
  const search = params.toString()
  return search ? `${url}?${search}` : url
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined
  const text = await response.text()
  if (!text) return undefined
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('json')) return text
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

export function createApiClient(config: ApiClientConfig) {
  async function request<T>(
    path: string,
    options: RequestOptions = {},
    isRetry = false,
  ): Promise<T> {
    // Resolved per call so tests and polyfills can replace the global.
    const doFetch = config.fetchImpl ?? globalThis.fetch
    const headers: Record<string, string> = {
      Accept: 'application/json',
      // Required by the API on cookie-authenticated endpoints (CSRF protection).
      'X-Requested-With': 'qa-agent-web',
      ...options.headers,
    }
    if (options.body !== undefined) headers['Content-Type'] = 'application/json'
    const token = config.getAccessToken?.()
    if (token) headers.Authorization = `Bearer ${token}`

    let response: Response
    try {
      response = await doFetch(buildUrl(config.baseUrl, path, options.query), {
        method: options.method ?? 'GET',
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: options.signal,
      })
    } catch (cause) {
      if (isAbortError(cause)) throw cause
      throw new ApiError({
        status: 0,
        code: 'network_error',
        message: 'Could not reach the server. Check your connection and try again.',
      })
    }

    const body = await parseBody(response)
    if (response.ok) return body as T

    // An expired access token: get a new one and repeat the request, once.
    if (response.status === 401 && !isRetry && token && config.onUnauthorized) {
      if (await config.onUnauthorized()) return request<T>(path, options, true)
    }

    const requestId = response.headers.get('x-request-id')
    if (isProblemDetail(body)) {
      throw new ApiError({
        status: response.status,
        code: body.code,
        message: body.detail ?? body.title,
        detail: body.detail,
        errors: body.errors,
        requestId: body.request_id ?? requestId,
        body,
      })
    }
    throw new ApiError({
      status: response.status,
      code: 'http_error',
      message: `Request failed with status ${response.status}.`,
      requestId,
      body,
    })
  }

  /** A binary response (an evidence file, an exported report), with its suggested file name. */
  async function download(
    path: string,
    query?: Record<string, QueryValue>,
    isRetry = false,
  ): Promise<{ blob: Blob; filename: string | null }> {
    const doFetch = config.fetchImpl ?? globalThis.fetch
    const headers: Record<string, string> = { 'X-Requested-With': 'qa-agent-web' }
    const token = config.getAccessToken?.()
    if (token) headers.Authorization = `Bearer ${token}`
    let response: Response
    try {
      response = await doFetch(buildUrl(config.baseUrl, path, query), { headers })
    } catch {
      throw new ApiError({
        status: 0,
        code: 'network_error',
        message: 'Could not reach the server. Check your connection and try again.',
      })
    }
    if (response.status === 401 && !isRetry && token && config.onUnauthorized) {
      if (await config.onUnauthorized()) return download(path, query, true)
    }
    if (!response.ok) {
      const body = await parseBody(response)
      throw new ApiError({
        status: response.status,
        code: isProblemDetail(body) ? body.code : 'http_error',
        message: isProblemDetail(body)
          ? (body.detail ?? body.title)
          : `Request failed with status ${response.status}.`,
        body,
      })
    }
    const disposition = response.headers.get('content-disposition') ?? ''
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? null
    return { blob: await response.blob(), filename }
  }

  /**
   * An open response whose body is read as it arrives (server-sent events).
   * Authenticated like every other request; an expired token is renewed once.
   */
  async function open(
    path: string,
    init: { headers?: Record<string, string>; signal?: AbortSignal } = {},
    isRetry = false,
  ): Promise<Response> {
    const doFetch = config.fetchImpl ?? globalThis.fetch
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'X-Requested-With': 'qa-agent-web',
      ...init.headers,
    }
    const token = config.getAccessToken?.()
    if (token) headers.Authorization = `Bearer ${token}`
    let response: Response
    try {
      response = await doFetch(buildUrl(config.baseUrl, path), { headers, signal: init.signal })
    } catch (cause) {
      if (isAbortError(cause)) throw cause
      throw new ApiError({
        status: 0,
        code: 'network_error',
        message: 'Could not reach the server. Check your connection and try again.',
      })
    }
    if (response.status === 401 && !isRetry && token && config.onUnauthorized) {
      if (await config.onUnauthorized()) return open(path, init, true)
    }
    if (!response.ok) {
      const body = await parseBody(response)
      throw new ApiError({
        status: response.status,
        code: isProblemDetail(body) ? body.code : 'http_error',
        message: isProblemDetail(body)
          ? (body.detail ?? body.title)
          : `Request failed with status ${response.status}.`,
        body,
      })
    }
    return response
  }

  return {
    open: (path: string, init?: { headers?: Record<string, string>; signal?: AbortSignal }) =>
      open(path, init),
    download: (path: string, query?: Record<string, QueryValue>) => download(path, query),
    request: <T>(path: string, options?: RequestOptions) => request<T>(path, options),
    get: <T>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
      request<T>(path, { ...options, method: 'GET' }),
    post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
      request<T>(path, { ...options, method: 'POST', body }),
    put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
      request<T>(path, { ...options, method: 'PUT', body }),
    patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
      request<T>(path, { ...options, method: 'PATCH', body }),
    delete: <T = void>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
      request<T>(path, { ...options, method: 'DELETE' }),
  }
}

export type ApiClient = ReturnType<typeof createApiClient>

/**
 * How the shared client authenticates. Filled in by the auth store at start-up;
 * kept here so the client does not have to import the store.
 */
const authHooks: Pick<ApiClientConfig, 'getAccessToken' | 'onUnauthorized'> = {}

export function configureApiAuth(hooks: typeof authHooks): void {
  Object.assign(authHooks, hooks)
}

/** Client for the versioned API (`/api/v1`). */
export const api = createApiClient({
  baseUrl: env.apiBaseUrl,
  getAccessToken: () => authHooks.getAccessToken?.() ?? null,
  onUnauthorized: () => authHooks.onUnauthorized?.() ?? Promise.resolve(false),
})

/** Client for unversioned operational endpoints (`/healthz`, `/readyz`). */
export const rootApi = createApiClient({ baseUrl: '' })
