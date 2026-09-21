// Silent session refresh at the fetch layer.
//
// THE GAP THIS CLOSES (found 2026-09-20 on a fork, present here too): the
// access token lives ACCESS_EXPIRE_MINUTES (30 by default), login stores a
// refresh token beside it, and nothing in the client ever used that token - so
// every signed-in user was logged out mid-chat after 30 minutes under the
// "Session expired" banner. The server side was complete all along:
// /api/auth/refresh rotates the pair, revokes the old refresh token, and reads
// a revoked token presented again as theft.
//
// WHY THE FETCH LAYER, NOT THE CALL SITES: the client makes its API calls from
// half a dozen files with no shared request helper, each building its own
// Authorization header. Wrapping window.fetch once at boot covers every one of
// them - the chat stream included - without touching a call site, and covers
// the call site somebody adds next month. The mobile client that shares this
// backend has the same semantics in its own authFetch: one refresh, one replay,
// logout only when the refresh itself fails.
//
// THE SERVER-SIDE HALF THIS DEPENDS ON: every route behind the auth middleware
// answers an expired bearer with 401 before any handler runs. The chat route is
// middleware-exempt (guests may reach it), so its handler has to produce that
// 401 itself for a PRESENTED-but-expired token instead of the guest 403 - see
// optional_user and the "auth_token_invalid" check in the chat router. Without
// that seam an idle session dies on its first message while every other
// request refreshes fine.
//
// THE RULES:
//   1. Only a 401 whose request carried a bearer token is ours. A request with
//      no bearer (login, setup, the authless demo path) is returned untouched.
//      The refresh call is made through the underlying fetch, so it can never
//      be intercepted by this wrapper. The bootstrap routes that write a
//      failed-attempt counter BEFORE their 401 (login, setup, the MFA
//      completion) are never replayed even if a call site someday sends them
//      a bearer: a replay there would be a second failed attempt.
//   2. If the request's bearer is already stale - another request refreshed
//      while this one was in flight - replay with the stored token. No second
//      refresh.
//   3. ONE refresh at a time, ACROSS TABS. The route rotates the refresh token
//      and revokes the old one, and a revoked token presented again is treated
//      as theft: it revokes EVERY session for that user and records a security
//      event. Two tabs both polling past the 30-minute mark would 401 within
//      the same tick and each present the same refresh token - the second one
//      would sign the user out of everything, by their own browser's hand.
//      So the refresh runs under a Web Lock shared by every tab of this origin
//      (navigator.locks), and the first thing it does inside the lock is
//      re-read storage: if another tab already rotated the pair, use its
//      access token and do not refresh. Within one tab, concurrent 401s share
//      a single in-flight promise. Web Locks exist only in secure contexts
//      (https, localhost); a plain-http deployment gets the re-read without
//      the lock, which narrows the window rather than closing it. A lock
//      request that itself fails falls back the same way.
//   4. ONE replay. If the replay 401s too, that response goes back to the
//      caller as it is; the guarded helpers raise the banner from there. A
//      handler that answers 401 for a reason other than expiry (a wrong TOTP
//      code on MFA enable) costs one needless rotation and one replay - the
//      sessions list will show the rotation; it is not suspicious.
//   5. A REFUSED refresh drops the refresh token - it is dead: revoked,
//      expired or rejected - and returns the ORIGINAL 401 unchanged. The
//      access token stays in storage: emitAuthExpired needs it there to tell
//      a lost session from "never signed in". A refresh that never reached
//      the rotation - network failure, the refresh throttle (429), a gateway
//      with no backend behind it (502, 503) - keeps the refresh token, so the
//      next 401 tries again. A 504 is NOT in that list: the server may have
//      rotated before the gateway gave up, and a kept token whose fate is
//      unknown trips reuse detection on its next use.
//   6. The refresh token goes ONLY where the access token was allowed to go:
//      the page's own origin, or the configured API base (VITE_API_URL). A
//      401 from any other origin is returned untouched, so a stray call site
//      that sent a bearer somewhere it should not have cannot also hand that
//      origin the refresh token. The refresh URL keeps the failing request's
//      PATH prefix (read from the parsed pathname, never the raw string, so a
//      query value cannot steer it), so a client built against a remote or
//      mounted API base needs nothing more.

const ACCESS_KEY = 'az_jwt_token'
const REFRESH_KEY = 'az_jwt_refresh'
const REFRESH_PATH = '/api/auth/refresh'
const LOCK_NAME = 'az-session-refresh'
const INSTALLED_FLAG = '__azSessionRefreshInstalled'

// Rule 1: paths whose 401 follows a failed-attempt write. Matched on the
// path after the API prefix.
const NEVER_REPLAY = ['/api/auth/login', '/api/auth/setup', '/api/auth/mfa/complete']

// Rule 5: refresh responses that prove the rotation never happened.
const KEEP_REFRESH_TOKEN_ON = new Set([429, 502, 503])

type FetchLike = typeof fetch

export type SessionRefreshOptions = {
  // Absolute API base when the API is not served from the page's own origin
  // (the VITE_API_URL build setting). Empty or unset = same origin only.
  apiBase?: string
}

