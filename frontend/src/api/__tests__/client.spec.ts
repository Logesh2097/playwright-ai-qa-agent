import { describe, expect, it, vi } from 'vitest'

import { ApiError, createApiClient } from '@/api/client'

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  })
}

function setup(response: Response | Error | DOMException, token: string | null = null) {
  const fetchImpl = vi.fn<typeof fetch>(async () => {
    // Not `instanceof Error`: jsdom's DOMException does not extend it.
    if (!(response instanceof Response)) throw response
    return response
  })
  const client = createApiClient({
    baseUrl: '/api/v1',
    fetchImpl,
    getAccessToken: () => token,
  })
  return { client, fetchImpl }
}

async function catchError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    if (error instanceof ApiError) return error
    throw error
  }
  throw new Error('expected the request to fail')
}

describe('api client', () => {
  it('returns parsed JSON and builds the URL from base, path and query', async () => {
    const { client, fetchImpl } = setup(jsonResponse({ name: 'qa-agent' }))

    const result = await client.get<{ name: string }>('/meta', {
      query: { limit: 10, cursor: null, q: 'a b' },
    })

    expect(result).toEqual({ name: 'qa-agent' })
    expect(fetchImpl).toHaveBeenCalledOnce()
    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe('/api/v1/meta?limit=10&q=a+b')
    expect(init?.method).toBe('GET')
  })

  it('sends a JSON body and the bearer token', async () => {
    const { client, fetchImpl } = setup(jsonResponse({ id: '1' }, { status: 201 }), 'token-123')

    await client.post('/projects', { name: 'Shop' })

    const init = fetchImpl.mock.calls[0]![1]!
    const headers = init.headers as Record<string, string>
    expect(init.body).toBe('{"name":"Shop"}')
    expect(headers['Content-Type']).toBe('application/json')
    expect(headers.Authorization).toBe('Bearer token-123')
  })

  it('omits the Authorization header when there is no token', async () => {
    const { client, fetchImpl } = setup(jsonResponse({}))
    await client.get('/meta')
    const headers = fetchImpl.mock.calls[0]![1]!.headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })

  it('returns undefined for 204 responses', async () => {
    const { client } = setup(new Response(null, { status: 204 }))
    await expect(client.delete('/projects/1')).resolves.toBeUndefined()
  })

  it('maps the error envelope to ApiError', async () => {
    const problem = {
      type: 'about:blank#validation_error',
      title: 'Validation failed',
      status: 422,
      code: 'validation_error',
      detail: 'base_url must use https',
      errors: [{ field: 'base_url', code: 'invalid_scheme', message: 'must use https' }],
      request_id: 'req-1',
    }
    const { client } = setup(
      jsonResponse(problem, {
        status: 422,
        headers: { 'content-type': 'application/problem+json' },
      }),
    )

    const error = await catchError(client.post('/environments', {}))

    expect(error.status).toBe(422)
    expect(error.code).toBe('validation_error')
    expect(error.message).toBe('base_url must use https')
    expect(error.errors).toHaveLength(1)
    expect(error.requestId).toBe('req-1')
  })

  it('handles error responses that are not the envelope', async () => {
    const { client } = setup(
      new Response('<html>Bad Gateway</html>', {
        status: 502,
        headers: { 'content-type': 'text/html', 'x-request-id': 'req-2' },
      }),
    )

    const error = await catchError(client.get('/meta'))

    expect(error.status).toBe(502)
    expect(error.code).toBe('http_error')
    expect(error.requestId).toBe('req-2')
  })

  it('reports network failures as ApiError with status 0', async () => {
    const { client } = setup(new TypeError('Failed to fetch'))

    const error = await catchError(client.get('/meta'))

    expect(error.isNetworkError).toBe(true)
    expect(error.code).toBe('network_error')
  })

  it('lets aborts propagate unchanged', async () => {
    const { client } = setup(new DOMException('aborted', 'AbortError'))
    await expect(client.get('/meta')).rejects.toMatchObject({ name: 'AbortError' })
  })
})
