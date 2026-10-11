import { useState, useEffect } from 'react'
import { actionError, guardedJson } from './errorSurface'
import { PRIMARY_COLOR } from './theme'
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
import { SettingsTab } from './admin/SettingsTab'

// ── Types ──────────────────────────────────────────────────────────────────

// ── Props ──────────────────────────────────────────────────────────────────

interface AdminPanelProps {
  api: string
  headers: () => Record<string, string>
  currentUser: { id: number; username: string; role: string }
  onClose: () => void
  onLogout: () => void
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
