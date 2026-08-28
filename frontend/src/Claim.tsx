import { useState } from 'react'

/**
 * The first-Owner claim screen, served at #setup.
 *
 * This is the ONE screen a fresh deployment shows before anything else exists,
 * and the one account the platform cannot re-create: once an Owner exists,
 * POST /api/auth/setup refuses forever and the claim code is spent. So the
 * failure modes below are handled explicitly rather than folded into a generic
 * error line - each one leaves the operator in a different place and needs a
 * different next move.
 */

const API = import.meta.env.VITE_API_URL || ''

const inputClass =
  'w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-lg px-3 py-2 ' +
  'text-sm text-white placeholder-gray-500 outline-none transition-colors'

const primaryButtonClass =
  'w-full py-2.5 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-500 ' +
  'transition-colors disabled:opacity-40 disabled:cursor-not-allowed'

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-xs font-medium text-gray-400 mb-1.5">{label}</span>
      {children}
      {hint && <span className="block text-xs text-gray-600 mt-1">{hint}</span>}
    </label>
  )
}

export default function Claim() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [claimCode, setClaimCode] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      // The username is trimmed, not just checked for emptiness. A pasted name
      // with a trailing space passes the disabled-check below and would create
      // the one account this deployment cannot re-create under a name the
      // operator then mistypes at every sign-in.
      const name = username.trim()
      const res = await fetch(`${API}/api/auth/setup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: name, password, claim_code: claimCode.trim() }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        if (res.status === 429) {
          // Worth its own branch: the throttle counts ATTEMPTS, and the code
          // check runs inside it, so a mistyped code spends one of five. An
          // operator who reads this as "the server is busy" retries and spends
          // another.
          setError(
            `${data.detail || 'Too many attempts.'} Attempts are counted per source ` +
            'address and a mistyped code spends one - re-read the boot banner ' +
            'before trying again rather than guessing.')
        } else {
          setError(data.detail || 'Could not claim this deployment')
        }
        return
      }
      // Straight into a session: the operator just proved who they are, and
      // every next step needs a token.
      const login = await fetch(`${API}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: name, password }),
      })
      const session = await login.json().catch(() => ({}))
      if (login.ok && session.access_token) {
        localStorage.setItem('az_jwt_token', session.access_token)
        // The refresh token is stored here too. Every other path that
        // establishes a session stores it, and the claim is the ONE session on
        // a brand-new deployment - a session without it dies silently at
        // access-token expiry with nothing to renew from.
        if (session.refresh_token) {
          localStorage.setItem('az_jwt_refresh', session.refresh_token)
        }
        window.location.href = '/'
      } else {
        // The deployment IS claimed at this point, so this is recoverable but
        // the operator must be told which half succeeded.
        setNotice(
          'Owner created, but the automatic sign-in did not complete' +
          (session.detail ? `: ${session.detail}` : '.') +
          ' The deployment is claimed - sign in from the main page.')
      }
    } catch {
      setError('Could not reach the server')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 flex items-center justify-center p-6">
      <div className="w-full max-w-md">
        <h1 className="text-xl font-semibold mb-1">Claim this deployment</h1>
        <p className="text-sm text-gray-500 mb-6">
          Create the Owner account - the highest access tier, and the only
          account that can be created without one already existing.
        </p>

        <form onSubmit={submit} className="space-y-4">
          <div className="rounded-xl border border-amber-600/30 bg-amber-500/5 p-4 text-sm text-amber-200/90">
            <p className="font-medium mb-1">
              Either this deployment has not been claimed yet, or you are not signed in.
            </p>
            <p className="text-amber-200/70 leading-relaxed">
              Whether a deployment is claimed is readable without signing in, on{' '}
              <code className="px-1 py-0.5 rounded bg-black/30 text-amber-100">
                GET /api/auth/needs-setup
              </code>
              . That is safe only because claiming also needs the code below.
              Already claimed it?{' '}
              {/* ?login=1, not "/": the boot handler redirects back here
                  whenever the deployment reports needs_setup, so a bare "/" is
                  a round trip to this same screen. The explicit door survives
                  in every state. */}
              <a className="text-amber-100 underline" href="/?login=1">Sign in from the main page</a>.
              Claiming it fresh needs the code printed in the server logs at
              startup - run{' '}
              <code className="px-1 py-0.5 rounded bg-black/30 text-amber-100">
                docker compose logs backend
              </code>{' '}
              and look for the banner. Only someone who can already read those
              logs has seen it, which is what stops whoever finds this
              deployment first from taking it.
            </p>
          </div>

          <Field label="Claim code">
            <input
              value={claimCode}
              onChange={e => setClaimCode(e.target.value)}
              placeholder="from the server logs"
              autoFocus
              className={inputClass}
            />
          </Field>
          <Field label="Your username">
            <input value={username} onChange={e => setUsername(e.target.value)} className={inputClass} />
          </Field>
          <Field
            label="Your password"
            hint="Policy is enforced server-side; the message names any rule you miss."
          >
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              className={inputClass}
            />
          </Field>

          {error && <p className="text-sm text-red-400">{error}</p>}
          {notice && <p className="text-sm text-amber-300">{notice}</p>}

          <button
            type="submit"
            disabled={busy || !claimCode.trim() || !username.trim() || !password}
            className={primaryButtonClass}
          >
            {busy ? 'Claiming...' : 'Claim this deployment'}
          </button>
        </form>
      </div>
    </div>
  )
}
