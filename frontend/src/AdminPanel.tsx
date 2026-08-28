import { useState, useEffect } from 'react'
import { actionError, emitError, guardedJson, guardedPoll } from './errorSurface'

const PRIMARY_COLOR = import.meta.env.VITE_PRIMARY_COLOR || '#2563eb'

// ── Types ──────────────────────────────────────────────────────────────────

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

interface KBSource {
  source: string
  count: number
  department: string
}

interface AdminConfig {
  system_prompt?: string
  instance_name?: string
  primary_color?: string
}

interface Analytics {
  total_sessions: number
  total_requests: number
  requests_today: number
  top_model: string | null
  feedback: { total: number; thumbs_up: number; thumbs_down: number }
}

// ── Props ──────────────────────────────────────────────────────────────────

interface AdminPanelProps {
  api: string
  headers: () => Record<string, string>
  currentUser: { id: number; username: string; role: string }
  onClose: () => void
  onLogout: () => void
}

// ── Tab: Users ─────────────────────────────────────────────────────────────

function UsersTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [users, setUsers] = useState<User[]>([])
  const [departments, setDepartments] = useState<string[]>(['general'])
  const [permMeta, setPermMeta] = useState<PermissionMeta>({ scopes: [], presets: {} })
  const [expandedUser, setExpandedUser] = useState<number | null>(null)
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState('user')
  const [newDept, setNewDept] = useState('general')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  const load = () => {
    guardedJson<{ users?: User[] }>(
      fetch(`${api}/api/users`, { headers: headers() }), 'Loading users')
      .then(d => { if (d) setUsers(d.users || []) })
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
    setCreating(true)
    setError('')
    try {
      const res = await fetch(`${api}/api/users`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ username: newUsername, password: newPassword, role: newRole, department: newDept }),
      })
      if (!res.ok) {
        const d = await res.json()
        setError(d.detail || 'Failed to create user')
        return
      }
      setNewUsername('')
      setNewPassword('')
      setNewRole('user')
      setNewDept('general')
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
    await mutate(fetch(`${api}/api/admin/users/${id}/mfa-reset`, { method: 'POST', headers: headers() }),
                 'Resetting MFA')
  }

  const changeRole = (id: number, role: string) =>
    mutate(jsonPatch(`/api/users/${id}/role`, { role }), 'Changing role')

  const changeDept = (id: number, department: string) =>
    mutate(jsonPatch(`/api/users/${id}/department`, { department }), 'Changing department')

  const setPermissions = (id: number, perms: string[] | null) =>
    mutate(jsonPatch(`/api/users/${id}/permissions`, { permissions: perms }),
           'Updating permissions')

  const togglePerm = (u: User, scope: string) => {
    const current = u.permissions || []
    const next = current.includes(scope) ? current.filter(p => p !== scope) : [...current, scope]
    setPermissions(u.id, next)
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
          <select
            value={newRole}
            onChange={e => setNewRole(e.target.value)}
            className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none"
          >
            <option value="user">user</option>
            <option value="manager">manager</option>
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
                  <option value="user">user</option>
                  <option value="manager">manager</option>
                  <option value="admin">admin</option>
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
                    onClick={() => setPermissions(u.id, null)}
                    className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
                    title="Reset to role defaults"
                  >
                    Reset to {u.role} defaults
                  </button>
                </div>
                <div className="flex flex-wrap gap-2">
                  {permMeta.scopes.map(scope => {
                    const active = u.permissions?.includes(scope)
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
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Tab: Knowledge Base ────────────────────────────────────────────────────
// Upload, list and remove sources, by department. A browse-and-search view
// over the indexed corpus is NOT here: it would need document-read and
// corpus-search endpoints this API does not serve.

function KBManage({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [sources, setSources] = useState<KBSource[]>([])
  const [departments, setDepartments] = useState<string[]>(['general'])
  const [filterDept, setFilterDept] = useState<string>('')
  const [uploadDept, setUploadDept] = useState<string>('general')
  const [uploading, setUploading] = useState(false)
  const [status, setStatus] = useState<{ ok: boolean; message: string } | null>(null)

  const loadDepts = () =>
    guardedJson<{ departments?: string[] }>(
      fetch(`${api}/api/ingest/departments`, { headers: headers() }), 'Loading departments')
      .then(d => { if (d) setDepartments(d.departments || ['general']) })

  const loadSources = (dept?: string) => {
    const url = dept ? `${api}/api/ingest/sources?department=${encodeURIComponent(dept)}` : `${api}/api/ingest/sources`
    guardedJson<{ sources?: KBSource[] }>(fetch(url, { headers: headers() }), 'Loading KB sources')
      .then(d => { if (d) setSources(d.sources || []) })
  }

  useEffect(() => {
    loadDepts()
    loadSources()
  }, [])

  const handleFilterChange = (dept: string) => {
    setFilterDept(dept)
    loadSources(dept || undefined)
  }

  const upload = async (file: File) => {
    setUploading(true)
    setStatus(null)
    const form = new FormData()
    form.append('file', file)
    form.append('department', uploadDept)
    try {
      const res = await fetch(`${api}/api/ingest/upload`, { method: 'POST', headers: headers(), body: form })
      const d = await res.json()
      if (!res.ok) throw new Error(d.detail || 'Upload failed')
      // 200 does NOT mean indexed. The ingest gate answers 200 with
      // status:"quarantined" for content it WITHHELD, and a queued upload
      // answers 200 with status:"queued" and no chunk count yet. Reporting all
      // three as success told an operator their document was in the corpus
      // when it was refused.
      if (d.status === 'quarantined') {
        setStatus({
          ok: false,
          message: `${d.source} was WITHHELD by the ingest gate (review id ${d.quarantine_id}). `
            + 'Read it under Quarantine and release it there if it is legitimate.',
        })
      } else if (d.status === 'queued') {
        setStatus({ ok: true, message: `${d.source} queued for ingestion (job ${d.job_id}) - watch it under Ingestion Queue.` })
      } else {
        setStatus({ ok: true, message: `${d.source} -> ${d.department} - ${d.chunks} chunks` })
      }
      loadDepts()
      loadSources(filterDept || undefined)
    } catch (e) {
      setStatus({ ok: false, message: e instanceof Error ? e.message : 'Upload failed' })
    } finally {
      setUploading(false)
    }
  }

  const remove = async (source: string, dept: string) => {
    const url = `${api}/api/ingest/source/${encodeURIComponent(source)}?department=${encodeURIComponent(dept)}`
    await fetch(url, { method: 'DELETE', headers: headers() })
    loadSources(filterDept || undefined)
  }

  const deptBadge = (dept: string) => (
    <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-400 font-mono">{dept}</span>
  )

  return (
    <div className="space-y-4">
      {/* Upload row */}
      <div className="flex gap-2 items-center">
        <label className="flex-1 border-2 border-dashed border-gray-700 hover:border-gray-500 rounded-xl px-4 py-6 text-center cursor-pointer transition-colors">
          <p className="text-sm text-gray-400">{uploading ? 'Uploading…' : 'Click to upload a document'}</p>
          <p className="text-xs text-gray-600 mt-1">.txt .md .pdf .docx .py .js .ts .json</p>
          <input
            type="file"
            className="hidden"
            accept=".txt,.md,.pdf,.docx,.py,.js,.ts,.json,.yaml,.yml"
            onChange={e => { const f = e.target.files?.[0]; if (f) upload(f) }}
          />
        </label>
        <div className="flex flex-col gap-2 min-w-36">
          <p className="text-xs text-gray-500">Upload to dept:</p>
          <input
            value={uploadDept}
            onChange={e => setUploadDept(e.target.value)}
            list="dept-list-kb"
            placeholder="general"
            className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-blue-500/60"
          />
          <datalist id="dept-list-kb">
            {departments.map(d => <option key={d} value={d} />)}
          </datalist>
        </div>
      </div>

      {status && (
        <p className={`text-xs px-1 ${status.ok ? 'text-green-400' : 'text-red-400'}`}>
          {status.ok ? '✓ ' : '✗ '}{status.message}
        </p>
      )}

      {/* Filter */}
      <div className="flex items-center gap-2">
        <p className="text-xs text-gray-500 uppercase tracking-widest">Sources ({sources.length})</p>
        <select
          value={filterDept}
          onChange={e => handleFilterChange(e.target.value)}
          className="ml-auto bg-gray-900 border border-gray-700 rounded-lg px-2 py-1 text-xs text-white outline-none"
        >
          <option value="">All departments</option>
          {departments.map(d => <option key={d} value={d}>{d}</option>)}
        </select>
      </div>

      {sources.length === 0 && <p className="text-xs text-gray-600">No documents ingested.</p>}
      {sources.map(s => (
        <div key={`${s.source}::${s.department}`} className="flex items-center justify-between px-4 py-3 bg-gray-800/50 border border-gray-700/50 rounded-xl group">
          <div className="flex items-center gap-2 min-w-0">
            {deptBadge(s.department)}
            <div className="min-w-0">
              <p className="text-sm text-gray-300 truncate max-w-xs">{s.source}</p>
              <p className="text-xs text-gray-600">{s.count} chunks</p>
            </div>
          </div>
          <button
            onClick={() => remove(s.source, s.department)}
            className="text-xs text-red-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity ml-4"
          >
            Remove
          </button>
        </div>
      ))}
    </div>
  )
}

// ── Tab: System Prompt ─────────────────────────────────────────────────────

function SystemPromptTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [prompt, setPrompt] = useState('')
  const [saved, setSaved] = useState(false)
  const [loading, setLoading] = useState(true)
  const [strategy, setStrategy] = useState<'warn' | 'summarize'>('warn')
  const [maxTokens, setMaxTokens] = useState(6000)
  const [strategySaved, setStrategySaved] = useState(false)
  const [encryptionVerified, setEncryptionVerified] = useState(false)
  const [suggestionsText, setSuggestionsText] = useState('')
  const [suggestionsSaved, setSuggestionsSaved] = useState(false)
  const [allowModelSelection, setAllowModelSelection] = useState(true)
  const [allowRagToggle, setAllowRagToggle] = useState(true)
  const [defaultModel, setDefaultModel] = useState('')
  const [defaultRagEnabled, setDefaultRagEnabled] = useState(false)
  const [guestChatEnabled, setGuestChatEnabled] = useState(true)
  const [availableModels, setAvailableModels] = useState<{ value: string; label: string }[]>([])
  const [controlsSaved, setControlsSaved] = useState(false)
  const [saveErr, setSaveErr] = useState('')

  useEffect(() => {
    fetch(`${api}/api/admin/config`, { headers: headers() })
      .then(r => r.json())
      .then((d: AdminConfig) => { setPrompt(d.system_prompt || ''); setLoading(false) })
      .catch(() => setLoading(false))
    fetch(`${api}/api/config`)
      .then(r => r.json())
      .then((d: {
        suggestions?: string[]
        allow_model_selection?: boolean
        allow_rag_toggle?: boolean
        default_model?: string
        default_rag_enabled?: boolean
        guest_mode_enabled?: boolean
      }) => {
        setSuggestionsText((d.suggestions || []).join('\n'))
        if (d.allow_model_selection !== undefined) setAllowModelSelection(d.allow_model_selection)
        if (d.allow_rag_toggle !== undefined) setAllowRagToggle(d.allow_rag_toggle)
        if (d.default_model) setDefaultModel(d.default_model)
        if (d.default_rag_enabled !== undefined) setDefaultRagEnabled(d.default_rag_enabled)
        if (d.guest_mode_enabled !== undefined) setGuestChatEnabled(d.guest_mode_enabled)
      })
      .catch(() => emitError('Loading public config failed'))
    guardedJson<{ groups?: { models: { value: string; label: string }[] }[] }>(
      fetch(`${api}/api/models`, { headers: headers() }), 'Loading models')
      .then(d => {
        if (!d) return
        const flat = (d.groups || []).flatMap(g => g.models)
        setAvailableModels(flat)
      })
    guardedJson<{ strategy?: 'warn' | 'summarize'; max_tokens?: number; encryption_verified?: boolean }>(
      fetch(`${api}/api/admin/context`, { headers: headers() }), 'Loading context settings')
      .then(d => {
        if (!d) return
        setStrategy(d.strategy || 'warn'); setMaxTokens(d.max_tokens || 6000); setEncryptionVerified(!!d.encryption_verified)
      })
  }, [])

  // Never confirm a save the server did not accept. An expired token makes
  // these PATCHes 401 while the UI still flashes "Saved", and the operator
  // walks away believing a setting changed - the failure is silent on both
  // sides, which is what makes it worth a helper rather than a habit.
  const guardedPatch = async (path: string, body: object): Promise<boolean> => {
    setSaveErr('')
    try {
      const res = await fetch(`${api}${path}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        setSaveErr(res.status === 401
          ? 'NOT saved - your session expired. Sign in again, then retry.'
          : `NOT saved - server returned ${res.status}.`)
        return false
      }
      return true
    } catch {
      setSaveErr('NOT saved - request failed (network).')
      return false
    }
  }

  const save = async () => {
    if (!(await guardedPatch('/api/admin/config', { system_prompt: prompt }))) return
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const saveSuggestions = async () => {
    const list = suggestionsText.split('\n').map(s => s.trim()).filter(Boolean)
    if (!(await guardedPatch('/api/admin/config', { suggestions: list }))) return
    setSuggestionsSaved(true)
    setTimeout(() => setSuggestionsSaved(false), 2000)
  }

  const saveControls = async (
    allowModel = allowModelSelection,
    allowRag = allowRagToggle,
    defModel = defaultModel,
    defRag = defaultRagEnabled,
    guestChat = guestChatEnabled,
  ) => {
    // Every key here must be in the backend's config allowlist. It rejects an
    // unknown key BY NAME with a 400 and validates the whole body before
    // writing any of it, so one stale key takes the entire block down with it -
    // which is exactly what guest_chat_enabled and guest_widget_enabled did.
    const ok = await guardedPatch('/api/admin/config', {
      allow_model_selection: allowModel,
      allow_rag_toggle: allowRag,
      default_model: defModel,
      default_rag_enabled: defRag,
      guest_mode_enabled: guestChat,
    })
    if (!ok) return
    setControlsSaved(true)
    setTimeout(() => setControlsSaved(false), 2000)
  }

  const saveStrategy = async (s: 'warn' | 'summarize') => {
    setStrategy(s)
    if (!(await guardedPatch('/api/admin/context', { strategy: s }))) return
    setStrategySaved(true)
    setTimeout(() => setStrategySaved(false), 2000)
  }

  if (loading) return <p className="text-xs text-gray-500">Loading…</p>

  return (
    <div className="space-y-6">
      {saveErr && (
        <p className="text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2">
          {saveErr}
        </p>
      )}
      {/* System prompt */}
      <div className="space-y-3">
        <p className="text-xs text-gray-400">
          This prompt is prepended to every conversation and defines the AI's role and persona.
          Changes take effect immediately - no restart required.
        </p>
        <textarea
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          rows={10}
          className="w-full bg-gray-900 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 outline-none resize-none font-mono leading-relaxed"
        />
        <div className="flex justify-end">
          <button
            onClick={save}
            className="px-5 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            {saved ? '✓ Saved' : 'Save Prompt'}
          </button>
        </div>
      </div>

      {/* Context window strategy */}
      <div className="border-t border-gray-800 pt-5 space-y-3">
        <div>
          <p className="text-xs text-gray-300 font-medium mb-0.5">Context Window Strategy</p>
          <p className="text-xs text-gray-500">
            Action taken when conversation history exceeds ~{maxTokens.toLocaleString()} tokens
            (set via <span className="font-mono">MAX_CONTEXT_TOKENS</span>).
          </p>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {(['warn', 'summarize'] as const).map(s => (
            <button
              key={s}
              onClick={() => saveStrategy(s)}
              className={`px-4 py-3 rounded-xl text-sm text-left border transition-colors ${
                strategy === s
                  ? 'border-blue-500/50 bg-blue-500/10 text-white'
                  : 'border-gray-700 bg-gray-800/50 text-gray-400 hover:text-white hover:border-gray-600'
              }`}
            >
              <p className="font-medium capitalize">{s}</p>
              <p className="text-xs mt-0.5 opacity-70">
                {s === 'warn'
                  ? 'Show banner to user, continue with full history'
                  : 'Auto-summarize older turns, compress context silently'}
              </p>
            </button>
          ))}
        </div>
        {strategySaved && <p className="text-xs text-green-400">Strategy saved</p>}
      </div>

      {/* Chat suggestions */}
      <div className="border-t border-gray-800 pt-5 space-y-3">
        <div>
          <p className="text-xs text-gray-300 font-medium mb-0.5">Chat Suggestions</p>
          <p className="text-xs text-gray-500">Shown on the empty chat screen. One suggestion per line (max 3 displayed).</p>
        </div>
        <textarea
          value={suggestionsText}
          onChange={e => setSuggestionsText(e.target.value)}
          rows={4}
          placeholder={"What can you help me with?\nHow do I get started?\nWhat kinds of questions can I ask?"}
          className="w-full bg-gray-900 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 outline-none resize-none leading-relaxed"
        />
        <div className="flex items-center justify-end gap-3">
          {suggestionsSaved && <span className="text-xs text-green-400">Saved</span>}
          <button
            onClick={saveSuggestions}
            className="px-5 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            Save Suggestions
          </button>
        </div>
      </div>

      {/* Usage controls */}
      <div className="border-t border-gray-800 pt-5 space-y-3">
        <div>
          <p className="text-xs text-gray-300 font-medium mb-0.5">Usage Controls</p>
          <p className="text-xs text-gray-500">Global defaults applied to all users. Hiding a control locks users to the value set here.</p>
        </div>

        {/* Default model */}
        <div className="space-y-1.5">
          <p className="text-xs text-gray-400">Default model</p>
          <select
            value={defaultModel}
            onChange={e => setDefaultModel(e.target.value)}
            className="w-full bg-gray-900 border border-gray-700 focus:border-blue-500/60 rounded-lg px-3 py-2 text-sm text-white outline-none"
          >
            <option value="">- use server default -</option>
            {availableModels.map(m => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </div>

        {/* Allow model selection toggle */}
        <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
          <div>
            <p className="text-sm text-gray-300">Allow model selection</p>
            <p className="text-xs text-gray-500 mt-0.5">Users can switch models in the sidebar</p>
          </div>
          <button
            onClick={() => { setAllowModelSelection(!allowModelSelection); saveControls(!allowModelSelection, allowRagToggle, defaultModel, defaultRagEnabled) }}
            className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${allowModelSelection ? 'bg-blue-600' : 'bg-gray-600'}`}
          >
            <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${allowModelSelection ? 'left-5' : 'left-0.5'}`} />
          </button>
        </div>

        {/* Default RAG */}
        <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
          <div>
            <p className="text-sm text-gray-300">RAG enabled by default</p>
            <p className="text-xs text-gray-500 mt-0.5">Knowledge base active for all new conversations</p>
          </div>
          <button
            onClick={() => { setDefaultRagEnabled(!defaultRagEnabled); saveControls(allowModelSelection, allowRagToggle, defaultModel, !defaultRagEnabled) }}
            className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${defaultRagEnabled ? 'bg-blue-600' : 'bg-gray-600'}`}
          >
            <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${defaultRagEnabled ? 'left-5' : 'left-0.5'}`} />
          </button>
        </div>

        {/* Allow RAG toggle */}
        <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
          <div>
            <p className="text-sm text-gray-300">Allow RAG toggle</p>
            <p className="text-xs text-gray-500 mt-0.5">Users can enable/disable knowledge base in the sidebar</p>
          </div>
          <button
            onClick={() => { setAllowRagToggle(!allowRagToggle); saveControls(allowModelSelection, !allowRagToggle, defaultModel, defaultRagEnabled) }}
            className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${allowRagToggle ? 'bg-blue-600' : 'bg-gray-600'}`}
          >
            <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${allowRagToggle ? 'left-5' : 'left-0.5'}`} />
          </button>
        </div>

        <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
          <div>
            <p className="text-sm text-gray-300">Guest access</p>
            <p className="text-xs text-gray-500 mt-0.5">Allow unauthenticated chat; when off, visitors get the sign-in wall. This is the admin half of a double gate - the host must also set ALLOW_GUEST_MODE=true, or guests stay refused whatever this says.</p>
          </div>
          <button
            onClick={() => { setGuestChatEnabled(!guestChatEnabled); saveControls(allowModelSelection, allowRagToggle, defaultModel, defaultRagEnabled, !guestChatEnabled) }}
            className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${guestChatEnabled ? 'bg-blue-600' : 'bg-gray-600'}`}
          >
            <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${guestChatEnabled ? 'left-5' : 'left-0.5'}`} />
          </button>
        </div>

        <div className="flex items-center justify-end gap-3 pt-1">
          {controlsSaved && <span className="text-xs text-green-400">Saved</span>}
          <button
            onClick={() => saveControls()}
            className="px-5 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            Save Controls
          </button>
        </div>
      </div>

      {/* Encryption at rest */}
      <div className="border-t border-gray-800 pt-5 space-y-2">
        <p className="text-xs text-gray-300 font-medium mb-1">Encryption at Rest</p>
        <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border text-sm ${
          encryptionVerified
            ? 'bg-green-900/20 border-green-700/40 text-green-300'
            : 'bg-gray-800/50 border-gray-700 text-gray-500'
        }`}>
          <span className={`text-base ${encryptionVerified ? 'text-green-400' : 'text-gray-600'}`}>
            {encryptionVerified ? '🔒' : '⚠'}
          </span>
          <div>
            <p className="text-xs font-medium">
              {encryptionVerified ? 'Host-verified' : 'Not verified'}
            </p>
            <p className="text-xs opacity-60 mt-0.5">
              {encryptionVerified
                ? 'ENCRYPTION_AT_REST_VERIFIED=true - host-level encryption confirmed'
                : 'Set ENCRYPTION_AT_REST_VERIFIED=true after enabling host-level or volume encryption. See docs/SECURITY-HARDENING.md'}
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Tab: Analytics ─────────────────────────────────────────────────────────

interface ModelGroup { provider: string; label: string; models: { value: string; label: string; badge: string }[] }
interface ModelSlot { value: string; effective?: string; default: string; overridden: boolean }
interface ModelConfig { chat: ModelSlot; eval_writer: ModelSlot; eval_judge: ModelSlot; same_family_warning: boolean }

function ModelSelect(props: {
  groups: ModelGroup[]
  value: string
  onChange: (v: string) => void
  followOption?: string   // label for a "" option (the eval writer's default)
}) {
  const known = new Set(props.groups.flatMap(g => g.models.map(m => m.value)))
  return (
    <select value={props.value} onChange={e => props.onChange(e.target.value)}
      className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-gray-500 min-w-[260px]">
      {props.followOption !== undefined && <option value="">{props.followOption}</option>}
      {props.value && !known.has(props.value) && (
        <option value={props.value}>{props.value} (saved)</option>
      )}
      {props.groups.map(g => (
        <optgroup key={g.provider} label={g.label}>
          {g.models.map(m => (
            <option key={m.value} value={m.value}>
              {m.label}{m.badge ? ` - ${m.badge}` : ''}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  )
}

function ModelsTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [groups, setGroups] = useState<ModelGroup[]>([])
  const [cfg, setCfg] = useState<ModelConfig | null>(null)
  const [draft, setDraft] = useState<{ chat: string; eval_writer: string; eval_judge: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    guardedJson<{ groups?: ModelGroup[] }>(
      fetch(`${api}/api/models`, { headers: headers() }), 'Loading models')
      .then(d => { if (d) setGroups(d.groups || []) })
    guardedJson<ModelConfig>(
      fetch(`${api}/api/admin/model-config`, { headers: headers() }), 'Loading model config')
      .then(d => {
        if (!d) return
        setCfg(d)
        setDraft({ chat: d.chat.value, eval_writer: d.eval_writer.value, eval_judge: d.eval_judge.value })
      })
  }, [api])

  const dirty = !!cfg && !!draft && (
    draft.chat !== cfg.chat.value ||
    draft.eval_writer !== cfg.eval_writer.value ||
    draft.eval_judge !== cfg.eval_judge.value
  )

  const save = async () => {
    if (!draft) return
    setSaving(true); setMsg('')
    try {
      const r = await fetch(`${api}/api/admin/model-config`, {
        method: 'PATCH',
        headers: { ...headers(), 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      })
      if (!r.ok) { const d = await r.json().catch(() => ({})); setMsg(d.detail || `Save failed (HTTP ${r.status})`); return }
      const d: ModelConfig = await r.json()
      setCfg(d)
      setDraft({ chat: d.chat.value, eval_writer: d.eval_writer.value, eval_judge: d.eval_judge.value })
      setMsg('Saved')
    } catch {
      setMsg('Save failed - could not reach the backend')
    } finally {
      setSaving(false)
    }
  }

  const flat = groups.flatMap(g => g.models)

  if (!cfg || !draft) return <p className="text-xs text-gray-600">Loading model config…</p>

  const rows: { key: 'chat' | 'eval_writer' | 'eval_judge'; label: string; desc: string; slot: ModelSlot; followOption?: string }[] = [
    { key: 'chat', label: 'Chat default', slot: cfg.chat,
      desc: 'What visitors get when they do not pick a model.' },
    { key: 'eval_writer', label: 'Eval answer writer', slot: cfg.eval_writer, followOption: 'Follow the chat default',
      desc: 'Pinned per run so a chat-dial change can never silently change what a measurement measures.' },
    { key: 'eval_judge', label: 'Eval judge', slot: cfg.eval_judge,
      desc: 'Grades every answer. Must come from a different company than the writer - the guard blocks same-family runs.' },
  ]

  return (
    <div className="space-y-8 max-w-3xl">
      <section className="space-y-3">
        <p className="text-xs text-gray-500 uppercase tracking-widest">Per-feature model pinning</p>

        {cfg.same_family_warning && (
          <div className="px-4 py-3 rounded-xl border border-amber-500/40 bg-amber-500/10 text-sm text-amber-300">
            ⚠ The eval writer and judge resolve to the SAME provider family - eval runs
            will be refused until one changes (or is deliberately overridden per run).
          </div>
        )}

        {rows.map(row => {
          const draftVal = draft[row.key]
          const overridden = row.key === 'eval_writer' ? draftVal !== '' : draftVal !== row.slot.default
          return (
            <div key={row.key} className="bg-gray-800/40 border border-gray-700/50 rounded-xl px-4 py-3.5">
              <div className="flex flex-wrap items-center gap-3">
                <div className="w-44">
                  <p className="text-sm font-medium text-gray-200">{row.label}</p>
                </div>
                <ModelSelect groups={groups} value={draftVal}
                  followOption={row.followOption}
                  onChange={v => { setMsg(''); setDraft({ ...draft, [row.key]: v }) }} />
                {overridden ? (
                  <>
                    {/* "pinned here", not "overridden from default": an explicit pin
                        can equal the default value - the badge marks the pin, and
                        claiming a difference that may not exist reads as a bug. */}
                    <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-300">
                      pinned here
                    </span>
                    <button
                      onClick={() => { setMsg(''); setDraft({ ...draft, [row.key]: row.key === 'eval_writer' ? '' : row.slot.default }) }}
                      className="text-xs text-gray-500 hover:text-white underline">
                      reset
                    </button>
                  </>
                ) : (
                  <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-700/50 text-gray-500">default</span>
                )}
              </div>
              <p className="text-xs text-gray-500 mt-2">{row.desc}
                {row.key === 'eval_writer' && cfg.eval_writer.effective && draftVal === '' &&
                  ` Currently resolves to ${cfg.eval_writer.effective}.`}
              </p>
            </div>
          )
        })}

        <div className="flex items-center gap-3">
          <button onClick={save} disabled={!dirty || saving}
            className="px-5 py-2 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-40"
            style={{ backgroundColor: PRIMARY_COLOR }}>
            {saving ? 'Saving…' : 'Save changes'}
          </button>
          {msg && <span className={`text-xs ${msg === 'Saved' ? 'text-emerald-400' : 'text-red-400'}`}>{msg}</span>}
          {!dirty && !msg && <span className="text-xs text-gray-600">No unsaved changes</span>}
        </div>
      </section>

      <section className="space-y-2">
        <p className="text-xs text-gray-500 uppercase tracking-widest">Available models ({flat.length})</p>
        {flat.length === 0 && <p className="text-xs text-gray-600">No models available. Check provider flags in .env.</p>}
        {groups.map(g => (
          <div key={g.provider}>
            <p className="text-[10px] text-gray-600 uppercase tracking-widest mt-3 mb-1">{g.label}</p>
            {g.models.map(m => (
              <div key={m.value} className="px-4 py-2.5 bg-gray-800/50 border border-gray-700/50 rounded-xl flex items-center justify-between mb-1">
                <p className="text-sm text-gray-300 font-mono">{m.label}</p>
                {m.badge && <span className="text-xs text-gray-500 bg-gray-700/50 px-2 py-0.5 rounded">{m.badge}</span>}
              </div>
            ))}
          </div>
        ))}
      </section>
    </div>
  )
}

// ── Tab: PII Safety ────────────────────────────────────────────────────────

interface AuditEntry {
  id: number
  username: string
  session_id: string
  timestamp: string
  prompt_preview: string
  response_length: number
  model: string | null
  use_rag: boolean
  sources: string[]
}

function AuditTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [entries, setEntries]     = useState<AuditEntry[]>([])
  const [total, setTotal]         = useState(0)
  const [pages, setPages]         = useState(1)
  const [page, setPage]           = useState(1)
  const [loading, setLoading]     = useState(true)
  const [exporting, setExporting] = useState(false)
  const [filterUser, setFilterUser] = useState('')
  const [filterFrom, setFilterFrom] = useState('')
  const [filterTo, setFilterTo]     = useState('')

  const load = (p = page) => {
    setLoading(true)
    const params = new URLSearchParams({ page: String(p), page_size: '50' })
    if (filterUser) params.set('username', filterUser)
    if (filterFrom) params.set('date_from', filterFrom)
    if (filterTo)   params.set('date_to', filterTo)
    guardedJson<{ entries?: AuditEntry[]; total?: number; pages?: number }>(
      fetch(`${api}/api/admin/audit?${params}`, { headers: headers() }), 'Loading audit log')
      .then(d => {
        if (!d) return
        setEntries(d.entries || [])
        setTotal(d.total || 0)
        setPages(d.pages || 1)
        setPage(p)
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => { load(1) }, [])

  const exportCsv = async () => {
    setExporting(true)
    try {
      const params = new URLSearchParams()
      if (filterUser) params.set('username', filterUser)
      if (filterFrom) params.set('date_from', filterFrom)
      if (filterTo)   params.set('date_to', filterTo)
      const res = await fetch(`${api}/api/admin/audit/export?${params}`, { headers: headers() })
      const text = await res.text()
      const blob = new Blob([text], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'audit_log.csv'
      a.click()
      URL.revokeObjectURL(url)
    } catch { /* ignore */ } finally {
      setExporting(false)
    }
  }

  const fmtTs = (ts: string) => {
    try { return new Date(ts + 'Z').toLocaleString() } catch { return ts }
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Username</label>
          <input
            value={filterUser} onChange={e => setFilterUser(e.target.value)}
            placeholder="Filter by user…"
            className="bg-gray-800 border border-gray-700 text-white text-xs rounded-lg px-3 py-2 outline-none focus:border-blue-500/60 w-44"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">From</label>
          <input type="date" value={filterFrom} onChange={e => setFilterFrom(e.target.value)}
            className="bg-gray-800 border border-gray-700 text-white text-xs rounded-lg px-3 py-2 outline-none focus:border-blue-500/60"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">To</label>
          <input type="date" value={filterTo} onChange={e => setFilterTo(e.target.value)}
            className="bg-gray-800 border border-gray-700 text-white text-xs rounded-lg px-3 py-2 outline-none focus:border-blue-500/60"
          />
        </div>
        <button
          onClick={() => load(1)}
          className="text-xs text-white px-4 py-2 rounded-lg transition-colors"
          style={{ backgroundColor: PRIMARY_COLOR }}
        >
          Search
        </button>
        <button
          onClick={exportCsv}
          disabled={exporting}
          className="text-xs text-gray-400 hover:text-white px-4 py-2 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors disabled:opacity-50 ml-auto"
        >
          {exporting ? 'Exporting…' : 'Export CSV'}
        </button>
      </div>

      {/* Summary */}
      <p className="text-xs text-gray-500">{total} total entries</p>

      {/* Table */}
      {loading ? (
        <p className="text-xs text-gray-600">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="text-xs text-gray-600">No audit entries found.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-gray-800">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-800 text-gray-500 uppercase tracking-wider">
                <th className="text-left px-4 py-2 font-medium">Timestamp</th>
                <th className="text-left px-4 py-2 font-medium">User</th>
                <th className="text-left px-4 py-2 font-medium">Session</th>
                <th className="text-left px-4 py-2 font-medium">Model</th>
                <th className="text-left px-4 py-2 font-medium">RAG</th>
                <th className="text-right px-4 py-2 font-medium">Chars</th>
                <th className="text-left px-4 py-2 font-medium">Prompt</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id} className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
                  <td className="px-4 py-2 text-gray-400 whitespace-nowrap">{fmtTs(e.timestamp)}</td>
                  <td className="px-4 py-2 text-white">{e.username}</td>
                  <td className="px-4 py-2 text-gray-500 font-mono">{e.session_id.slice(0, 8)}…</td>
                  <td className="px-4 py-2 text-gray-400">{e.model || '-'}</td>
                  <td className="px-4 py-2">
                    {e.use_rag
                      ? <span className="text-blue-400">Yes</span>
                      : <span className="text-gray-600">No</span>}
                  </td>
                  <td className="px-4 py-2 text-right text-gray-500">{e.response_length.toLocaleString()}</td>
                  <td className="px-4 py-2 text-gray-300 max-w-xs truncate" title={e.prompt_preview}>
                    {e.prompt_preview.length > 80 ? e.prompt_preview.slice(0, 80) + '…' : e.prompt_preview}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {pages > 1 && (
        <div className="flex items-center gap-3 justify-end">
          <button
            onClick={() => load(page - 1)} disabled={page <= 1}
            className="text-xs text-gray-400 hover:text-white disabled:opacity-30 px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors"
          >
            ← Prev
          </button>
          <span className="text-xs text-gray-500">Page {page} of {pages}</span>
          <button
            onClick={() => load(page + 1)} disabled={page >= pages}
            className="text-xs text-gray-400 hover:text-white disabled:opacity-30 px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors"
          >
            Next →
          </button>
        </div>
      )}
    </div>
  )
}

// ── MonitoringTab ──────────────────────────────────────────────────────────

interface ProviderHealth { name: string; ok: boolean; latency_ms: number | null }
interface DetailedHealth {
  disk: { used_gb: number; total_gb: number; pct: number; ok: boolean } | { error: string }
  db_ms: number | null
  db_error?: string
  providers: ProviderHealth[]
  last_request_at: string | null
  otel_configured: boolean
  alerts: { webhook_configured: boolean; email_configured: boolean; disk_threshold_pct: number; cooldown_seconds: number }
  metrics: Record<string, number>
}

function MonitoringTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [health, setHealth] = useState<DetailedHealth | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const load = async () => {
    setLoading(true); setErr('')
    try {
      const r = await fetch(`${api}/api/health/detailed`, { headers: headers() })
      if (!r.ok) { setErr('Failed to load health data'); return }
      setHealth(await r.json())
    } catch { setErr('Could not reach the server') }
    finally { setLoading(false) }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [])

  const downloadScrapeConfig = () => {
    const yaml = `# Prometheus scrape config for Architecture Zero
# Add under scrape_configs in prometheus.yml
scrape_configs:
  - job_name: 'architecture-zero'
    static_configs:
      - targets: ['YOUR_HOST:80']
    metrics_path: /metrics
    scrape_interval: 30s
`
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([yaml], { type: 'text/yaml' }))
    a.download = 'az-prometheus-scrape.yml'
    a.click()
  }

  const hasDiskError = health && 'error' in health.disk
  const disk = health && !hasDiskError ? health.disk as { used_gb: number; total_gb: number; pct: number; ok: boolean } : null

  return (
    <div className="max-w-2xl space-y-5">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500">Auto-refreshes every 30s</p>
        <button onClick={load} disabled={loading}
          className="text-xs text-gray-400 hover:text-white transition-colors disabled:opacity-50">
          {loading ? 'Refreshing…' : '↻ Refresh'}
        </button>
      </div>

      {err && <p className="text-sm text-red-400">{err}</p>}

      {health && <>
        {/* Providers */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
          <p className="text-sm font-medium mb-3">Providers</p>
          {health.providers.length === 0
            ? <p className="text-xs text-gray-500">No providers configured</p>
            : <div className="space-y-2">
                {health.providers.map(p => (
                  <div key={p.name} className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${p.ok ? 'bg-green-500' : 'bg-red-500'}`} />
                      <span className="text-sm capitalize">{p.name}</span>
                    </div>
                    <span className="text-xs text-gray-500">
                      {p.latency_ms != null ? `${p.latency_ms}ms` : p.ok ? 'configured' : 'unreachable'}
                    </span>
                  </div>
                ))}
              </div>
          }
        </div>

        {/* System */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
          <p className="text-sm font-medium mb-3">System</p>
          <div className="space-y-3">
            {hasDiskError
              ? <p className="text-xs text-red-400">Disk: {(health.disk as { error: string }).error}</p>
              : disk && <>
                  <div>
                    <div className="flex items-center justify-between text-xs mb-1">
                      <span className="text-gray-400">Disk</span>
                      <span className={disk.ok ? 'text-gray-300' : 'text-yellow-400'}>
                        {disk.pct}% - {disk.used_gb} / {disk.total_gb} GB
                      </span>
                    </div>
                    <div className="w-full bg-gray-800 rounded-full h-1.5">
                      <div className={`h-1.5 rounded-full transition-all ${disk.pct > 85 ? 'bg-red-500' : disk.pct > 70 ? 'bg-yellow-500' : 'bg-green-500'}`}
                        style={{ width: `${Math.min(disk.pct, 100)}%` }} />
                    </div>
                  </div>
                </>
            }
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400">Database</span>
              <span className={health.db_ms != null ? 'text-gray-300' : 'text-red-400'}>
                {health.db_ms != null ? `${health.db_ms}ms` : health.db_error || 'error'}
              </span>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400">Last request</span>
              <span className="text-gray-300">
                {health.last_request_at ? new Date(health.last_request_at).toLocaleTimeString() : 'none recorded'}
              </span>
            </div>
          </div>
        </div>

        {/* Counters */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
          <p className="text-sm font-medium mb-3">Request Counters (since last restart)</p>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
            {Object.entries(health.metrics).map(([key, val]) => (
              <div key={key} className="flex items-center justify-between text-xs">
                <span className="text-gray-400">{key.replace(/_/g, ' ')}</span>
                <span className="text-gray-300 font-mono">{val}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Alerts */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-3">
          <p className="text-sm font-medium">Alerting</p>
          <div className="space-y-1.5">
            {[
              { label: 'Webhook', ok: health.alerts.webhook_configured },
              { label: 'Email (SMTP)', ok: health.alerts.email_configured },
              { label: 'OpenTelemetry', ok: health.otel_configured },
            ].map(({ label, ok }) => (
              <div key={label} className="flex items-center gap-2 text-xs">
                <span className={`w-2 h-2 rounded-full ${ok ? 'bg-green-500' : 'bg-gray-600'}`} />
                <span className="text-gray-400">{label} {ok ? 'configured' : 'not configured'}</span>
              </div>
            ))}
          </div>
          <p className="text-xs text-gray-500">
            Disk alert fires at {health.alerts.disk_threshold_pct}% usage (1h cooldown).
            Configure via <span className="font-mono text-gray-400">ALERT_WEBHOOK_URL</span>,{' '}
            <span className="font-mono text-gray-400">ALERT_EMAIL</span>, and{' '}
            <span className="font-mono text-gray-400">DISK_ALERT_THRESHOLD_PCT</span> in .env.
          </p>
        </div>

        {/* Prometheus */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-3">
          <p className="text-sm font-medium">Prometheus</p>
          <p className="text-xs text-gray-500">
            <span className="font-mono text-gray-400">GET /metrics</span> exposes counters in Prometheus text format.
            See <span className="font-mono text-gray-400">docs/OBSERVABILITY.md</span> for Grafana setup.
          </p>
          <button onClick={downloadScrapeConfig}
            className="px-3 py-1.5 rounded-lg text-xs border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 transition-colors">
            ↓ Download scrape config
          </button>
        </div>
      </>}
    </div>
  )
}

// ── BackupTab ──────────────────────────────────────────────────────────────

interface BackupStatus {
  last_backup: string | null
  last_backup_file: string | null
}

interface BackupResult {
  file: string
  size_bytes: number
  timestamp: string
}

function BackupTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [status, setStatus] = useState<BackupStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<BackupResult | null>(null)
  const [error, setError]   = useState('')

  useEffect(() => {
    guardedJson<BackupStatus>(
      fetch(`${api}/api/admin/backup/status`, { headers: headers() }), 'Loading backup status')
      .then(d => { if (d) setStatus(d) })
  }, [])

  const fmtSize = (bytes: number) => {
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const runBackup = async () => {
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const r = await fetch(`${api}/api/admin/backup`, { method: 'POST', headers: headers() })
      const data = await r.json()
      if (!r.ok) { setError(data.detail || 'Backup failed'); return }
      setResult(data)
      setStatus({ last_backup: data.timestamp, last_backup_file: data.file })
    } catch {
      setError('Could not reach the server')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-xl space-y-5">
      <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
        <p className="text-sm font-medium mb-3">Last Backup</p>
        {status?.last_backup ? (
          <div className="space-y-1">
            <p className="text-sm text-gray-300">{new Date(status.last_backup).toLocaleString()}</p>
            {status.last_backup_file && (
              <p className="text-xs text-gray-500 font-mono">{status.last_backup_file}</p>
            )}
          </div>
        ) : (
          <p className="text-sm text-gray-500">No backup on record</p>
        )}
      </div>

      <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-4">
        <div>
          <p className="text-sm font-medium">On-demand Backup</p>
          <p className="text-xs text-gray-500 mt-1">
            Snapshots all data (ChromaDB + database) into a compressed archive at{' '}
            <span className="font-mono text-gray-400">/app/data/backups/</span> inside the
            backend container. Archives older than {30} days are pruned automatically.
          </p>
        </div>
        <button
          onClick={runBackup}
          disabled={loading}
          className="px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ backgroundColor: PRIMARY_COLOR }}
        >
          {loading ? 'Backing up…' : 'Run Backup Now'}
        </button>
        {result && (
          <div className="text-xs text-green-400 space-y-0.5">
            <p>✓ Backup complete</p>
            <p className="font-mono text-gray-400">{result.file} ({fmtSize(result.size_bytes)})</p>
          </div>
        )}
        {error && <p className="text-xs text-red-400">{error}</p>}
      </div>

      <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-3">
        <p className="text-sm font-medium">Scheduled Backup (host cron)</p>
        <p className="text-xs text-gray-500">
          Run <span className="font-mono text-gray-400">scripts/backup.sh</span> on a schedule from
          the host to write backups to an external location with 30-day rotation.
        </p>
        <pre className="text-xs text-gray-400 bg-gray-800 rounded-lg p-3 overflow-x-auto whitespace-pre">{`# /etc/cron.d/az-backup - daily at 2 am
0 2 * * * root cd /opt/your-instance && ./scripts/backup.sh`}</pre>
        <p className="text-xs text-gray-500">
          See <span className="font-mono text-gray-400">docs/DISASTER-RECOVERY.md</span> for full
          restore procedures and off-site backup recommendations.
        </p>
      </div>
    </div>
  )
}

// ── Tab: Ingestion Queue ───────────────────────────────────────────────────

interface IngestJob {
  job_id: string
  status: 'queued' | 'running' | 'complete' | 'failed'
  source: string
  department: string
  chunks_processed: number
  chunks_total: number | null
  error: string | null
  created_at: string
  completed_at: string | null
}

function IngestQueueTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [jobs, setJobs] = useState<IngestJob[]>([])
  const [enabled, setEnabled] = useState(false)
  const [expandedJob, setExpandedJob] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = () => {
    guardedJson<{ enabled?: boolean; jobs?: IngestJob[] }>(
      fetch(`${api}/api/admin/jobs`, { headers: headers() }), 'Loading ingestion jobs')
      .then(d => {
        if (d) {
          setEnabled(d.enabled ?? false)
          setJobs(d.jobs ?? [])
        }
        setLoading(false)
      })
  }

  useEffect(() => {
    load()
    // Auto-refresh while any job is queued or running
    const id = setInterval(() => {
      setJobs(prev => {
        const active = prev.some(j => j.status === 'queued' || j.status === 'running')
        if (active) load()
        return prev
      })
    }, 3000)
    return () => clearInterval(id)
  }, [])

  const statusBadge = (status: IngestJob['status']) => {
    const styles: Record<string, string> = {
      queued:   'bg-gray-700 text-gray-300',
      running:  'bg-blue-900/60 text-blue-300',
      complete: 'bg-green-900/60 text-green-300',
      failed:   'bg-red-900/60 text-red-400',
    }
    return (
      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${styles[status] ?? 'bg-gray-700 text-gray-400'}`}>
        {status === 'running' ? '⟳ running' : status}
      </span>
    )
  }

  const pct = (j: IngestJob) => {
    if (!j.chunks_total || j.chunks_total === 0) return 0
    return Math.round((j.chunks_processed / j.chunks_total) * 100)
  }

  const fmt = (iso: string | null) => {
    if (!iso) return '-'
    try { return new Date(iso).toLocaleString() } catch { return iso }
  }

  if (loading) return <p className="text-sm text-gray-400">Loading…</p>

  return (
    <div className="space-y-4">
      {!enabled && (
        <div className="bg-yellow-900/30 border border-yellow-700/50 rounded-xl p-4">
          <p className="text-sm text-yellow-300 font-medium">Async jobs disabled</p>
          <p className="text-xs text-yellow-500 mt-1">
            Set <span className="font-mono">ENABLE_ASYNC_JOBS=true</span> and start the Celery worker
            service to enable background ingestion. Uploads currently run synchronously.
          </p>
        </div>
      )}

      <div className="bg-gray-800/50 border border-gray-700 rounded-xl overflow-hidden">
        <div className="p-4 border-b border-gray-700 flex items-center justify-between">
          <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Recent Jobs</p>
          <button
            onClick={load}
            className="text-xs text-gray-500 hover:text-white transition-colors"
          >
            Refresh
          </button>
        </div>

        {jobs.length === 0 ? (
          <p className="text-sm text-gray-500 p-4">No ingestion jobs yet.</p>
        ) : (
          <div className="divide-y divide-gray-700/50">
            {jobs.map(j => (
              <div key={j.job_id} className="p-4 space-y-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm text-white truncate font-medium">{j.source}</p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      dept: {j.department} · {fmt(j.created_at)}
                    </p>
                  </div>
                  <div className="flex-shrink-0">{statusBadge(j.status)}</div>
                </div>

                {/* Progress bar (running/complete) */}
                {(j.status === 'running' || j.status === 'complete') && j.chunks_total !== null && (
                  <div className="space-y-1">
                    <div className="w-full bg-gray-700 rounded-full h-1.5">
                      <div
                        className="h-1.5 rounded-full transition-all"
                        style={{
                          width: `${pct(j)}%`,
                          backgroundColor: j.status === 'complete' ? '#22c55e' : PRIMARY_COLOR,
                        }}
                      />
                    </div>
                    <p className="text-xs text-gray-500">
                      {j.chunks_processed} / {j.chunks_total} chunks ({pct(j)}%)
                    </p>
                  </div>
                )}

                {/* Error detail */}
                {j.status === 'failed' && j.error && (
                  <div>
                    <button
                      onClick={() => setExpandedJob(expandedJob === j.job_id ? null : j.job_id)}
                      className="text-xs text-red-400 hover:text-red-300"
                    >
                      {expandedJob === j.job_id ? '▲ hide error' : '▼ show error'}
                    </button>
                    {expandedJob === j.job_id && (
                      <pre className="mt-2 text-xs text-red-300 bg-red-900/20 border border-red-800/40 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">
                        {j.error}
                      </pre>
                    )}
                  </div>
                )}

                {j.completed_at && (
                  <p className="text-xs text-gray-600">Completed: {fmt(j.completed_at)}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="bg-gray-800/30 border border-gray-700/50 rounded-xl p-4 space-y-2">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Setup</p>
        <p className="text-xs text-gray-500">Enable the Celery worker in <span className="font-mono text-gray-400">docker-compose.yml</span>:</p>
        <pre className="text-xs text-gray-400 bg-gray-800 rounded-lg p-3 overflow-x-auto whitespace-pre">{`# Uncomment in docker-compose.yml
celery-worker:
  build: ./backend
  command: celery -A app.jobs.celery_app worker --loglevel=info
  env_file: .env
  environment:
    - ENABLE_ASYNC_JOBS=true
  volumes:
    - ./backend/data:/app/data`}</pre>
      </div>
    </div>
  )
}

// ── Tab: Evals ─────────────────────────────────────────────────────────────

interface TrustBand { low: number; high: number; runs: number }

interface AdminTrustData {
  available: boolean
  reason?: string
  honesty?: { pct: number; n: number; measured_at: string } | null
  correctness?: TrustBand | null
  holdout?: TrustBand | null
  gap?: TrustBand | null
  faithfulness?: TrustBand | null
  freshness?: TrustBand | null
  retrieval?: { pct: number } | null
  measured_at?: string | null
  corpus_fingerprint_short?: string | null
  cross_family_judging?: boolean
  provenance?: {
    writer: string | null
    judge: string
    corpus_fingerprint: string | null
    band_run_ids: string[]
    question_set: { total: number; honesty: number }
  }
  instrument?: { hybrid_persona_grounding: boolean; judge_input_boundary: boolean }
}

function trustBandText(b: TrustBand | null | undefined): string {
  if (!b) return 'not yet measured'
  if (b.low === b.high) return `${b.low}%`
  return `${b.low}-${b.high}%`
}

function trustGapText(b: TrustBand | null | undefined): string {
  if (!b) return 'not yet measured'
  const f = (v: number) => `${v > 0 ? '+' : ''}${v}`
  if (b.low === b.high) return `${f(b.low)} pts`
  return `${f(b.low)} to ${f(b.high)} pts`
}

function TrustTile(props: { value: string; label: string; note: string; good?: boolean }) {
  return (
    <div className="bg-gray-800/40 border border-gray-700/50 rounded-xl px-4 py-3.5">
      <div className={`text-2xl font-semibold tabular-nums ${props.good ? 'text-emerald-400' : 'text-white'}`}>
        {props.value}
      </div>
      <div className="text-[13px] font-medium mt-0.5 text-gray-200">{props.label}</div>
      <div className="text-xs mt-1 leading-snug text-gray-500">{props.note}</div>
    </div>
  )
}

function TrustTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [data, setData] = useState<AdminTrustData | null>(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState('')

  const load = () => {
    fetch(`${api}/api/admin/trust`, { headers: headers() })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(setData).catch(e => setError(String(e)))
  }
  useEffect(load, [])

  // The first measurement, from the tab that displays it. A retrieval-only
  // pass on purpose: it needs no model calls, so it costs nothing and cannot
  // fail on a missing provider key, and it still produces a real stored run.
  // Answer-mode passes are the API's job (POST /api/admin/evals/run with
  // retrieval_only=false) - this is the floor, not the whole instrument.
  const runFirstMeasurement = async () => {
    setRunning(true)
    setProgress('Seeding the question set...')
    try {
      const seeded = await actionError(
        fetch(`${api}/api/admin/evals/questions/seed`, { method: 'POST', headers: headers() }),
        'Seeding evaluation questions')
      if (seeded) return
      setProgress('Running retrieval...')
      const started = await fetch(`${api}/api/admin/evals/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ retrieval_only: true }),
      })
      const run = await started.json().catch(() => ({}))
      if (!started.ok) { emitError(run.detail || 'Could not start the run'); return }
      // The run is a background thread with a status endpoint; poll it rather
      // than guessing how long a corpus takes.
      for (let i = 0; i < 600; i++) {
        await new Promise(r => setTimeout(r, 1000))
        const st = await guardedPoll<{ done?: number; total?: number; complete?: boolean }>(
          fetch(`${api}/api/admin/evals/run-status/${run.run_id}`, { headers: headers() }))
        if (!st) break
        setProgress(`Running retrieval... ${st.done ?? 0} / ${st.total ?? '?'}`)
        if (st.complete) break
      }
      setProgress('')
      load()
    } finally {
      setRunning(false)
      setProgress('')
    }
  }

  if (error) return <p className="text-sm text-red-400">Could not load the trust panel ({error}).</p>
  if (!data) return <p className="text-sm text-gray-500">Loading measured numbers...</p>
  if (!data.available) {
    return (
      <div className="max-w-2xl space-y-4">
        <p className="text-sm text-gray-400">
          No complete measured runs yet. Every number on this page is derived
          from stored evaluation runs at request time - so until one has been
          run, there is honestly nothing to show.
        </p>
        <p className="text-sm text-gray-500">
          A retrieval-only pass is the cheapest place to start: it makes no
          model calls, needs no provider key, and answers the first question
          worth asking - does retrieval surface the right documents?
        </p>
        <button
          onClick={runFirstMeasurement}
          disabled={running}
          className="text-sm px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 transition-colors"
        >
          {running ? (progress || 'Working...') : 'Run the first measurement'}
        </button>
        {running && (
          <p className="text-xs text-gray-600">
            This seeds the shipped question set and runs it. Leaving this tab
            does not stop it - the run continues server-side.
          </p>
        )}
      </div>
    )
  }

  const gapGood = !!data.gap && data.gap.high <= 0

  // Same honesty rule as the public panel: a band of one run is a point, not
  // a spread - the caption must not promise more than the data holds.
  const bandN = Math.max(
    data.correctness?.runs ?? 0,
    data.holdout?.runs ?? 0,
    data.faithfulness?.runs ?? 0,
    data.freshness?.runs ?? 0,
  )

  return (
    <div className="space-y-8 max-w-4xl">
      <div>
        <p className="text-sm text-gray-400 mb-4 max-w-2xl">
          Every number below is derived from stored evaluation runs at request
          time - nothing typed in. Answers are written by one company&apos;s
          model and graded by a different company&apos;s model.{' '}
          <a href="/#trust" target="_blank" rel="noopener noreferrer"
            className="underline hover:text-white" style={{ color: PRIMARY_COLOR }}>
            Open the public trust page
          </a>
        </p>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
          {data.honesty && (
            <TrustTile value={`${data.honesty.pct}%`} good={data.honesty.pct === 100}
              label="Honesty under pressure"
              note={`${data.honesty.n} demands for records that do not exist - refused or corrected, none invented`} />
          )}
          <TrustTile value={trustBandText(data.correctness)}
            label="Answer correctness" note="tuned cohort, graded against owner-written keys" />
          {data.holdout && (
            <TrustTile value={trustBandText(data.holdout)}
              label="Held-out exam" note="questions the tuning never sees" />
          )}
          {data.gap && (
            <TrustTile value={trustGapText(data.gap)} good={gapGood}
              label="Tuned-vs-holdout gap"
              note={gapGood ? 'the locked exam scores HIGHER - published, not hidden'
                            : 'how much the tunable score flatters vs the locked exam'} />
          )}
          <TrustTile value={trustBandText(data.faithfulness)}
            good={!!data.faithfulness && data.faithfulness.low === 100}
            label="Claims grounded in sources" note="claims must trace to retrieved documents" />
          <TrustTile value={trustBandText(data.freshness)}
            label="Source freshness" note="the material served is current" />
          {data.retrieval && (
            <TrustTile value={`${data.retrieval.pct}%`}
              label="Right document found" note="for questions with a known home document" />
          )}
        </div>
        <p className="text-xs text-gray-500 mt-3">
          Last measured {data.measured_at}.{' '}
          {bandN > 1
            ? 'Spreads are low-high bands across identical runs, never one lucky point.'
            : 'Values are from a single run at the current configuration so far; repeat runs at the same configuration widen them into low-high bands rather than one lucky point.'}
        </p>
      </div>

      {data.provenance && (
        <section className="bg-gray-800/30 border border-gray-700/50 rounded-xl p-5">
          <h2 className="text-sm font-semibold text-gray-200 mb-3">Provenance (operator view)</h2>
          <dl className="text-sm space-y-2">
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Answers written by</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5">{data.provenance.writer || 'unknown'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Graded by</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5">{data.provenance.judge || 'unknown'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Corpus fingerprint</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5 break-all">{data.provenance.corpus_fingerprint || 'unstamped'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Band run ids</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5 break-all">{data.provenance.band_run_ids.map(id => id.slice(0, 8)).join(', ') || 'none'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Question set</dt>
              <dd className="text-gray-300 text-xs pt-0.5">{data.provenance.question_set.total} questions ({data.provenance.question_set.honesty} honesty traps)</dd></div>
          </dl>
          {data.instrument && (
            <p className="text-xs text-gray-500 mt-3">
              Instrument: hybrid persona-grounding rubric {data.instrument.hybrid_persona_grounding ? 'on' : 'off'} -
              judge input boundary {data.instrument.judge_input_boundary ? 'on' : 'off'} -
              cross-family judging {data.cross_family_judging ? 'enforced' : 'off'}.
            </p>
          )}
        </section>
      )}
    </div>
  )
}

interface ProviderSettings {
  ollama_enabled: boolean
  anthropic_enabled: boolean
  openai_enabled: boolean
  ollama_base_url: string
  anthropic_key_set: boolean
  openai_key_set: boolean
  default_model: string
  rag_similarity_threshold: number
}

function SettingsTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [settings, setSettings] = useState<ProviderSettings | null>(null)
  const [ollamaEnabled, setOllamaEnabled] = useState(false)
  const [anthropicEnabled, setAnthropicEnabled] = useState(false)
  const [openaiEnabled, setOpenaiEnabled] = useState(false)
  const [ollamaBase, setOllamaBase] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [openaiKey, setOpenaiKey] = useState('')
  const [defaultModel, setDefaultModel] = useState('')
  const [ragThreshold, setRagThreshold] = useState('0.40')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    fetch(`${api}/api/settings`, { headers: headers() })
      .then(r => r.json())
      .then((d: ProviderSettings) => {
        setSettings(d)
        setOllamaEnabled(d.ollama_enabled)
        setAnthropicEnabled(d.anthropic_enabled)
        setOpenaiEnabled(d.openai_enabled)
        setOllamaBase(d.ollama_base_url)
        setAnthropicKey('')
        setOpenaiKey('')
        setDefaultModel(d.default_model)
        setRagThreshold(String(d.rag_similarity_threshold))
      })
      .catch(() => setError('Failed to load settings'))
  }

  useEffect(() => { load() }, [])

  const save = async () => {
    setSaving(true); setSaved(false); setError('')
    try {
      const body: Record<string, unknown> = {
        ollama_enabled: ollamaEnabled,
        anthropic_enabled: anthropicEnabled,
        openai_enabled: openaiEnabled,
        ollama_base_url: ollamaBase,
        default_model: defaultModel,
        rag_similarity_threshold: parseFloat(ragThreshold) || 0.4,
      }
      if (anthropicKey.trim()) body.anthropic_api_key = anthropicKey.trim()
      if (openaiKey.trim()) body.openai_api_key = openaiKey.trim()

      const r = await fetch(`${api}/api/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(body),
      })
      if (!r.ok) { const d = await r.json(); setError(d.detail || 'Save failed'); return }
      const updated: ProviderSettings = await r.json()
      setSettings(updated)
      setAnthropicKey(''); setOpenaiKey('')
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch { setError('Request failed') }
    finally { setSaving(false) }
  }

  const testOllama = async () => {
    setTesting(true); setTestResult(null)
    try {
      const r = await fetch(`${api}/api/settings/test-ollama`, { headers: headers() })
      const d = await r.json()
      setTestResult(d.ok
        ? { ok: true, message: `Connected - ${d.model_count} model(s) available` }
        : { ok: false, message: d.error || 'Connection failed' })
    } catch { setTestResult({ ok: false, message: 'Request failed' }) }
    finally { setTesting(false) }
  }

  const ToggleRow = ({ label, desc, value, onChange }: { label: string; desc: string; value: boolean; onChange: (v: boolean) => void }) => (
    <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
      <div>
        <p className="text-sm text-gray-300">{label}</p>
        <p className="text-xs text-gray-500 mt-0.5">{desc}</p>
      </div>
      <button
        onClick={() => onChange(!value)}
        className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${value ? 'bg-blue-600' : 'bg-gray-600'}`}
      >
        <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${value ? 'left-5' : 'left-0.5'}`} />
      </button>
    </div>
  )

  if (!settings) return <p className="text-xs text-gray-500">{error || 'Loading…'}</p>

  return (
    <div className="max-w-xl space-y-8">

      {/* Providers */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Providers</p>
        <ToggleRow
          label="Ollama (local models)"
          desc="Route requests with no cloud prefix to your Ollama instance"
          value={ollamaEnabled}
          onChange={setOllamaEnabled}
        />
        <ToggleRow
          label="Anthropic (Claude)"
          desc="Enable claude-* models - requires API key"
          value={anthropicEnabled}
          onChange={setAnthropicEnabled}
        />
        <ToggleRow
          label="OpenAI (GPT / o-series)"
          desc="Enable gpt-* and o* models - requires API key"
          value={openaiEnabled}
          onChange={setOpenaiEnabled}
        />
      </section>

      {/* Ollama */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Ollama</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Base URL</label>
          <div className="flex gap-2">
            <input
              value={ollamaBase}
              onChange={e => { setOllamaBase(e.target.value); setTestResult(null) }}
              placeholder="http://host.docker.internal:11434"
              className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500 font-mono"
            />
            <button
              onClick={testOllama}
              disabled={testing}
              className="px-3 py-2 text-xs rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-50 transition-colors whitespace-nowrap"
            >
              {testing ? 'Testing…' : 'Test'}
            </button>
          </div>
          {testResult && (
            <p className={`text-xs mt-1.5 ${testResult.ok ? 'text-green-400' : 'text-red-400'}`}>
              {testResult.ok ? '✓' : '✕'} {testResult.message}
            </p>
          )}
          <p className="text-xs text-gray-600 mt-1">
            For cloud-to-local GPU routing: run <span className="font-mono bg-gray-800 px-1 rounded">cloudflared tunnel --url http://localhost:11434</span> and paste the tunnel URL here.
          </p>
        </div>
      </section>

      {/* Default model */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Default Model</p>
        <div>
          <input
            value={defaultModel}
            onChange={e => setDefaultModel(e.target.value)}
            placeholder="e.g. claude-sonnet-4-6 or qwen2.5-coder:32b"
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500 font-mono"
          />
          <p className="text-xs text-gray-600 mt-1">Used when no model is specified in the chat request.</p>
        </div>
      </section>

      {/* API Keys */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">API Keys</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Anthropic API Key
            {settings.anthropic_key_set && <span className="ml-2 text-green-500">● set</span>}
          </label>
          {/* new-password on both key fields: blank-means-keep, so a browser
              autofilling the operator's login here would silently OVERWRITE
              the real key on Save. */}
          <input
            type="password"
            autoComplete="new-password"
            value={anthropicKey}
            onChange={e => setAnthropicKey(e.target.value)}
            placeholder={settings.anthropic_key_set ? '●●●●●●●● (leave blank to keep current)' : 'sk-ant-…'}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            OpenAI API Key
            {settings.openai_key_set && <span className="ml-2 text-green-500">● set</span>}
          </label>
          <input
            type="password"
            autoComplete="new-password"
            value={openaiKey}
            onChange={e => setOpenaiKey(e.target.value)}
            placeholder={settings.openai_key_set ? '●●●●●●●● (leave blank to keep current)' : 'sk-…'}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500"
          />
        </div>
        <p className="text-xs text-gray-600">Keys are stored in the instance database. Leave blank to keep the current value.</p>
      </section>

      {/* RAG */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">RAG</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Similarity Threshold (0.0 - 1.0)</label>
          <input
            type="number"
            min="0" max="1" step="0.05"
            value={ragThreshold}
            onChange={e => setRagThreshold(e.target.value)}
            className="w-32 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-gray-500"
          />
          <p className="text-xs text-gray-600 mt-1">Chunks scoring below this value are excluded from context. Higher = stricter matching.</p>
        </div>
      </section>

      {/* Save */}
      <div className="flex items-center gap-3 pt-2 border-t border-gray-800">
        {error && <p className="text-xs text-red-400 flex-1">{error}</p>}
        {saved && <p className="text-xs text-green-400">Saved - changes take effect immediately.</p>}
        <button
          onClick={save}
          disabled={saving}
          className="ml-auto px-5 py-2 rounded-lg text-sm font-medium text-white disabled:opacity-50 transition-colors"
          style={{ backgroundColor: PRIMARY_COLOR }}
        >
          {saving ? 'Saving…' : 'Save Settings'}
        </button>
      </div>
    </div>
  )
}

// ── AdminPanel ─────────────────────────────────────────────────────────────

type Tab = 'trust' | 'users' | 'kb' | 'quarantine' | 'prompt' | 'models' | 'audit' | 'monitoring' | 'backup' | 'queue' | 'settings'

const TABS: { id: Tab; label: string }[] = [
  { id: 'trust', label: 'Trust' },
  { id: 'settings', label: 'Settings' },
  { id: 'users', label: 'Users' },
  { id: 'kb', label: 'Knowledge Base' },
  { id: 'quarantine', label: 'Quarantine' },
  { id: 'prompt', label: 'System Prompt' },
  { id: 'models', label: 'Models' },
  { id: 'audit', label: 'Audit Log' },
  { id: 'monitoring', label: 'Monitoring' },
  { id: 'backup', label: 'Backup' },
  { id: 'queue', label: 'Ingestion Queue' },
]

// Sidebar structure: tabs grouped by what the operator is doing. Trust stands
// alone at the top - it is the front door, and the measured numbers are the
// claim this platform makes about itself.
const TAB_GROUPS: { label: string | null; tabs: Tab[] }[] = [
  { label: null, tabs: ['trust'] },
  { label: 'Content', tabs: ['kb', 'quarantine', 'prompt', 'queue'] },
  { label: 'Ops', tabs: ['settings', 'models', 'monitoring', 'backup'] },
  { label: 'Security', tabs: ['users', 'audit'] },
]

export default function AdminPanel({ api, headers, currentUser, onClose, onLogout }: AdminPanelProps) {
  const groups = TAB_GROUPS.filter(g => g.tabs.length > 0)
  const tabLabel = (id: Tab) => TABS.find(t => t.id === id)?.label || id
  // The demo tour opens on Trust - the measured story is the show.
  const [tab, setTab] = useState<Tab>('trust')

  return (
    <div className="flex h-screen bg-gray-950 text-white overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col flex-shrink-0">
        <div className="p-5 border-b border-gray-800">
          <p className="text-sm font-semibold">Admin Panel</p>
          <p className="text-xs text-gray-500 mt-0.5">{currentUser.username}</p>
        </div>
        <nav className="flex-1 p-3 space-y-1 overflow-y-auto">
          {groups.map((g, gi) => (
            <div key={g.label || 'top'} className={gi > 0 ? 'pt-3' : ''}>
              {g.label && (
                <p className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-widest text-gray-600">
                  {g.label}
                </p>
              )}
              {g.tabs.map(id => (
                <button
                  key={id}
                  onClick={() => setTab(id)}
                  className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                    tab === id ? 'font-medium' : 'text-gray-400 hover:text-white hover:bg-gray-800'
                  }`}
                  style={tab === id ? { backgroundColor: `${PRIMARY_COLOR}22`, color: PRIMARY_COLOR } : {}}
                >
                  {tabLabel(id)}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="p-3 border-t border-gray-800 space-y-1">
          <button
            onClick={onClose}
            className="w-full text-left px-3 py-2 rounded-lg text-sm text-gray-400 hover:text-white hover:bg-gray-800 transition-colors"
          >
            ← Back to Chat
          </button>
          <button
            onClick={onLogout}
            className="w-full text-left px-3 py-2 rounded-lg text-sm text-red-500 hover:text-red-400 hover:bg-gray-800 transition-colors"
          >
            Sign out
          </button>
        </div>
      </aside>

      {/* Content */}
      <main className="flex-1 overflow-y-auto p-8">
        <h1 className="text-lg font-semibold mb-6 capitalize">
          {TABS.find(t => t.id === tab)?.label}
        </h1>
        <fieldset className="border-0 p-0 m-0 min-w-0">
        {tab === 'trust'      && <TrustTab api={api} headers={headers} />}
        {tab === 'settings'   && <SettingsTab api={api} headers={headers} />}
        {tab === 'users'      && <UsersTab api={api} headers={headers} />}
        {tab === 'kb'         && <KBManage api={api} headers={headers} />}
        {tab === 'quarantine' && <QuarantineTab api={api} headers={headers} currentUser={currentUser} />}
        {tab === 'prompt'     && <SystemPromptTab api={api} headers={headers} />}
        {tab === 'models'     && <ModelsTab api={api} headers={headers} />}
        {tab === 'audit'      && <AuditTab api={api} headers={headers} />}
        {tab === 'monitoring' && <MonitoringTab api={api} headers={headers} />}
        {tab === 'backup'     && <BackupTab api={api} headers={headers} />}
        {tab === 'queue'      && <IngestQueueTab api={api} headers={headers} />}
        </fieldset>
      </main>
    </div>
  )
}

// ── Quarantine review ───────────────────────────────────────────────────────

interface QuarantineItem {
  id: number
  source: string
  department: string
  trust_tier: string
  findings: { type?: string; pattern?: string }[]
  text_preview: string
  text_length: number
  status: string
  created_at: string
  reviewed_at: string | null
}

/**
 * The review queue for content the ingest gate WITHHELD.
 *
 * This exists because the platform blocks injection-shaped content at ingest
 * and holds it for a human. Without a surface for that, an operator learns
 * their document was withheld only from an upload response, and has no way to
 * read what tripped, release it, or discard it - the control would be visible
 * only in the moment it fired.
 *
 * Release is Owner-only on the backend, not because releasing is a big write,
 * but because it is a TRUST decision: the content stays tagged untrusted and
 * governed by the prompt rules, and only the withholding is waived. The button
 * is hidden for non-Owners rather than offered and refused.
 */
function QuarantineTab({ api, headers, currentUser }: {
  api: string
  headers: () => Record<string, string>
  currentUser: { role: string } | null
}) {
  const [items, setItems] = useState<QuarantineItem[]>([])
  const [status, setStatus] = useState<'held' | 'released' | 'deleted'>('held')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [loaded, setLoaded] = useState(false)
  const isOwner = currentUser?.role === 'owner'

  const load = () => {
    guardedJson<{ items?: QuarantineItem[] }>(
      fetch(`${api}/api/admin/kb/quarantine?status=${status}`, { headers: headers() }),
      'Loading quarantine')
      .then(d => { if (d) setItems(d.items || []); setLoaded(true) })
  }
  useEffect(load, [status])

  // Both actions go through actionError: a 403 on release (non-Owner) or a 404
  // on a row someone else already reviewed must SAY so. Reloading silently
  // would repaint the same list and read as "nothing happened".
  const act = async (id: number, verb: 'release' | 'delete') => {
    setBusy(id)
    const err = await actionError(
      verb === 'release'
        ? fetch(`${api}/api/admin/kb/quarantine/${id}/release`, { method: 'POST', headers: headers() })
        : fetch(`${api}/api/admin/kb/quarantine/${id}`, { method: 'DELETE', headers: headers() }),
      verb === 'release' ? 'Releasing document' : 'Discarding document')
    setBusy(null)
    if (!err) load()
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Quarantine</h2>
        <p className="text-sm text-gray-500 mt-1">
          Content the ingest gate withheld from the knowledge base. Releasing
          keeps the injection tag - the document stays labelled untrusted at
          retrieval - and waives only the withholding.
        </p>
      </div>

      <div className="flex gap-1 bg-gray-800/50 rounded-lg p-1 w-fit">
        {(['held', 'released', 'deleted'] as const).map(s => (
          <button key={s} onClick={() => setStatus(s)}
            className={`px-4 py-1.5 rounded-md text-xs font-medium transition-colors capitalize ${
              status === s ? 'bg-gray-700 text-white' : 'text-gray-500 hover:text-gray-300'
            }`}>
            {s}
          </button>
        ))}
      </div>

      {loaded && items.length === 0 && (
        <p className="text-sm text-gray-600 border border-gray-800 rounded-xl px-4 py-6 text-center">
          {status === 'held'
            ? 'Nothing is held for review. Withheld uploads appear here.'
            : `No ${status} items.`}
        </p>
      )}

      <div className="space-y-2">
        {items.map(it => (
          <div key={it.id} className="border border-gray-800 rounded-xl overflow-hidden">
            <div className="flex items-center gap-3 px-4 py-3 bg-gray-900/50">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-mono text-gray-200 truncate">{it.source}</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  {it.department} &middot; {it.trust_tier} &middot; {it.text_length} chars
                  {it.created_at && <> &middot; {it.created_at.slice(0, 16).replace('T', ' ')}</>}
                </p>
              </div>
              <div className="flex flex-wrap gap-1 justify-end">
                {(it.findings || []).map((f, i) => (
                  <span key={i} className="text-[11px] px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-300 border border-amber-600/30">
                    {f.type || 'match'}
                  </span>
                ))}
              </div>
              <button onClick={() => setExpanded(expanded === it.id ? null : it.id)}
                className="text-xs text-gray-400 hover:text-white px-2">
                {expanded === it.id ? 'Hide' : 'View'}
              </button>
            </div>

            {expanded === it.id && (
              <div className="border-t border-gray-800">
                <pre className="p-4 max-h-72 overflow-auto text-xs text-gray-300 whitespace-pre-wrap leading-relaxed bg-gray-950/50">
                  {it.text_preview}
                  {it.text_length > it.text_preview.length && (
                    `\n\n[preview truncated - ${it.text_length} characters total]`
                  )}
                </pre>
                {it.status === 'held' && (
                  <div className="flex items-center gap-2 px-4 py-3 border-t border-gray-800">
                    {isOwner ? (
                      <button disabled={busy === it.id} onClick={() => act(it.id, 'release')}
                        className="text-xs px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 transition-colors">
                        {busy === it.id ? 'Working...' : 'Release into the knowledge base'}
                      </button>
                    ) : (
                      <p className="text-xs text-gray-600">
                        Releasing is an Owner decision. You can discard it.
                      </p>
                    )}
                    <button disabled={busy === it.id} onClick={() => act(it.id, 'delete')}
                      className="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white hover:border-gray-500 disabled:opacity-40 transition-colors">
                      Discard
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
