import { useState, useEffect } from 'react'
import { actionError, guardedJson } from './errorSurface'
import { PRIMARY_COLOR } from './theme'
import { useStepUp } from './admin/stepUp'
import { DefaultModelPointer, SaveStatus } from './admin/settingsControls'
import { UsersTab } from './admin/UsersTab'
import { KBManage } from './admin/KBManage'
import { SystemPromptTab } from './admin/SystemPromptTab'
import { ChatControlsTab } from './admin/ChatControlsTab'
import { GuestAccessTab } from './admin/GuestAccessTab'
import { ModelsTab } from './admin/ModelsTab'
import { AuditTab } from './admin/AuditTab'
import { MonitoringTab } from './admin/MonitoringTab'
import { BackupTab } from './admin/BackupTab'
import { IngestQueueTab } from './admin/IngestQueueTab'
import { TrustTab } from './admin/TrustTab'

// ── Types ──────────────────────────────────────────────────────────────────

// ── Props ──────────────────────────────────────────────────────────────────

interface AdminPanelProps {
  api: string
  headers: () => Record<string, string>
  currentUser: { id: number; username: string; role: string }
  onClose: () => void
  onLogout: () => void
}

// ── Tab: Settings ──────────────────────────────────────────────────────────

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

export function SettingsTab({ api, headers, goToModels }: {
  api: string; headers: () => Record<string, string>; goToModels?: () => void
}) {
  const stepUp = useStepUp()
  const [settings, setSettings] = useState<ProviderSettings | null>(null)
  const [ollamaEnabled, setOllamaEnabled] = useState(false)
  const [anthropicEnabled, setAnthropicEnabled] = useState(false)
  const [openaiEnabled, setOpenaiEnabled] = useState(false)
  const [ollamaBase, setOllamaBase] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [openaiKey, setOpenaiKey] = useState('')
  const [ragThreshold, setRagThreshold] = useState('0.40')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    // Same class as SystemPromptTab. This route is require_owner while the
    // panel opens for several lesser permissions, so a non-owner admin gets a
    // 403 - which unguarded renders as a confident, fully-drawn form reporting
    // every provider disabled on an instance where they may all be live.
    guardedJson<ProviderSettings>(
      fetch(`${api}/api/settings`, { headers: headers() }), 'Loading provider settings')
      .then(d => {
        if (!d) {
          // The guard already raised a toast; this is what the tab BODY says.
          // Without it the render guard below sits on "Loading..." forever,
          // which is a quieter version of the dishonesty this call was fixed
          // for.
          setError('Could not load provider settings - this section is Owner-only.')
          return
        }
        setError('')
        setSettings(d)
        setOllamaEnabled(d.ollama_enabled)
        setAnthropicEnabled(d.anthropic_enabled)
        setOpenaiEnabled(d.openai_enabled)
        setOllamaBase(d.ollama_base_url)
        setAnthropicKey('')
        setOpenaiKey('')
        setRagThreshold(String(d.rag_similarity_threshold))
      })
      .catch(() => setError('Failed to load settings'))
  }

  useEffect(() => { load() }, [])

  const save = async () => {
    setSaving(true); setSaved(false); setError('')
    // REFUSE HERE RATHER THAN SEND SOMETHING ELSE. Omitting the key makes the
    // server skip the write and still answer 200, which is the same
    // silently-discarded-but-reported-saved defect being fixed on that end.
    const parsedThreshold = parseFloat(ragThreshold)
    if (!Number.isFinite(parsedThreshold) || parsedThreshold < 0 || parsedThreshold > 1) {
      setError('Similarity threshold must be a number between 0 and 1.')
      setSaving(false)
      return
    }
    try {
      const body: Record<string, unknown> = {
        ollama_enabled: ollamaEnabled,
        anthropic_enabled: anthropicEnabled,
        openai_enabled: openaiEnabled,
        ollama_base_url: ollamaBase,
        // No default_model: Models owns it (2026-10-03), and the server
        // leaves an omitted key as it is.
        // NOT `parseFloat(x) || 0.4`. Zero is a legitimate threshold, the input
        // advertises min="0", and `0 || 0.4` is 0.4 - so typing 0 silently sent
        // 0.4 while the field went on displaying 0. Validated above instead.
        rag_similarity_threshold: parsedThreshold,
      }
      if (anthropicKey.trim()) body.anthropic_api_key = anthropicKey.trim()
      if (openaiKey.trim()) body.openai_api_key = openaiKey.trim()
      // Step-up (2026-09-21): this route holds the egress address and every
      // provider key, so saving costs the caller's own password. Cancel = no save.
      const pw = await stepUp.ask('change provider settings')
      if (pw === null) return
      body.current_password = pw

      const r = await fetch(`${api}/api/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(body),
      })
      if (!r.ok) { const d = await r.json(); setError(d.detail || 'Save failed'); return }
      const updated: ProviderSettings = await r.json()
      setSettings(updated)
      setOllamaEnabled(updated.ollama_enabled)
      setAnthropicEnabled(updated.anthropic_enabled)
      setOpenaiEnabled(updated.openai_enabled)
      setOllamaBase(updated.ollama_base_url)
      // REHYDRATE FROM THE RESPONSE. The input kept displaying whatever was
      // typed, so a value the server did not store still read back as the
      // current setting under a green "Saved" banner. What the server returns
      // is what the server has.
      setRagThreshold(String(updated.rag_similarity_threshold))
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

  // Lit only while the form differs from what the server last returned. A
  // typed key always counts: a blank key box means "keep the current one".
  const dirty = !!settings && (
    ollamaEnabled !== settings.ollama_enabled ||
    anthropicEnabled !== settings.anthropic_enabled ||
    openaiEnabled !== settings.openai_enabled ||
    ollamaBase !== settings.ollama_base_url ||
    anthropicKey.trim() !== '' || openaiKey.trim() !== '' ||
    parseFloat(ragThreshold) !== settings.rag_similarity_threshold
  )

  if (!settings) return <p className="text-xs text-gray-500">{error || 'Loading…'}</p>

  return (
    <div className="max-w-xl space-y-8">
      {stepUp.prompt}

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

      {/* Default model - shown here, set in Models */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Default Model</p>
        <DefaultModelPointer model={settings.default_model} goToModels={goToModels} />
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
        <div className="ml-auto flex items-center gap-3">
          <SaveStatus dirty={dirty} saved={saved} savedText="Saved - changes take effect immediately." />
          <button
            onClick={save}
            disabled={!dirty || saving}
            className="px-5 py-2 rounded-lg text-sm font-medium text-white disabled:opacity-40 transition-colors"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            {saving ? 'Saving…' : 'Save Settings'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── AdminPanel ─────────────────────────────────────────────────────────────

type Tab = 'trust' | 'users' | 'kb' | 'quarantine' | 'prompt' | 'chat' | 'guest' | 'models' | 'audit' | 'monitoring' | 'backup' | 'queue' | 'settings'

const TABS: { id: Tab; label: string }[] = [
  { id: 'trust', label: 'Trust' },
  { id: 'settings', label: 'Settings' },
  { id: 'users', label: 'Users' },
  { id: 'kb', label: 'Knowledge Base' },
  { id: 'quarantine', label: 'Quarantine' },
  { id: 'prompt', label: 'System Prompt' },
  { id: 'chat', label: 'Chat Controls' },
  { id: 'guest', label: 'Guest Access' },
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
  { label: 'Ops', tabs: ['settings', 'models', 'chat', 'monitoring', 'backup'] },
  { label: 'Security', tabs: ['users', 'guest', 'audit'] },
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
        {tab === 'trust'      && <TrustTab api={api} headers={headers} currentUser={currentUser} />}
        {tab === 'settings'   && <SettingsTab api={api} headers={headers} goToModels={() => setTab('models')} />}
        {tab === 'users'      && <UsersTab api={api} headers={headers} />}
        {tab === 'kb'         && <KBManage api={api} headers={headers} />}
        {tab === 'quarantine' && <QuarantineTab api={api} headers={headers} currentUser={currentUser} />}
        {tab === 'prompt'     && <SystemPromptTab api={api} headers={headers} />}
        {tab === 'chat'       && <ChatControlsTab api={api} headers={headers} goToModels={() => setTab('models')} />}
        {tab === 'guest'      && <GuestAccessTab api={api} headers={headers} />}
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
