import { describe, it, expect, beforeEach } from 'vitest'
import { withSessionRefresh } from '../sessionRefresh'

// The access token lives 30 minutes and the client stored a refresh token it
// never used, so every signed-in user was logged out mid-chat under the
// "Session expired" banner (found 2026-09-20). These pin the wrapper that
// closes that: one refresh, one replay, and - the part that matters most -
// ONE refresh in flight however many requests 401 at once, because the route
// treats a rotated refresh token presented twice as theft and revokes every
// session the user has.

type Call = { url: string; init?: RequestInit; bearer: string | null; ctype: string | null; body: string | null }

function bearer(init?: RequestInit, input?: RequestInfo | URL): string | null {
  const h = new Headers(input instanceof Request ? input.headers : undefined)
  new Headers(init?.headers).forEach((v, k) => h.set(k, v))
  const v = h.get('authorization') || ''
  return v.startsWith('Bearer ') ? v.slice(7) : null
}

// A scripted backend: `api` answers protected calls by the bearer they carry,
// `refresh` answers the refresh route. Every call is recorded.
function fakeFetch(opts: {
  valid: string
  refresh?: (req: { bearer: string | null }) => Response | Promise<Response>
  delayRefreshMs?: number
}) {
  const calls: Call[] = []
  const fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const b = bearer(init, input)
    const h = new Headers(input instanceof Request ? input.headers : undefined)
    new Headers(init?.headers).forEach((v, k) => h.set(k, v))
    const body = input instanceof Request ? await input.clone().text() : typeof init?.body === 'string' ? init.body : null
    calls.push({ url, init, bearer: b, ctype: h.get('content-type'), body })
    if (url.endsWith('/api/auth/refresh')) {
      if (opts.delayRefreshMs) await new Promise(r => setTimeout(r, opts.delayRefreshMs))
      return opts.refresh
        ? opts.refresh({ bearer: b })
        : new Response(JSON.stringify({ access_token: 'access-2', refresh_token: 'refresh-2' }), { status: 200 })
    }
    if (b === opts.valid) return new Response(JSON.stringify({ ok: true }), { status: 200 })
    return new Response(JSON.stringify({ detail: 'Invalid or expired token' }), { status: 401 })
  }) as typeof globalThis.fetch
  return { fetch, calls }
}

const refreshCalls = (calls: Call[]) => calls.filter(c => c.url.endsWith('/api/auth/refresh'))

