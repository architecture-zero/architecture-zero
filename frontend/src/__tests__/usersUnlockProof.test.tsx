/**
 * AN UNLOCK ASKS FOR PROOF.
 *
 * The server unlocks an account only on proof: for someone else's account the
 * operator's own password, for the operator's own account a current
 * authenticator code. The Users tab's Unlock button sent the request the
 * moment it was clicked, with nothing in it. It asks first now, through the
 * prompt the tab's other confirmed writes use, and sends what was typed;
 * Cancel sends nothing. On the operator's own row with no authenticator
 * enrolled there is no proof to ask for: the request goes as it is, and the
 * server's refusal - how long the lock has left, and whom to ask - is shown.
 *
 * FIX: nothing is sent before the proof is confirmed; another account's row
 * asks for the password and sends it; the operator's own row asks for the
 * code and sends it; Cancel sends nothing.
 * PIN (through the prompt, so not runnable on the button as it was): a
 * refusal is shown; an accepted unlock shows no failure and re-reads the list.
 *
 * And the Locked badge and the Unlock button are drawn only while the lock
 * stands: locked_until stays on the row after its time, and a click then
 * asked for proof to clear a lock that had already ended.
 */
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { UsersTab } from '../admin/UsersTab'
import { ErrorSurface } from '../errorSurface'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const ME = 1
const account = (id: number, username: string, mfa: number) => ({
  id, username, role: 'admin', department: 'general', permissions: [],
  is_active: 1, created_at: '2026-01-01', mfa_enabled: mfa, failed_attempts: 5,
  locked_until: '2099-01-01T00:00:00+00:00',
})

interface Sent { url: string; body: unknown; contentType: string | null }
interface Calls { unlocks: Sent[]; deletes: string[]; userReads: number }

function installFetch(users: unknown[], answer: () => Response): Calls {
  const calls: Calls = { unlocks: [], deletes: [], userReads: 0 }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method || 'GET').toUpperCase()
    if (method === 'POST' && url.endsWith('/unlock')) {
      calls.unlocks.push({
        url,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        contentType: new Headers(init?.headers).get('Content-Type'),
      })
      return answer()
    }
    if (method === 'DELETE') {
      calls.deletes.push(url)
      return answer()
    }
    if (url.endsWith('/api/users')) {
      calls.userReads += 1
      return json({ users })
    }
    if (url.endsWith('/api/admin/permissions')) return json({ scopes: [], presets: {} })
    if (url.endsWith('/api/ingest/departments')) return json({ departments: ['general'] })
    return json({})
  }))
  return calls
}

const show = () => render(<><ErrorSurface /><UsersTab api="" headers={() => ({})} currentUser={{ role: 'owner', id: ME }} /></>)

const confirmWith = async (placeholder: string, typed: string) => {
  fireEvent.change(await screen.findByPlaceholderText(placeholder), { target: { value: typed } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Users tab: the Unlock button asks for the proof', () => {
  it("FIX: another account's row asks for the operator's password and sends it", async () => {
    const calls = installFetch([account(2, 'pat', 0)], () => json({ status: 'unlocked' }))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Unlock' }))
    expect(await screen.findByText('Confirm with your password')).toBeInTheDocument()
    expect(screen.getByText(/To unlock pat, enter the password/)).toBeInTheDocument()
    expect(calls.unlocks).toEqual([])
    await confirmWith('Your password', 'my-own-password')
    await waitFor(() => expect(calls.unlocks).toHaveLength(1))
    expect(calls.unlocks[0]).toEqual({
      url: '/api/admin/users/2/unlock',
      body: { current_password: 'my-own-password' },
      contentType: 'application/json',
    })
  })

  it("FIX: the operator's own row asks for an authenticator code and sends it", async () => {
    const calls = installFetch([account(ME, 'me', 1)], () => json({ status: 'unlocked' }))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Unlock' }))
    expect(await screen.findByText('Confirm with your authenticator code')).toBeInTheDocument()
    expect(screen.getByText(/To unlock your own account, enter the current code/)).toBeInTheDocument()
    expect(screen.queryByPlaceholderText('Your password')).toBeNull()
    expect(calls.unlocks).toEqual([])
    await confirmWith('Authenticator code', '123456')
    await waitFor(() => expect(calls.unlocks).toHaveLength(1))
    expect(calls.unlocks[0]).toEqual({
      url: `/api/admin/users/${ME}/unlock`,
      body: { mfa_code: '123456' },
      contentType: 'application/json',
    })
  })

  it('FIX: Cancel sends nothing', async () => {
    const calls = installFetch([account(2, 'pat', 0)], () => json({ status: 'unlocked' }))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Unlock' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByText('Confirm with your password')).toBeNull())
    expect(calls.unlocks).toEqual([])
  })

  it("FIX: the operator's own row with no authenticator asks for nothing and shows the server's refusal", async () => {
    const refusal = 'Unlocking your own account takes a current authenticator code, and your '
      + 'account has no authenticator enrolled. Wait 12 minute(s) for the lock to end, or '
      + 'ask another Owner to unlock it.'
    const calls = installFetch([account(ME, 'me', 0)], () => json({ detail: refusal }, 400))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Unlock' }))
    expect(await screen.findByText(`Unlocking account failed: ${refusal}`)).toBeInTheDocument()
    expect(screen.queryByText(/Confirm with your/)).toBeNull()
    expect(calls.unlocks).toEqual([{
      url: `/api/admin/users/${ME}/unlock`, body: {}, contentType: 'application/json',
    }])
  })

  it('PIN: a refused unlock shows the server detail', async () => {
    const calls = installFetch([account(2, 'pat', 0)],
      () => json({ detail: 'Current password is incorrect' }, 400))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Unlock' }))
    await confirmWith('Your password', 'a-typo')
    expect(await screen.findByText('Unlocking account failed: Current password is incorrect')).toBeInTheDocument()
    expect(calls.unlocks).toHaveLength(1)
  })

  it('PIN: an accepted unlock shows no failure and re-reads the list', async () => {
    const calls = installFetch([account(2, 'pat', 0)], () => json({ status: 'unlocked' }))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Unlock' }))
    await confirmWith('Your password', 'my-own-password')
    await waitFor(() => expect(calls.userReads).toBeGreaterThanOrEqual(2))
    expect(calls.unlocks).toHaveLength(1)
    expect(screen.queryByText(/Unlocking account failed/)).toBeNull()
  })
})

describe('Users tab: a lock is shown only while it stands', () => {
  it('FIX: a lock whose time has passed draws no Locked badge and no Unlock button', async () => {
    installFetch([{ ...account(2, 'pat', 0), locked_until: '2000-01-01T00:00:00+00:00' }],
      () => json({ status: 'unlocked' }))
    show()
    expect(await screen.findByText('pat')).toBeInTheDocument()
    expect(screen.queryByText('Locked')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Unlock' })).toBeNull()
  })

  it('CONTROL: a lock that stands draws both', async () => {
    installFetch([account(2, 'pat', 0)], () => json({ status: 'unlocked' }))
    show()
    expect(await screen.findByText('Locked')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unlock' })).toBeInTheDocument()
  })
})
