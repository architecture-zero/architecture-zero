/**
 * THE ADMIN PANEL LISTS A TAB ONLY TO AN ACCOUNT ITS ROUTES ALLOW (2026-10-30).
 *
 * The sidebar listed all thirteen tabs to anyone who could open the panel.
 * The admin preset holds manage_kb, manage_users and view_analytics, and only
 * the Owner bypasses a scope, so an admin opened eight tabs whose loads answer
 * 403 ("Loading ... failed" toasts and empty forms). Each tab now names the
 * scope its load routes take - or OWNER for the require_owner tabs (Settings,
 * Monitoring, Backup) - and the sidebar lists it only to an account that
 * holds it, as the server's check_permission decides: the Owner, or an
 * account whose resolved permissions include the scope.
 *
 * view_audit_log is granted on its own, never in a preset but the Owner's, and
 * did not open the panel at all: the header's Admin button now counts it, so
 * that account reaches the one tab it was granted.
 */
import { render, screen, within, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import AdminPanel from '../AdminPanel'
import { ChatHeader } from '../chat/ChatHeader'
import type { AuthUser } from '../chat/types'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = globalThis.fetch
let fetched: string[] = []

beforeEach(() => {
  fetched = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    fetched.push(String(input))
    return json({})
  }) as unknown as typeof fetch
})

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

const ADMIN_PRESET = ['chat', 'view_history', 'manage_kb', 'manage_users', 'view_analytics']
const EVERY_SCOPE = [...ADMIN_PRESET, 'view_audit_log', 'manage_system']

function openPanel(user: AuthUser) {
  render(<AdminPanel api="" headers={() => ({})} currentUser={user} onClose={() => {}} onLogout={() => {}} />)
  return within(screen.getByRole('navigation')).getAllByRole('button').map(b => b.textContent)
}

function headerOffersAdmin(user: AuthUser) {
  render(
    <ChatHeader
      sidebarOpen={false} setSidebarOpen={() => {}} helpEnabled={false} helpMode={false}
      enterHelp={() => {}} leaveHelp={() => {}} instanceName="Test" answeringModel=""
      currentUser={user} setView={() => {}} isGuest={false} setProfileOpen={() => {}}
      handleLogout={() => {}}
    />)
  return screen.queryByRole('button', { name: 'Admin' }) !== null
}

describe('admin panel: a tab is listed only to an account its routes allow', () => {
  it('an admin-preset account sees only the tabs its scopes load', () => {
    const tabs = openPanel({ id: 2, username: 'ops', role: 'admin', permissions: ADMIN_PRESET })
    expect(tabs).toEqual(['Trust', 'Knowledge Base', 'Quarantine', 'Ingestion Queue', 'Users'])
    // It opens on Trust, which it may load.
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Trust')
    expect(fetched.every(u => u.includes('/api/admin/trust'))).toBe(true)
  })

  it('a view_audit_log-only account can open the panel, to Audit Log', () => {
    const auditor: AuthUser = { id: 3, username: 'auditor', role: 'member', permissions: ['chat', 'view_history', 'view_audit_log'] }
    expect(headerOffersAdmin(auditor)).toBe(true)
    cleanup()
    const tabs = openPanel(auditor)
    expect(tabs).toEqual(['Audit Log'])
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Audit Log')
    expect(fetched.length).toBeGreaterThan(0)
    expect(fetched.every(u => u.includes('/api/admin/audit'))).toBe(true)
  })

  it('CONTROL: the owner sees every tab', () => {
    const tabs = openPanel({ id: 1, username: 'owner', role: 'owner', permissions: EVERY_SCOPE })
    expect(tabs).toEqual([
      'Trust',
      'Knowledge Base', 'Quarantine', 'System Prompt', 'Ingestion Queue',
      'Settings', 'Models', 'Chat Controls', 'Monitoring', 'Backup',
      'Users', 'Guest Access', 'Audit Log',
    ])
  })

  it('CONTROL: an account holding no operator scope is not offered the panel', () => {
    expect(headerOffersAdmin({ id: 4, username: 'reader', role: 'member', permissions: ['chat', 'view_history'] })).toBe(false)
  })
})
