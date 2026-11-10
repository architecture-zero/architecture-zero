/**
 * THE USERS TAB SHOWS A SECOND FACTOR THAT CANNOT BE READ (2026-11-10).
 *
 * An account's two-factor seed is stored encrypted under the instance's
 * JWT_SECRET_KEY. After a restore under a different secret, or a rotation
 * without the re-key, the seed cannot be decrypted, and the server refuses
 * every sign-in by that account rather than downgrade it to password-only.
 * The users list has reported the state since then (mfa_secret_unreadable),
 * and no screen showed it: the only place a person met it was the refusal at
 * that account's own sign-in, and the roster said "MFA" in green.
 *
 * FIX: the row of such an account carries an "MFA unreadable" badge in place
 * of the green one, and one line saying what it means and the two ways out.
 * CONTROL: a readable second factor still shows the green badge and no line;
 * an account that never finished enrolling shows neither, whatever its seed
 * reads as (the server's rule has both halves: enabled AND unreadable); and a
 * list from a server that does not report the field renders as before.
 */
import { render, screen, cleanup, within } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { UsersTab } from '../admin/UsersTab'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = globalThis.fetch

const base = {
  role: 'member', department: 'general', permissions: [], is_active: 1,
  created_at: '2026-11-10T00:00:00', failed_attempts: 0, locked_until: null,
}
const STRANDED = { ...base, id: 5, username: 'pat', mfa_enabled: 1, mfa_secret_unreadable: true }
const HEALTHY = { ...base, id: 6, username: 'sam', mfa_enabled: 1, mfa_secret_unreadable: false }
const UNENROLLED = { ...base, id: 7, username: 'lee', mfa_enabled: 0, mfa_secret_unreadable: true }
const OLDER_SERVER = { ...base, id: 8, username: 'kim', mfa_enabled: 1 }

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

function install(users: unknown[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/users')) return json({ users })
    if (url.includes('/api/ingest/departments')) return json({ departments: ['general'] })
    if (url.includes('/api/admin/permissions')) return json({ scopes: [], presets: {} })
    return json({})
  }) as unknown as typeof fetch
}

const show = (users: unknown[]) => {
  install(users)
  render(<UsersTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
}

// The row of one account: the card that holds its name.
const rowOf = async (name: string) => {
  const label = await screen.findByText(name)
  return label.closest('div.overflow-hidden') as HTMLElement
}

describe('Users tab: a second factor that cannot be read', () => {
  it('marks the account and says what it means and how to fix it', async () => {
    show([STRANDED, HEALTHY])
    const row = within(await rowOf('pat'))
    expect(row.getByText('MFA unreadable')).toBeInTheDocument()
    expect(row.queryByText('MFA')).toBeNull()
    const line = row.getByText(/stored seed cannot be read/)
    expect(line.textContent).toMatch(/sign-in.*refused/)
    expect(line.textContent).toMatch(/Reset MFA/)
    expect(line.textContent).toMatch(/rekey_at_rest\.py/)
    // The way out the roster already offers is still on the row.
    expect(row.getByRole('button', { name: 'Reset MFA' })).toBeInTheDocument()
  })

  it('leaves a readable second factor as it was', async () => {
    show([STRANDED, HEALTHY])
    const row = within(await rowOf('sam'))
    expect(row.getByText('MFA')).toBeInTheDocument()
    expect(row.queryByText('MFA unreadable')).toBeNull()
    expect(row.queryByText(/stored seed cannot be read/)).toBeNull()
  })

  it('says nothing for an account that never finished enrolling', async () => {
    show([UNENROLLED])
    const row = within(await rowOf('lee'))
    expect(row.queryByText('MFA unreadable')).toBeNull()
    expect(row.queryByText('MFA')).toBeNull()
    expect(row.queryByText(/stored seed cannot be read/)).toBeNull()
  })

  it('renders a list without the field as before', async () => {
    show([OLDER_SERVER])
    const row = within(await rowOf('kim'))
    expect(row.getByText('MFA')).toBeInTheDocument()
    expect(row.queryByText('MFA unreadable')).toBeNull()
  })
})
