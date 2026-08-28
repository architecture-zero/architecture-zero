import { useEffect, useState } from 'react'

// Shared error surface (error-masking sweep 2026-08-05). Contract:
//   - ACTIONS never look successful on a non-2xx: use actionError() and show
//     its returned text (server `detail` preferred) - the 2026-07-15 false-
//     "Saved" bug class, generalized.
//   - One-shot LOADS surface failures as a toast instead of silently rendering
//     empty UI: use guardedJson().
//   - Repeating POLLS stay quiet on transient failures (the next tick retries)
//     but a 401 is not transient: use guardedPoll() - it raises the sticky
//     session-expired banner, so an expired token reads as "session expired",
//     never as an app with no data (the invisible-pill class).
// The host component (<ErrorSurface/>) renders the toast stack + the expired
// banner; App mounts it once per visible view (views return exclusively, so
// the module-level subscriber is single at any moment).

type Sub = { toast: (msg: string) => void; expired: () => void }
let sub: Sub | null = null
const recent = new Map<string, number>()

export function emitError(msg: string) {
  const now = Date.now()
  if (now - (recent.get(msg) || 0) < 5000) return // dedupe bursts
  recent.set(msg, now)
  sub?.toast(msg)
}

export function emitAuthExpired() {
  sub?.expired()
}

export async function guardedJson<T = unknown>(p: Promise<Response>, what: string): Promise<T | null> {
  try {
    const r = await p
    if (r.status === 401) { emitAuthExpired(); return null }
    if (!r.ok) { emitError(`${what} failed (HTTP ${r.status})`); return null }
    return await r.json()
  } catch {
    emitError(`${what} failed - backend unreachable`)
    return null
  }
}

export async function guardedPoll<T = unknown>(p: Promise<Response>): Promise<T | null> {
  try {
    const r = await p
    if (r.status === 401) { emitAuthExpired(); return null }
    if (!r.ok) return null
    return await r.json()
  } catch {
    return null
  }
}

export async function actionError(p: Promise<Response>, what: string): Promise<string | null> {
  // null = success (2xx). Anything else returns display-ready failure text.
  try {
    const r = await p
    if (r.ok) return null
    if (r.status === 401) emitAuthExpired()
    let detail = ''
    try { detail = String((await r.json())?.detail || '') } catch { /* non-JSON body */ }
    return `${what} failed: ${detail || `HTTP ${r.status}`}`
  } catch {
    return `${what} failed - backend unreachable`
  }
}

export function ErrorSurface({ onLogout }: { onLogout?: () => void }) {
  const [toasts, setToasts] = useState<{ id: number; msg: string }[]>([])
  const [expired, setExpired] = useState(false)

  useEffect(() => {
    sub = {
      toast: (msg: string) => {
        const id = Date.now() + Math.random()
        setToasts(prev => [...prev.slice(-3), { id, msg }])
        setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 6000)
      },
      expired: () => setExpired(true),
    }
    return () => { sub = null }
  }, [])

  return (
    <>
      {expired && (
        <div className="fixed top-0 inset-x-0 z-50 bg-amber-900/90 border-b border-amber-700 px-4 py-2 flex items-center justify-center gap-3">
          <p className="text-sm text-amber-100">Session expired - your login is no longer valid.</p>
          {onLogout && (
            <button
              onClick={onLogout}
              className="text-sm text-amber-100 underline hover:text-white"
            >
              Log in again
            </button>
          )}
        </div>
      )}
      {toasts.length > 0 && (
        <div className="fixed bottom-4 right-4 z-50 space-y-2 max-w-sm">
          {toasts.map(t => (
            <div
              key={t.id}
              className="bg-red-900/90 border border-red-700/60 rounded-lg px-4 py-2 text-sm text-red-100 shadow-lg"
            >
              {t.msg}
            </div>
          ))}
        </div>
      )}
    </>
  )
}
