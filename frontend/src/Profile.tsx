import { useState } from 'react'

const PRIMARY_COLOR = import.meta.env.VITE_PRIMARY_COLOR || '#2563eb'

interface ProfileProps {
  api: string
  headers: () => Record<string, string>
  user: { id: number; username: string; role: string; department?: string }
  onClose: () => void
  onUsernameChange: (newUsername: string, accessToken: string, refreshToken: string) => void
  onPasswordChange: () => void
}

export default function Profile({ api, headers, user, onClose, onUsernameChange, onPasswordChange }: ProfileProps) {
  const [tab, setTab] = useState<'username' | 'password'>('username')

  const [newUsername, setNewUsername] = useState(user.username)
  const [usernameError, setUsernameError] = useState('')
  const [usernameSuccess, setUsernameSuccess] = useState('')
  const [savingUsername, setSavingUsername] = useState(false)

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [passwordError, setPasswordError] = useState('')
  const [passwordSuccess, setPasswordSuccess] = useState('')
  const [savingPassword, setSavingPassword] = useState(false)

  const saveUsername = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newUsername.trim() || newUsername === user.username) return
    setSavingUsername(true)
    setUsernameError('')
    setUsernameSuccess('')
    try {
      const res = await fetch(`${api}/api/auth/me/username`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ new_username: newUsername.trim() }),
      })
      const data = await res.json()
      if (!res.ok) { setUsernameError(data.detail || 'Failed to update username'); return }
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
      <div className="bg-gray-900 border border-gray-700 rounded-2xl w-full max-w-md mx-4 shadow-2xl">

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
            className="text-gray-500 hover:text-white transition-colors text-lg leading-none"
          >
            ✕
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-800">
          {(['username', 'password'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 py-2.5 text-xs font-medium transition-colors border-b-2 ${
                tab === t ? 'text-white' : 'text-gray-500 hover:text-gray-300 border-transparent'
              }`}
              style={tab === t ? { borderColor: PRIMARY_COLOR } : {}}
            >
              {t === 'username' ? 'Username' : 'Password'}
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
              {usernameError && <p className="text-xs text-red-400">{usernameError}</p>}
              {usernameSuccess && <p className="text-xs text-green-400">{usernameSuccess}</p>}
              <button
                type="submit"
                disabled={savingUsername || !newUsername.trim() || newUsername.trim() === user.username}
                className="w-full py-2.5 rounded-xl text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ backgroundColor: PRIMARY_COLOR }}
              >
                {savingUsername ? 'Saving…' : 'Save username'}
              </button>
            </form>
          ) : (
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
          )}
        </div>
      </div>
    </div>
  )
}