describe('withSessionRefresh', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('az_jwt_token', 'access-1')
    localStorage.setItem('az_jwt_refresh', 'refresh-1')
  })

  it('on a 401 with the current token: one refresh, one replay with the new token, storage rotated', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch)
    const r = await f('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(200)
    expect(calls.map(c => [c.url, c.bearer])).toEqual([
      ['/api/kb/files', 'access-1'],
      [`${location.origin}/api/auth/refresh`, 'refresh-1'],
      ['/api/kb/files', 'access-2'],
    ])
    expect(localStorage.getItem('az_jwt_token')).toBe('access-2')
    expect(localStorage.getItem('az_jwt_refresh')).toBe('refresh-2')
  })

  it('concurrent 401s share ONE refresh (a rotated token presented twice reads as theft server-side)', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2', delayRefreshMs: 20 })
    const f = withSessionRefresh(fetch)
    const auth = { headers: { Authorization: 'Bearer access-1' } }
    const results = await Promise.all([
      f('/api/kb/files', auth), f('/api/analytics', auth), f('/api/history', auth),
      f('/api/users', auth), f('/api/admin/connectors', auth),
    ])
    expect(results.map(r => r.status)).toEqual([200, 200, 200, 200, 200])
    expect(refreshCalls(calls)).toHaveLength(1)
    const replays = calls.filter(c => c.bearer === 'access-2')
    expect(replays).toHaveLength(5)
  })

  it('a request whose bearer is already stale replays with the stored token and refreshes nothing', async () => {
    localStorage.setItem('az_jwt_token', 'access-2')   // someone else refreshed already
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch)
    const r = await f('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(200)
    expect(refreshCalls(calls)).toHaveLength(0)
    expect(calls[calls.length - 1].bearer).toBe('access-2')
  })

  it('a refused refresh returns the ORIGINAL 401, drops the dead refresh token, keeps the access token for the banner', async () => {
    const { fetch, calls } = fakeFetch({
      valid: 'never',
      refresh: () => new Response(JSON.stringify({ detail: 'Invalid or revoked refresh token' }), { status: 401 }),
    })
    const f = withSessionRefresh(fetch)
    const r = await f('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(401)
    expect(await r.json()).toEqual({ detail: 'Invalid or expired token' })
    expect(calls.map(c => c.url)).toEqual(['/api/kb/files', `${location.origin}/api/auth/refresh`])   // no replay
    expect(localStorage.getItem('az_jwt_refresh')).toBeNull()
    expect(localStorage.getItem('az_jwt_token')).toBe('access-1')
  })

  it('a refresh that never reached the server keeps the refresh token for the next 401', async () => {
    const { fetch } = fakeFetch({ valid: 'never', refresh: () => { throw new TypeError('Failed to fetch') } })
    const f = withSessionRefresh(fetch)
    const r = await f('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(401)
    expect(localStorage.getItem('az_jwt_refresh')).toBe('refresh-1')
  })

  it('replays ONCE: a replay that still 401s is returned as it is, with no second refresh', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'never' })   // refresh "succeeds" but the new token is refused too
    const f = withSessionRefresh(fetch)
    const r = await f('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(401)
    expect(refreshCalls(calls)).toHaveLength(1)
    expect(calls).toHaveLength(3)
  })

  it('leaves a 401 with no bearer alone (login, setup, the authless demo path)', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-1' })
    const f = withSessionRefresh(fetch)
    const r = await f('/api/auth/login', { method: 'POST', body: '{}' })
    expect(r.status).toBe(401)
    expect(calls).toHaveLength(1)
  })

  it('leaves a 401 alone when the user signed out between send and reply', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const slow = (async (input: RequestInfo | URL, init?: RequestInit) => {
      localStorage.removeItem('az_jwt_token')
      return fetch(input, init)
    }) as typeof globalThis.fetch
    const r = await withSessionRefresh(slow)('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(401)
    expect(refreshCalls(calls)).toHaveLength(0)
  })

  it('sends the refresh to the configured API base with the prefix the failing request used', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch, { apiBase: 'https://api.example.com' })
    await f('https://api.example.com/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(refreshCalls(calls)[0].url).toBe('https://api.example.com/api/auth/refresh')
  })

  it('never sends the refresh token to an origin the access token should not have gone to', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch, { apiBase: 'https://api.example.com' })
    const r = await f('https://evil.example.net/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(401)
    expect(refreshCalls(calls)).toHaveLength(0)
    expect(localStorage.getItem('az_jwt_refresh')).toBe('refresh-1')
  })

  it('two tabs past expiry refresh ONCE between them: the second waits on the lock, then reuses the rotated token', async () => {
    // A fake Web Lock shared by both "tabs": callbacks run one at a time per name.
    const queues = new Map<string, Promise<unknown>>()
    const locks = {
      request: (name: string, fn: () => Promise<unknown>) => {
        const prev = queues.get(name) ?? Promise.resolve()
        const next = prev.then(fn, fn)
        queues.set(name, next.catch(() => undefined))
        return next
      },
    }
    Object.defineProperty(navigator, 'locks', { value: locks, configurable: true })
    try {
      const { fetch, calls } = fakeFetch({ valid: 'access-2', delayRefreshMs: 20 })
      const tabA = withSessionRefresh(fetch)
      const tabB = withSessionRefresh(fetch)   // separate in-process state, shared storage and lock
      const auth = { headers: { Authorization: 'Bearer access-1' } }
      const [a, b] = await Promise.all([tabA('/api/history', auth), tabB('/api/analytics', auth)])
      expect([a.status, b.status]).toEqual([200, 200])
      expect(refreshCalls(calls)).toHaveLength(1)   // a second refresh with refresh-1 would revoke every session
      expect(calls.filter(c => c.bearer === 'access-2')).toHaveLength(2)
    } finally {
      Object.defineProperty(navigator, 'locks', { value: undefined, configurable: true })
    }
  })

  it('replays a Request object with its body and its other headers intact', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch)
    // Node's Request wants an absolute URL; the browser resolves a relative one.
    const req = new Request(`${location.origin}/api/chat`, {
      method: 'POST',
      headers: { Authorization: 'Bearer access-1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ q: 'hello' }),
    })
    const r = await f(req)
    expect(r.status).toBe(200)
    const replay = calls[calls.length - 1]
    expect(replay.url).toBe(`${location.origin}/api/chat`)
    expect(refreshCalls(calls)[0].url).toBe(`${location.origin}/api/auth/refresh`)
    expect(replay.bearer).toBe('access-2')
    expect(replay.ctype).toBe('application/json')
    expect(replay.body).toBe(JSON.stringify({ q: 'hello' }))
  })
  it('builds the refresh URL from the PATH prefix, so a query value cannot steer it', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch)
    const r = await f('/api/kb/files?next=/api/evil/', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(200)
    expect(refreshCalls(calls)[0].url).toBe(`${location.origin}/api/auth/refresh`)
  })

  it('keeps the mounted API prefix of the failing request', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch)
    await f('/tenant-a/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(refreshCalls(calls)[0].url).toBe(`${location.origin}/tenant-a/api/auth/refresh`)
  })

  it('never replays a bootstrap route even when a call site sent it a bearer (a replay is a second failed attempt)', async () => {
    const { fetch, calls } = fakeFetch({ valid: 'access-2' })
    const f = withSessionRefresh(fetch)
    for (const path of ['/api/auth/login', '/api/auth/setup', '/api/auth/mfa/complete']) {
      calls.length = 0
      const r = await f(path, { method: 'POST', body: '{}', headers: { Authorization: 'Bearer access-1' } })
      expect(r.status).toBe(401)
      expect(calls).toHaveLength(1)
    }
    expect(localStorage.getItem('az_jwt_refresh')).toBe('refresh-1')
  })

  it('keeps the refresh token when the refresh never reached the rotation (429 throttle, 502, 503) and drops it on 504', async () => {
    for (const [status, kept] of [[429, true], [502, true], [503, true], [504, false], [500, false]] as const) {
      localStorage.setItem('az_jwt_token', 'access-1')
      localStorage.setItem('az_jwt_refresh', 'refresh-1')
      const { fetch } = fakeFetch({ valid: 'never', refresh: () => new Response('', { status }) })
      const r = await withSessionRefresh(fetch)('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
      expect(r.status).toBe(401)
      expect(localStorage.getItem('az_jwt_refresh')).toBe(kept ? 'refresh-1' : null)
    }
  })

  it('drops the refresh token when the rotation answers without a new one (the old one is revoked either way)', async () => {
    const { fetch, calls } = fakeFetch({
      valid: 'access-2',
      refresh: () => new Response(JSON.stringify({ access_token: 'access-2' }), { status: 200 }),
    })
    const r = await withSessionRefresh(fetch)('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
    expect(r.status).toBe(200)
    expect(calls[calls.length - 1].bearer).toBe('access-2')
    expect(localStorage.getItem('az_jwt_token')).toBe('access-2')
    expect(localStorage.getItem('az_jwt_refresh')).toBeNull()
  })

  it('falls back to an unlocked refresh when the lock request itself fails, instead of rejecting the fetch', async () => {
    Object.defineProperty(navigator, 'locks', {
      value: { request: () => Promise.reject(new DOMException('document is not fully active', 'InvalidStateError')) },
      configurable: true,
    })
    try {
      const { fetch, calls } = fakeFetch({ valid: 'access-2' })
      const r = await withSessionRefresh(fetch)('/api/kb/files', { headers: { Authorization: 'Bearer access-1' } })
      expect(r.status).toBe(200)
      expect(refreshCalls(calls)).toHaveLength(1)
    } finally {
      Object.defineProperty(navigator, 'locks', { value: undefined, configurable: true })
    }
  })
})
