// The admin shell.
//
// After the tab split (2026-10-10) this file lays out the admin surface and
// nothing else: the sidebar (the tabs and their groups) and which tab
// component renders for which id. Every tab lives in its own file under
// src/admin/, with the shapes it alone reads; what more than one tab shares
// is there too (stepUp.tsx, settingsControls.tsx) and the accent color is
// src/theme.ts. The backend's routers/ shape (app/main.py assembles, the
// routers hold the routes), carried to the client.
//
// Direction is one-way and enforced by __tests__/adminHygiene.test.ts: this
// file imports the tabs; nothing under src/admin/ imports it or another tab.
import { useState } from 'react'
import { PRIMARY_COLOR } from './theme'
// The tabs, in the sidebar's order (TABS below).
import { TrustTab } from './admin/TrustTab'
import { SettingsTab } from './admin/SettingsTab'
import { UsersTab } from './admin/UsersTab'
import { KBManage } from './admin/KBManage'
import { QuarantineTab } from './admin/QuarantineTab'
import { SystemPromptTab } from './admin/SystemPromptTab'
import { ChatControlsTab } from './admin/ChatControlsTab'
import { GuestAccessTab } from './admin/GuestAccessTab'
import { ModelsTab } from './admin/ModelsTab'
import { AuditTab } from './admin/AuditTab'
import { MonitoringTab } from './admin/MonitoringTab'
import { BackupTab } from './admin/BackupTab'
import { IngestQueueTab } from './admin/IngestQueueTab'

interface AdminPanelProps {
  api: string
  headers: () => Record<string, string>
  // `permissions` is what /api/auth/me resolves; the login payload has none,
  // so it can be absent until that read lands.
  currentUser: { id: number; username: string; role: string; permissions?: string[] }
  onClose: () => void
  onLogout: () => void
}

// ── AdminPanel ─────────────────────────────────────────────────────────────

type Tab = 'trust' | 'users' | 'kb' | 'quarantine' | 'prompt' | 'chat' | 'guest' | 'models' | 'audit' | 'monitoring' | 'backup' | 'queue' | 'settings'

// `needs`: the scope a tab's LOAD routes take, read at the routes, so the
// sidebar lists a tab only to an account the server lets through
// (2026-10-30). It listed every tab to anyone who could open the panel, and
// the admin preset holds manage_kb, manage_users and view_analytics only, so
// an admin opened eight tabs whose loads answer 403. OWNER marks a tab whose
// routes sit behind require_owner, which no scope reaches.
const OWNER = 'owner'
const TABS: { id: Tab; label: string; needs: string }[] = [
  { id: 'trust', label: 'Trust', needs: 'view_analytics' },
  { id: 'settings', label: 'Settings', needs: OWNER },
  { id: 'users', label: 'Users', needs: 'manage_users' },
  { id: 'kb', label: 'Knowledge Base', needs: 'manage_kb' },
  { id: 'quarantine', label: 'Quarantine', needs: 'manage_kb' },
  { id: 'prompt', label: 'System Prompt', needs: 'manage_system' },
  { id: 'chat', label: 'Chat Controls', needs: 'manage_system' },
  { id: 'guest', label: 'Guest Access', needs: 'manage_system' },
  { id: 'models', label: 'Models', needs: 'manage_system' },
  { id: 'audit', label: 'Audit Log', needs: 'view_audit_log' },
  { id: 'monitoring', label: 'Monitoring', needs: OWNER },
  { id: 'backup', label: 'Backup', needs: OWNER },
  { id: 'queue', label: 'Ingestion Queue', needs: 'manage_kb' },
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
  // The server's check_permission: the Owner passes everything; anyone else,
  // admin included, by the resolved permissions alone.
  const holds = (scope: string) =>
    currentUser.role === 'owner' || (scope !== OWNER && (currentUser.permissions || []).includes(scope))
  const shown = new Set(TABS.filter(t => holds(t.needs)).map(t => t.id))
  const groups = TAB_GROUPS
    .map(g => ({ ...g, tabs: g.tabs.filter(id => shown.has(id)) }))
    .filter(g => g.tabs.length > 0)
  const tabLabel = (id: Tab) => TABS.find(t => t.id === id)?.label || id
  // The demo tour opens on Trust - the measured story is the show - for an
  // account that may load it; anyone else opens on the first tab listed to
  // them, never on one the server refuses.
  const [tab, setTab] = useState<Tab | null>(() =>
    shown.has('trust') ? 'trust' : (groups[0]?.tabs[0] ?? null))

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
