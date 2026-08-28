import { useState } from 'react'

const PRIMARY_COLOR = import.meta.env.VITE_PRIMARY_COLOR || '#2563eb'
const INSTANCE_NAME = import.meta.env.VITE_INSTANCE_NAME || 'Architecture Zero'
const INITIALS = INSTANCE_NAME.split(' ').map((w: string) => w[0]).join('').slice(0, 2).toUpperCase()

interface LoginProps {
  api: string
  onLogin: (token: string, refreshToken: string, user: { id: number; username: string; role: string; permissions: string[] }) => void
  onGuest?: () => void
}

export default function Login({ api, onLogin, onGuest }: LoginProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // MFA step
  const [mfaToken, setMfaToken] = useState('')
  const [mfaCode, setMfaCode] = useState('')

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password.trim()) return
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`${api}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const data = await res.json()
      if (!res.ok) {
        setError(data.detail || 'Login failed')
        return
      }
      if (data.mfa_required) {
        setMfaToken(data.mfa_token)
        return
      }
      onLogin(data.access_token, data.refresh_token, data.user)
    } catch {
      setError('Could not reach the server')
    } finally {
      setLoading(false)
    }
  }

  const submitMfa = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!mfaCode.trim()) return
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`${api}/api/auth/mfa/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mfa_token: mfaToken, code: mfaCode }),
      })
      const data = await res.json()
      if (!res.ok) {
        setError(data.detail || 'Invalid code')
        return
      }
      onLogin(data.access_token, data.refresh_token, data.user)
    } catch {
      setError('Could not reach the server')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex h-screen bg-gray-950 text-white items-center justify-center">
      <div className="w-full max-w-sm px-6">
        <div className="flex flex-col items-center mb-8">
          <div
            className="w-14 h-14 rounded-2xl flex items-center justify-center text-lg font-bold mb-4 shadow-lg"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            {INITIALS}
          </div>
          <h1 className="text-xl font-semibold">{INSTANCE_NAME}</h1>
          <p className="text-sm text-gray-500 mt-1">
            {mfaToken ? 'Enter authenticator code' : 'Sign in to continue'}
          </p>
        </div>

        {mfaToken ? (
          <form onSubmit={submitMfa} className="space-y-3">
            <input
              type="text"
              inputMode="numeric"
              placeholder="6-digit code"
              value={mfaCode}
              onChange={e => setMfaCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              autoFocus
              maxLength={6}
              className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 outline-none transition-colors text-center tracking-widest text-lg"
            />
            {error && <p className="text-xs text-red-400 px-1">{error}</p>}
            <button
              type="submit"
              disabled={loading || mfaCode.length !== 6}
              className="w-full py-3 rounded-xl text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ backgroundColor: PRIMARY_COLOR }}
            >
              {loading ? 'Verifying…' : 'Verify'}
            </button>
            <button
              type="button"
              onClick={() => { setMfaToken(''); setMfaCode(''); setError('') }}
              className="w-full py-2 text-xs text-gray-500 hover:text-gray-300 transition-colors"
            >
              Back to sign in
            </button>
          </form>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <div>
              <input
                type="text"
                placeholder="Username"
                value={username}
                onChange={e => setUsername(e.target.value)}
                autoFocus
                className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 outline-none transition-colors"
              />
            </div>
            <div>
              <input
                type="password"
                placeholder="Password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 outline-none transition-colors"
              />
            </div>

            {error && (
              <p className="text-xs text-red-400 px-1">{error}</p>
            )}

            <button
              type="submit"
              disabled={loading || !username.trim() || !password.trim()}
              className="w-full py-3 rounded-xl text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ backgroundColor: PRIMARY_COLOR }}
            >
              {loading ? 'Signing in…' : 'Sign in'}
            </button>
            {onGuest && (
              <button
                type="button"
                onClick={onGuest}
                className="w-full py-2 text-xs text-gray-500 hover:text-gray-300 transition-colors"
              >
                Continue as guest
              </button>
            )}
          </form>
        )}
      </div>
    </div>
  )
}
