import { useEffect, useRef, useState } from 'react'
import { emitAuthExpired } from './errorSurface'
import { PRIMARY_COLOR } from './theme'

interface ProfileProps {
  api: string
  headers: () => Record<string, string>
  // mfa_enabled is what GET /api/auth/me reports. A sign-in answer does not
  // carry it, so it can be absent, and absent means unknown, not off.
  user: { id: number; username: string; role: string; department?: string; mfa_enabled?: boolean }
  onClose: () => void
  onUsernameChange: (newUsername: string, accessToken: string, refreshToken: string) => void
  onPasswordChange: () => void
  // Two-factor went on, or went off for a replacement that is not confirmed
  // yet. The shell's copy of the account follows, with no reload.
  onMfaChange: (enabled: boolean) => void
}

export default function Profile({ api, headers, user, onClose, onUsernameChange, onPasswordChange, onMfaChange }: ProfileProps) {
  const [tab, setTab] = useState<'username' | 'password' | 'mfa'>('username')

  const [newUsername, setNewUsername] = useState(user.username)
  const [usernameError, setUsernameError] = useState('')
  const [usernameSuccess, setUsernameSuccess] = useState('')
  const [savingUsername, setSavingUsername] = useState(false)
  // Step-up: a username change re-issues the token pair, so the server asks
  // for the caller's own password (a bearer alone proves the browser, not
  // the person). Kept separate from the password tab's field on purpose.
  const [usernamePassword, setUsernamePassword] = useState('')

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [passwordError, setPasswordError] = useState('')
  const [passwordSuccess, setPasswordSuccess] = useState('')
  const [savingPassword, setSavingPassword] = useState(false)

  const saveUsername = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newUsername.trim() || newUsername === user.username || !usernamePassword) return
    setSavingUsername(true)
    setUsernameError('')
    setUsernameSuccess('')
    try {
      const res = await fetch(`${api}/api/auth/me/username`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ new_username: newUsername.trim(), current_password: usernamePassword }),
      })
      const data = await res.json()
      if (!res.ok) { setUsernameError(data.detail || 'Failed to update username'); return }
      setUsernamePassword('')
      setUsernameSuccess('Username updated')
      onUsernameChange(newUsername.trim(), data.access_token, data.refresh_token)
    } catch {
      setUsernameError('Could not reach the server')
    } finally {
      setSavingUsername(false)
    }
  }

  const savePassword = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!currentPassword || !newPassword || !confirmPassword) return
    if (newPassword !== confirmPassword) { setPasswordError('New passwords do not match'); return }
    setSavingPassword(true)
    setPasswordError('')
    setPasswordSuccess('')
    try {
      const res = await fetch(`${api}/api/auth/me/password`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      })
      const data = await res.json()
      if (!res.ok) { setPasswordError(data.detail || 'Failed to update password'); return }
      setPasswordSuccess('Password updated - you will be signed out')
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      setTimeout(() => onPasswordChange(), 1500)
    } catch {
      setPasswordError('Could not reach the server')
    } finally {
      setSavingPassword(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      {/* No taller than the window, scrolling inside when it would be: the
          Two-factor tab with a QR code on it is longer than a phone-sized
          window, and without this its ends (Close, the last button) sat
          outside it with no way to reach them. */}
      <div className="bg-gray-900 border border-gray-700 rounded-2xl w-full max-w-md mx-4 shadow-2xl max-h-[calc(100%-2rem)] overflow-y-auto">

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-800">
          <div>
            <h2 className="text-sm font-semibold text-white">Account Settings</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {user.username} · <span className="capitalize">{user.role}</span>
              {user.department && user.department !== 'general' ? ` · ${user.department}` : ''}
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="text-gray-500 hover:text-white transition-colors text-lg leading-none"
          >
            ✕
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-800">
          {(['username', 'password', 'mfa'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 py-2.5 text-xs font-medium transition-colors border-b-2 ${
                tab === t ? 'text-white' : 'text-gray-500 hover:text-gray-300 border-transparent'
              }`}
              style={tab === t ? { borderColor: PRIMARY_COLOR } : {}}
            >
              {t === 'username' ? 'Username' : t === 'password' ? 'Password' : 'Two-factor'}
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="px-6 py-5">
          {tab === 'username' ? (
            <form onSubmit={saveUsername} className="space-y-4">
              <div>
                <label className="text-xs text-gray-400 block mb-1.5">New username</label>
                <input
                  type="text"
                  value={newUsername}
                  onChange={e => setNewUsername(e.target.value)}
                  autoFocus
                  className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-2.5 text-sm text-white outline-none transition-colors"
                />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1.5">Current password</label>
                <input
                  type="password"
                  value={usernamePassword}
                  onChange={e => setUsernamePassword(e.target.value)}
                  autoComplete="current-password"
                  className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-2.5 text-sm text-white outline-none transition-colors"
                />
              </div>
              {usernameError && <p className="text-xs text-red-400">{usernameError}</p>}
              {usernameSuccess && <p className="text-xs text-green-400">{usernameSuccess}</p>}
              <button
                type="submit"
                disabled={savingUsername || !newUsername.trim() || newUsername.trim() === user.username || !usernamePassword}
                className="w-full py-2.5 rounded-xl text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ backgroundColor: PRIMARY_COLOR }}
              >
                {savingUsername ? 'Saving…' : 'Save username'}
              </button>
            </form>
          ) : tab === 'password' ? (
            <form onSubmit={savePassword} className="space-y-3">
              <div>
                <label className="text-xs text-gray-400 block mb-1.5">Current password</label>
                <input
                  type="password"
                  value={currentPassword}
                  onChange={e => setCurrentPassword(e.target.value)}
                  autoFocus
                  className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-2.5 text-sm text-white outline-none transition-colors"
                />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1.5">New password</label>
                <input
                  type="password"
                  value={newPassword}
                  onChange={e => setNewPassword(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-2.5 text-sm text-white outline-none transition-colors"
                />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1.5">Confirm new password</label>
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-2.5 text-sm text-white outline-none transition-colors"
                />
              </div>
              {passwordError && <p className="text-xs text-red-400">{passwordError}</p>}
              {passwordSuccess && <p className="text-xs text-green-400">{passwordSuccess}</p>}
              <button
                type="submit"
                disabled={savingPassword || !currentPassword || !newPassword || !confirmPassword}
                className="w-full py-2.5 rounded-xl text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ backgroundColor: PRIMARY_COLOR }}
              >
                {savingPassword ? 'Saving…' : 'Change password'}
              </button>
            </form>
          ) : null}
          {/* Mounted for as long as this screen is, drawing only while its
              tab is the one showing: a setup in progress survives a look at
              another tab, and closing the screen takes it away. */}
          <TwoFactorTab
            active={tab === 'mfa'}
            api={api}
            headers={headers}
            known={user.mfa_enabled}
            onMfaChange={onMfaChange}
          />
        </div>
      </div>
    </div>
  )
}

// -- Two-factor ---------------------------------------------------------------
// The account's own door to an authenticator app: turn two-factor on, or
// replace the authenticator it is on with. The server's two routes
// (/api/auth/mfa/setup and /api/auth/mfa/enable) are older than this tab;
// before it, enrolling meant calling them by hand. Every rule is the server's
// and the tab shows what the server says: the password comes first on both
// paths, a replacement also takes a current code from the authenticator being
// replaced, and a setup has to be confirmed before it expires. There is no
// "turn off" here. An administrator resets two-factor.
//
// The key and the QR code are an authentication seed. They are held in this
// component's state and nowhere else: not in storage, not in the console, not
// in an address. A confirmed setup clears them, and so does starting again.
// Closing the account screen unmounts this component and they go with it. An
// answer that lands after that is dropped whole: the account it was for may
// no longer be the one signed in.

type TwoFactorStatus = 'checking' | 'on' | 'off' | 'unknown'

// The server's two answers that mean the setup this tab is holding is gone: it
// waited too long, or the stored key was reset meanwhile. Both arrive as a 400,
// like a mistyped code, so the words are what tells them apart. Should the
// words change, "Start again" is still on the page under the code.
const SETUP_GONE = /expired|setup first/i

// A body as JSON, or null when it is not JSON: a proxy's HTML error page, an
// empty answer.
async function jsonBody(res: Response): Promise<Record<string, unknown> | null> {
  try {
    const body: unknown = await res.json()
    return body !== null && typeof body === 'object' ? (body as Record<string, unknown>) : null
  } catch {
    return null
  }
}

// What a refused request said: the server's own words (`detail`) when the body
// carries them as text, else the status. A validation error's detail is a
// list, and a list rendered as it comes takes the page down.
async function refusalWords(res: Response): Promise<string> {
  if (res.status === 401) emitAuthExpired()
  const detail = (await jsonBody(res))?.detail
  return typeof detail === 'string' && detail ? detail : `The server refused this (HTTP ${res.status})`
}

const sixDigits = (typed: string) => typed.replace(/\D/g, '').slice(0, 6)

// The QR code, drawn from the PNG the server made. The Content-Security-Policy
// this app is served under allows images from its own origin only, and an
// <img> pointed at a data: address is an image load that policy refuses.
// Decoding the same bytes and drawing them on a canvas loads nothing, so the
// code shows under that policy as it stands. A browser that cannot do this
// says so, and the key under it is always there to type.
function QrCode({ png }: { png: string }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let current = true
    setFailed(false)
    const draw = async () => {
      const prefix = 'data:image/png;base64,'
      if (!png.startsWith(prefix)) throw new Error('not a PNG')
      const bytes = Uint8Array.from(atob(png.slice(prefix.length)), c => c.charCodeAt(0))
      const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }))
      try {
        if (!current) return
        const ctx = canvas.current?.getContext('2d')
        if (!canvas.current || !ctx) throw new Error('no canvas')
        canvas.current.width = bitmap.width
        canvas.current.height = bitmap.height
        ctx.drawImage(bitmap, 0, 0)
      } finally {
        bitmap.close()
      }
    }
    draw().catch(() => { if (current) setFailed(true) })
    return () => { current = false }
  }, [png])
  return (
    <>
      <canvas
        ref={canvas}
        role="img"
        aria-label="QR code for your authenticator app"
        hidden={failed}
        className="w-48 h-48 rounded-lg bg-white"
      />
      {failed && (
        <p className="text-xs text-amber-300 text-center">
          This browser could not draw the QR code. Type the key below into your authenticator app.
        </p>
      )}
    </>
  )
}

function TwoFactorTab({ active, api, headers, known, onMfaChange }: {
  active: boolean
  api: string
  headers: () => Record<string, string>
  // What the shell last knew about this account, or undefined when it never
  // learned it.
  known: boolean | undefined
  onMfaChange: (enabled: boolean) => void
}) {
  const [status, setStatus] = useState<TwoFactorStatus>('checking')
  // On, and the stored key cannot be read on this server: the next sign-in
  // by this account is refused. GET /api/auth/me reports it.
  const [unreadable, setUnreadable] = useState(false)
  const [password, setPassword] = useState('')
  const [oldCode, setOldCode] = useState('')
  const [replacing, setReplacing] = useState(false)
  const [pending, setPending] = useState<{ secret: string; qr: string } | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [turnedOn, setTurnedOn] = useState(false)
  const [gone, setGone] = useState(false)
  const [busy, setBusy] = useState(false)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  // Whether two-factor is on comes from GET /api/auth/me, read when the tab
  // opens: the shell's copy of the account is as old as the last sign-in, and
  // a sign-in answer does not carry the field at all. When the read fails,
  // what the shell last knew stands in. With nothing known the tab says so
  // and offers no setup it cannot place.
  const readStatus = async () => {
    setStatus('checking')
    let enabled: boolean | undefined
    let stranded = false
    try {
      const res = await fetch(`${api}/api/auth/me`, { headers: headers() })
      if (res.status === 401) emitAuthExpired()
      const me = res.ok ? await jsonBody(res) : null
      if (me && typeof me.mfa_enabled === 'boolean') {
        enabled = me.mfa_enabled
        stranded = enabled && me.mfa_secret_unreadable === true
      }
    } catch { /* unreachable: what the shell knew stands in below */ }
    if (!alive.current) return
    if (enabled !== undefined && enabled !== known) onMfaChange(enabled)
    if (enabled === undefined) enabled = known
    setUnreadable(stranded)
    // Nothing to replace on an account that is not known to be on.
    if (enabled !== true) setReplacing(false)
    setStatus(enabled === undefined ? 'unknown' : enabled ? 'on' : 'off')
  }

  // Read on the way in, unless a setup is on the page: that state is this
  // tab's own, and the account reads "off" for as long as it is pending. What
  // an earlier visit to the tab was told does not carry over.
  useEffect(() => {
    if (!active || pending) return
    setError('')
    setTurnedOn(false)
    readStatus()
  }, [active])

  // Step 1 on both paths. Off: the password. On: the password and a current
  // code from the authenticator being replaced, with rekey said out loud.
  const start = async (e: React.FormEvent) => {
    e.preventDefault()
    const rekey = status === 'on'
    if (busy || !password || (rekey && oldCode.length !== 6)) return
    setBusy(true)
    setError('')
    setTurnedOn(false)
    try {
      const res = await fetch(`${api}/api/auth/mfa/setup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(rekey
          ? { current_password: password, rekey: true, mfa_code: oldCode }
          : { current_password: password, rekey: false }),
      })
      if (!res.ok) {
        const words = await refusalWords(res)
        if (!alive.current) return
        setError(words)
        // 409: two-factor is already on, so this tab's picture of the account
        // was stale. Read it again; the server's words stay on the page.
        if (res.status === 409) readStatus()
        return
      }
      const body = await jsonBody(res)
      if (!alive.current) return
      const secret = body?.secret
      const qr = body?.qr
      if (typeof secret !== 'string' || !secret || typeof qr !== 'string') {
        // A 2xx that is not this API's answer proves nothing either way. Say
        // so, and ask the server what the account is now.
        setError('The server answered, but not with a setup this screen can read. Start again.')
        readStatus()
        return
      }
      setPending({ secret, qr })
      setPassword('')
      setOldCode('')
      setCode('')
      setGone(false)
      // Storing a new key turns two-factor off on the server, and it stays off
      // until a code from the new authenticator is confirmed below.
      setStatus('off')
      if (rekey) onMfaChange(false)
    } catch {
      if (alive.current) setError('Could not reach the server')
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  // Step 3: the first code from the new authenticator turns two-factor on.
  const confirm = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || code.length !== 6) return
    setBusy(true)
    setError('')
    try {
      const res = await fetch(`${api}/api/auth/mfa/enable`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ code }),
      })
      if (!res.ok) {
        const words = await refusalWords(res)
        if (!alive.current) return
        setError(words)
        setCode('')
        if (res.status === 409) {
          // Already on, so there is no setup left to confirm: the key on the
          // page is dead. Drop it and read the account again.
          setPending(null)
          setReplacing(false)
          readStatus()
        } else if (res.status === 400 && SETUP_GONE.test(words)) {
          setPending(null)
          setGone(true)
        }
        return
      }
      const body = await jsonBody(res)
      if (!alive.current) return
      setPending(null)
      setCode('')
      setReplacing(false)
      setGone(false)
      if (!body) {
        // The same here: no "on" is claimed on an answer that was not read.
        setError('The server answered, but not in a form this screen can read.')
        readStatus()
        return
      }
      setUnreadable(false)
      setStatus('on')
      setTurnedOn(true)
      onMfaChange(true)
    } catch {
      if (alive.current) setError('Could not reach the server')
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  const startAgain = () => {
    setPending(null)
    setCode('')
    setError('')
    setGone(false)
    setReplacing(false)
  }

  if (!active) return null

  const field = 'w-full bg-gray-800 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-2.5 text-sm text-white outline-none transition-colors'
  const label = 'text-xs text-gray-400 block mb-1.5'
  const primary = 'w-full py-2.5 rounded-xl text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'
  const quiet = 'w-full py-1 text-xs text-gray-500 hover:text-gray-300 transition-colors'
  const refused = error ? <p role="alert" className="text-xs text-red-400">{error}</p> : null

  if (status === 'checking') {
    return <p className="text-xs text-gray-400">Checking two-factor for this account...</p>
  }

  if (status === 'unknown') {
    return (
      <div className="space-y-3">
        <p className="text-sm font-medium text-white">Two-factor could not be read for this account.</p>
        <p className="text-xs text-gray-400">
          The server did not say whether it is on, so there is nothing to set up or replace here yet.
        </p>
        <button type="button" onClick={readStatus} className={primary} style={{ backgroundColor: PRIMARY_COLOR }}>
          Try again
        </button>
      </div>
    )
  }

  if (status === 'on') {
    return (
      <div className="space-y-3">
        <p className="text-sm font-medium text-green-400">Two-factor is ON for this account.</p>
        {turnedOn && <p className="text-xs text-green-400">Turned on just now.</p>}
        {unreadable ? (
          <p className="text-xs text-red-400">
            Its stored key cannot be read on this server, so the next sign-in by this account will be
            refused. An administrator can reset it (Admin, Users, Reset MFA). Then set it up again here.
          </p>
        ) : replacing ? (
          <form onSubmit={start} className="space-y-3">
            <p className="text-xs text-amber-300">
              Replacing turns two-factor off for this account until a code from the new authenticator
              is confirmed, and the authenticator you have now stops working as soon as you start.
              Stay signed in until it is confirmed: where two-factor is required to sign in, an
              account without it cannot sign back in.
            </p>
            <div>
              <label htmlFor="mfa-password" className={label}>Your password</label>
              <input
                id="mfa-password"
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoComplete="current-password"
                autoFocus
                className={field}
              />
            </div>
            <div>
              <label htmlFor="mfa-old-code" className={label}>A code from the authenticator you are replacing</label>
              <input
                id="mfa-old-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={oldCode}
                onChange={e => setOldCode(sixDigits(e.target.value))}
                className={`${field} text-center tracking-widest`}
              />
            </div>
            {refused}
            <button
              type="submit"
              disabled={busy || !password || oldCode.length !== 6}
              className={primary}
              style={{ backgroundColor: PRIMARY_COLOR }}
            >
              {busy ? 'Starting...' : 'Start replacement'}
            </button>
            <button
              type="button"
              onClick={() => { setReplacing(false); setPassword(''); setOldCode(''); setError('') }}
              className={quiet}
            >
              Cancel
            </button>
          </form>
        ) : (
          <>
            <p className="text-xs text-gray-400">
              Signing in asks for a 6-digit code from your authenticator app after your password.
            </p>
            {refused}
            <button
              type="button"
              onClick={() => { setReplacing(true); setError(''); setTurnedOn(false) }}
              className="w-full py-2.5 rounded-xl text-sm font-medium border border-gray-700 text-gray-200 hover:bg-gray-800 transition-colors"
            >
              Replace authenticator
            </button>
            <p className="text-xs text-gray-500">
              Lost your authenticator? An administrator can reset two-factor for your account
              (Admin, Users, Reset MFA). Then set it up again here.
            </p>
          </>
        )}
      </div>
    )
  }

  // Off: three numbered steps, the later ones named before they can be done.
  return (
    <div className="space-y-4">
      <p className="text-sm font-medium text-white">Two-factor is OFF for this account.</p>
      {replacing && pending && (
        <p className="text-xs text-amber-300">
          It stays off until a code from the new authenticator is confirmed below. Stay signed in until then.
        </p>
      )}
      {pending ? (
        <p className="text-xs text-gray-400">1. Your password: <span className="text-green-400">confirmed</span></p>
      ) : (
        <form onSubmit={start} className="space-y-3">
          <div>
            <label htmlFor="mfa-password" className={label}>1. Your password</label>
            <input
              id="mfa-password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
              className={field}
            />
          </div>
          {refused}
          <button
            type="submit"
            disabled={busy || !password}
            className={primary}
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            {busy ? 'Starting...' : gone ? 'Start again' : 'Start setup'}
          </button>
        </form>
      )}
      <div className="space-y-2">
        <p className={`text-xs ${pending ? 'text-gray-400' : 'text-gray-600'}`}>2. Scan with your authenticator app</p>
        {pending && (
          <div className="flex flex-col items-center gap-2">
            <QrCode png={pending.qr} />
            <p className="text-xs text-gray-400">or type this key:</p>
            {/* Shown in fours and held as one run of text, so a copy is the key
                itself with no spaces in it. */}
            <p data-testid="two-factor-key" className="font-mono text-sm text-white text-center select-all">
              {(pending.secret.match(/.{1,4}/g) || []).map((four, i) => (
                <span key={i} className="inline-block mx-1">{four}</span>
              ))}
            </p>
          </div>
        )}
      </div>
      {pending ? (
        <form onSubmit={confirm} className="space-y-3">
          <div>
            <label htmlFor="mfa-code" className={label}>3. The 6-digit code</label>
            <input
              id="mfa-code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              onChange={e => setCode(sixDigits(e.target.value))}
              autoFocus
              className={`${field} text-center tracking-widest`}
            />
          </div>
          {refused}
          <button
            type="submit"
            disabled={busy || code.length !== 6}
            className={primary}
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            {busy ? 'Turning on...' : 'Turn on'}
          </button>
          <p className="text-xs text-gray-500">
            A setup that is not confirmed in time expires, and then it is started again.
          </p>
          <button type="button" onClick={startAgain} className={quiet}>Start again</button>
        </form>
      ) : (
        <p className="text-xs text-gray-600">3. The 6-digit code</p>
      )}
    </div>
  )
}
