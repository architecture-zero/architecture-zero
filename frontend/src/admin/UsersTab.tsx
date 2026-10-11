import { useState, useEffect } from 'react'
import { actionError, emitError, guardedJson } from '../errorSurface'
import { PRIMARY_COLOR } from '../theme'
import { useStepUp } from './stepUp'

// ── Tab: Users ─────────────────────────────────────────────────────────────

interface User {
  id: number
  username: string
  role: string
  department: string
  permissions: string[]
  is_active: number
  created_at: string
  mfa_enabled: number
  failed_attempts: number
  locked_until: string | null
}

interface PermissionMeta {
  scopes: string[]
  presets: Record<string, string[]>
}

export function UsersTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [users, setUsers] = useState<User[]>([])
  const [departments, setDepartments] = useState<string[]>(['general'])
  const [permMeta, setPermMeta] = useState<PermissionMeta>({ scopes: [], presets: {} })
  const [expandedUser, setExpandedUser] = useState<number | null>(null)
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState('member')
  const [newDept, setNewDept] = useState('general')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  // The step-up: the operator's own password. Create carries it in the form;
  // role and authority-raising permission writes ask through the prompt.
  const [actorPassword, setActorPassword] = useState('')
  const stepUp = useStepUp()

  const load = () => {
    guardedJson<{ users?: User[] }>(
      fetch(`${api}/api/users`, { headers: headers() }), 'Loading users')
      // Normalized at the boundary. The column defaults to the literal "{}"
      // and the API returns json.loads of it, so a user whose permissions were
      // never explicitly written arrives as an OBJECT, not a list - and
      // `perms?.includes(x)` on an object THROWS inside render, which with no
      // error boundary takes the whole panel to a blank page. The server is
      // deliberately tolerant of the same sentinel (permissions.py checks
      // isinstance(stored, list)); this is the client half of that tolerance.
      .then(d => {
        if (!d) return
        setUsers((d.users || []).map(u => ({
          ...u,
          permissions: Array.isArray(u.permissions) ? u.permissions : [],
        })))
      })
    guardedJson<{ departments?: string[] }>(
      fetch(`${api}/api/ingest/departments`, { headers: headers() }), 'Loading departments')
      .then(d => { if (d) setDepartments(d.departments || ['general']) })
    guardedJson<PermissionMeta>(
      fetch(`${api}/api/admin/permissions`, { headers: headers() }), 'Loading permissions')
      .then(d => { if (d) setPermMeta(d) })
  }

  useEffect(() => { load() }, [])

  const createUser = async () => {
    if (!newUsername.trim() || !newPassword.trim()) return
    if (!actorPassword) { setError('Your own password is required to create an account'); return }
    setCreating(true)
    setError('')
    try {
      const res = await fetch(`${api}/api/users`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ username: newUsername, password: newPassword, role: newRole, department: newDept,
                               current_password: actorPassword }),
      })
      if (!res.ok) {
        const d = await res.json()
        setError(d.detail || 'Failed to create user')
        return
      }
      setNewUsername('')
      setNewPassword('')
      setNewRole('member')
      setNewDept('general')
      setActorPassword('')
      load()
    } catch {
      setError('Request failed')
    } finally {
      setCreating(false)
    }
  }

  // Every mutation below goes through actionError. They were bare
  // fire-and-reload: the status was never read, so a refused change repainted
  // the previous value and said nothing - the operator sees the row snap back
  // and has no way to tell a permission problem from a bug. These are role and
  // permission writes on a user-management screen; a silent refusal is the one
  // outcome they must not have.
  const mutate = async (p: Promise<Response>, what: string) => {
    const err = await actionError(p, what)
    // The refusal text was computed and then dropped, so the "said out loud"
    // promise above was not kept - a step-up 400 would have been exactly the
    // silent snap-back it describes. Toast it (2026-09-09).
    if (err) emitError(err)
    load()
    return !err
  }

  const jsonPatch = (path: string, body: unknown) =>
    fetch(`${api}${path}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...headers() },
      body: JSON.stringify(body),
    })

  const deactivate = (id: number) =>
    mutate(fetch(`${api}/api/users/${id}`, { method: 'DELETE', headers: headers() }),
           'Deactivating user')

  const unlockUser = (id: number) =>
    mutate(fetch(`${api}/api/admin/users/${id}/unlock`, { method: 'POST', headers: headers() }),
           'Unlocking account')

  const resetMFA = async (id: number) => {
    if (!confirm('Disable MFA for this user? They will need to re-enroll.')) return
    const u = users.find(x => x.id === id)
    // Step-up (2026-09-21): stripping a second factor costs the caller's own password.
    const pw = await stepUp.ask(`reset MFA for ${u?.username ?? 'this user'}`)
    if (pw === null) return
    await mutate(fetch(`${api}/api/admin/users/${id}/mfa-reset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers() },
      body: JSON.stringify({ current_password: pw }),
    }), 'Resetting MFA')
  }

  const changeRole = async (id: number, role: string) => {
    const u = users.find(x => x.id === id)
    const pw = await stepUp.ask(`change ${u?.username ?? 'this user'}'s role to ${role}`)
    if (pw === null) { load(); return false }
    return mutate(jsonPatch(`/api/users/${id}/role`, { role, current_password: pw }), 'Changing role')
  }

  const changeDept = (id: number, department: string) =>
    mutate(jsonPatch(`/api/users/${id}/department`, { department }), 'Changing department')

  // The step-up is owed only when the write ADDS manage_users or
  // manage_system that the user does not hold today (the server's
  // permissions.raises_authority). A reset to the role preset can add them
  // too, so it is checked the same way, against effectivePerms below.
  const STEP_UP_SCOPES = ['manage_users', 'manage_system']
  const raisesAuthority = (u: User, perms: string[] | null) => {
    const before = effectivePerms(u)
    const after = perms && perms.length ? perms : (permMeta?.presets?.[u.role] || [])
    return after.some(s => STEP_UP_SCOPES.includes(s) && !before.includes(s))
  }

  const setPermissions = async (u: User, perms: string[] | null) => {
    let body: Record<string, unknown> = { permissions: perms }
    if (raisesAuthority(u, perms)) {
      const pw = await stepUp.ask(`grant ${u.username} manage_users or manage_system`)
      if (pw === null) { load(); return false }
      body = { ...body, current_password: pw }
    }
    return mutate(jsonPatch(`/api/users/${u.id}/permissions`, body), 'Updating permissions')
  }

  // What the user ACTUALLY has right now - the client-side mirror of the
  // server's effective_permissions (permissions.py): a non-empty stored list is
  // an explicit override, otherwise the role preset applies.
  //
  // THE BUG THIS EXISTS TO PREVENT: the pills read the STORED list, which is
  // empty for everyone who has never been given an override - i.e. every user
  // on a fresh deployment. So a member with the full member preset rendered
  // with every pill dark, and the first click sent a ONE-ELEMENT list, silently
  // revoking every other permission their role grants. The screen looked like
  // "grant this one thing" and behaved like "replace everything with this one
  // thing", and it was unreachable until the round-1 blocker fix, so it was
  // being used for the first time.
  const effectivePerms = (u: User): string[] => {
    // AN OWNER HOLDS EVERYTHING, and not because of a stored list. The server
    // short-circuits before effective_permissions is ever consulted
    // (require_permission returns early on is_owner, require_owner tests it
    // directly), so mirroring only the stored-list logic made this screen
    // authoritative-looking and wrong in the PERMISSIVE direction for exactly
    // the account that matters most: it would show an Owner missing pills they
    // in fact have, and a click to "revoke" one would report success while
    // changing nothing the server enforces.
    if (u.role === 'owner') return permMeta?.scopes || []
    const stored = u.permissions
    if (stored && stored.length) return stored
    return permMeta?.presets?.[u.role] || []
  }

  const togglePerm = (u: User, scope: string) => {
    const current = effectivePerms(u)
    const next = current.includes(scope) ? current.filter(p => p !== scope) : [...current, scope]
    setPermissions(u, next)
  }

  return (
    <div className="space-y-6">
      {/* Add user form */}
      <div className="bg-gray-800/50 border border-gray-700 rounded-xl p-4 space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Add User</p>
        <div className="flex gap-2">
          {/* autoComplete off/new-password: browsers were autofilling the
              operator's OWN saved login into this create-user form - one stray
              Add click away from a duplicate account carrying their password. */}
          <input
            placeholder="Username"
            autoComplete="off"
            value={newUsername}
            onChange={e => setNewUsername(e.target.value)}
            className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500/60"
          />
          <input
            placeholder="Password"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={e => setNewPassword(e.target.value)}
            className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500/60"
          />
        </div>
        <div className="flex gap-2">
          {/* The step-up: creating an account costs the operator's OWN password. */}
          <input
            placeholder="Your password (required to create an account)"
            type="password"
            autoComplete="current-password"
            value={actorPassword}
            onChange={e => setActorPassword(e.target.value)}
            className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500/60"
          />
        </div>
        <div className="flex gap-2">
          <select
            value={newRole}
            onChange={e => setNewRole(e.target.value)}
            className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none"
          >
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
          <input
            placeholder="Department"
            value={newDept}
            onChange={e => setNewDept(e.target.value)}
            list="dept-list"
            className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500/60"
          />
          <datalist id="dept-list">
            {departments.map(d => <option key={d} value={d} />)}
          </datalist>
          <button
            onClick={createUser}
            disabled={creating}
            className="px-4 py-2 rounded-lg text-sm font-medium disabled:opacity-50"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            Add
          </button>
        </div>
        {error && <p className="text-xs text-red-400">{error}</p>}
      </div>

      {stepUp.prompt}
      {/* User list */}
      <div className="space-y-2">
        <p className="text-xs text-gray-500 uppercase tracking-widest">Users ({users.length})</p>
        {users.map(u => (
          <div key={u.id} className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
            {/* Row */}
            <div className="flex items-center justify-between px-4 py-3">
              <div>
                <p className="text-sm text-white font-medium flex items-center gap-1.5">
                  {u.username}
                  {u.mfa_enabled ? (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-green-500/15 text-green-400 border border-green-500/30">MFA</span>
                  ) : null}
                  {u.locked_until ? (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/15 text-red-400 border border-red-500/30">Locked</span>
                  ) : null}
                </p>
                <p className="text-xs text-gray-500">{u.created_at.slice(0, 10)} · {u.department}</p>
              </div>
              <div className="flex items-center gap-2">
                <input
                  value={u.department}
                  onChange={e => changeDept(u.id, e.target.value)}
                  onBlur={e => changeDept(u.id, e.target.value)}
                  list="dept-list"
                  className="bg-gray-900 border border-gray-700 rounded-lg px-2 py-1 text-xs text-white outline-none w-24"
                  title="Department"
                />
                <select
                  value={u.role}
                  onChange={e => changeRole(u.id, e.target.value)}
                  className="bg-gray-900 border border-gray-700 rounded-lg px-2 py-1 text-xs text-white outline-none"
                >
                  <option value="member">member</option>
                  <option value="admin">admin</option>
                  <option value="owner">owner</option>
                </select>
                <button
                  onClick={() => setExpandedUser(expandedUser === u.id ? null : u.id)}
                  className="text-xs text-gray-500 hover:text-gray-300 transition-colors px-1"
                  title="Edit permissions"
                >
                  {expandedUser === u.id ? '▲' : '▼'} perms
                </button>
                {u.locked_until && (
                  <button
                    onClick={() => unlockUser(u.id)}
                    className="text-xs text-yellow-500 hover:text-yellow-400 transition-colors"
                  >
                    Unlock
                  </button>
                )}
                {u.mfa_enabled ? (
                  <button
                    onClick={() => resetMFA(u.id)}
                    className="text-xs text-orange-500 hover:text-orange-400 transition-colors"
                  >
                    Reset MFA
                  </button>
                ) : null}
                <button
                  onClick={() => deactivate(u.id)}
                  className="text-xs text-red-500 hover:text-red-400 transition-colors"
                >
                  Deactivate
                </button>
              </div>
            </div>

            {/* Permissions panel */}
            {expandedUser === u.id && (
              <div className="border-t border-gray-700/50 px-4 py-3 bg-gray-900/40">
                <div className="flex items-center justify-between mb-2">
                  <p className="text-xs text-gray-400 uppercase tracking-widest">Permissions</p>
                  <button
                    onClick={() => setPermissions(u, null)}
                    className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
                    title="Reset to role defaults"
                  >
                    Reset to {u.role} defaults
                  </button>
                </div>
                <div className="flex flex-wrap gap-2">
                  {permMeta.scopes.map(scope => {
                    const active = effectivePerms(u).includes(scope)
                    return (
                      <button
                        key={scope}
                        onClick={() => togglePerm(u, scope)}
                        className={`text-xs px-2.5 py-1 rounded-full border transition-colors font-mono ${
                          active
                            ? 'border-blue-500/60 text-blue-300 bg-blue-500/10'
                            : 'border-gray-700 text-gray-500 hover:text-gray-300'
                        }`}
                      >
                        {scope}
                      </button>
                    )
                  })}
                </div>
                <p className="text-xs text-gray-600 mt-2">
                  Preset ({u.role}): {(permMeta.presets[u.role] || []).join(', ') || '-'}
                </p>
                <p className="text-xs text-gray-600 mt-1">
                  {u.role === 'owner'
                    ? 'Owner - holds every permission by role, and the server checks that before any stored list. Editing these pills will not restrict an Owner.'
                    : u.permissions && u.permissions.length
                      ? 'Using a per-user override.'
                      : `Following ${u.role} defaults - changing a pill here creates an override for this user.`}
                </p>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