function read(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch { /* storage unavailable: nothing to persist */ }
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.href
  return input.url
}

function parse(url: string): URL | null {
  try {
    const base = typeof location !== 'undefined' ? location.href : undefined
    return new URL(url, base)
  } catch {
    return null
  }
}

function bearerOf(headers: Headers): string | null {
  const v = headers.get('authorization') || ''
  return v.startsWith('Bearer ') ? v.slice(7).trim() : null
}

// The headers a request was actually sent with: the Request object's own
// (when the caller passed one) overlaid by init.headers, matching what fetch
// itself does.
function sentHeaders(input: RequestInfo | URL, init: RequestInit | undefined): Headers {
  const h = new Headers(input instanceof Request ? input.headers : undefined)
  new Headers(init?.headers).forEach((value, name) => h.set(name, value))
  return h
}

type LockRequest = <T>(name: string, fn: () => Promise<T>) => Promise<T>

function lockRequest(): LockRequest | null {
  if (typeof navigator === 'undefined') return null
  const locks = (navigator as Navigator & { locks?: { request?: unknown } }).locks
  if (!locks || typeof locks.request !== 'function') return null
  return (name, fn) => (locks.request as LockRequest)(name, fn)
}

export function withSessionRefresh(base: FetchLike, opts: SessionRefreshOptions = {}): FetchLike {
  let inflight: Promise<string | null> | null = null
  const apiOrigin = opts.apiBase ? parse(opts.apiBase)?.origin ?? null : null

  // Returns the access token to replay with, or null when there is none.
  // `sent` is the token the failing request carried.
  const refresh = (refreshUrl: string, sent: string): Promise<string | null> => {
    if (inflight) return inflight
    const run = async (): Promise<string | null> => {
      // Inside the lock (or, without Web Locks, as late as possible): another
      // tab may have rotated the pair already. Its access token is the one to
      // use, and the refresh token it holds is the only one still valid.
      const now = read(ACCESS_KEY)
      if (now && now !== sent) return now
      const refreshToken = read(REFRESH_KEY)
      if (!refreshToken) return null
      try {
        const r = await base(refreshUrl, {
          method: 'POST',
          headers: { Authorization: `Bearer ${refreshToken}` },
        })
        if (!r.ok) {
          if (!KEEP_REFRESH_TOKEN_ON.has(r.status)) write(REFRESH_KEY, null)
          return null
        }
        const data = await r.json().catch(() => null) as { access_token?: string; refresh_token?: string } | null
        if (!data?.access_token) { write(REFRESH_KEY, null); return null }
        write(ACCESS_KEY, data.access_token)
        // The old refresh token is revoked by the rotation whatever the body
        // says; a server that omits the new one leaves us with nothing valid,
        // and keeping the old one would trip reuse detection on its next use.
        write(REFRESH_KEY, data.refresh_token ?? null)
        return data.access_token
      } catch {
        return null
      }
    }
    const lock = lockRequest()
    const attempt = lock ? lock(LOCK_NAME, run).catch(() => run()) : run()
    inflight = attempt.finally(() => { inflight = null })
    return inflight
  }

  return async (input, init) => {
    // A Request object's body is a one-shot stream; clone BEFORE the first send
    // or there is nothing left to replay.
    const spare = input instanceof Request ? input.clone() : null
    const res = await base(input, init)
    if (res.status !== 401) return res

    const headers = sentHeaders(input, init)
    const sent = bearerOf(headers)
    if (!sent) return res                                  // rule 1
    const current = read(ACCESS_KEY)
    if (!current) return res                               // signed out meanwhile

    const u = parse(urlOf(input))
    const pageOrigin = typeof location !== 'undefined' ? location.origin : null
    if (!u || (u.origin !== pageOrigin && u.origin !== apiOrigin)) return res   // rule 6
    const apiAt = u.pathname.indexOf('/api/')
    if (apiAt < 0) return res
    const route = u.pathname.slice(apiAt)
    if (NEVER_REPLAY.some(p => route === p || route.startsWith(p + '/'))) return res   // rule 1

    const token = current !== sent                          // rule 2
      ? current
      : await refresh(u.origin + u.pathname.slice(0, apiAt) + REFRESH_PATH, sent)   // rule 3
    if (!token) return res                                 // rule 5

    headers.set('Authorization', `Bearer ${token}`)
    if (spare) return base(new Request(spare, { ...init, headers }))
    return base(input, { ...init, headers })               // rule 4: one replay
  }
}

// Called once at boot, before the first render. Idempotent across module
// re-evaluation too (dev HMR): the flag lives on window, not in this module,
// so a re-evaluated copy cannot wrap the wrapper and buy a second replay.
export function installSessionRefresh() {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return
  const w = window as Window & { [INSTALLED_FLAG]?: true }
  if (w[INSTALLED_FLAG]) return
  w[INSTALLED_FLAG] = true
  window.fetch = withSessionRefresh(window.fetch.bind(window), {
    apiBase: import.meta.env.VITE_API_URL || '',
  })
}
