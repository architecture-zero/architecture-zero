/**
 * THE USERS TAB'S DEPARTMENT FIELD (2026-10-31).
 *
 * Moving an account between departments is the Owner's alone on the server
 * (PATCH /api/users/{id}/department refuses everyone else), but every row
 * showed the field to an Admin too - an edit the server refuses, offered.
 * And the field wrote on every keystroke: typing "hr" sent "h", then "hr",
 * each a PATCH, and the first one moved the account into a department "h".
 *
 * FIX: below the Owner the row has no department field (the department is
 * still shown as text); the Owner's field writes once, when the edit is
 * committed - on blur or Enter - and not when nothing changed.
 */
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { UsersTab } from '../admin/UsersTab'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = globalThis.fetch

const MEMBER = {
  id: 5, username: 'pat', role: 'member', department: 'general', permissions: [],
  is_active: 1, created_at: '2026-10-31T00:00:00', mfa_enabled: 0, failed_attempts: 0,
  locked_until: null,
}

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

function install(refuse = false) {
  const writes: { url: string; body: unknown }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method || 'GET').toUpperCase()
    if (method === 'PATCH' && url.includes('/department')) {
      writes.push({ url, body: JSON.parse(String(init?.body)) })
      return refuse ? json({ detail: 'Refused' }, 403) : json({ status: 'updated' })
    }
    if (url.includes('/api/users')) return json({ users: [MEMBER] })
    if (url.includes('/api/ingest/departments')) return json({ departments: ['general', 'hr'] })
    if (url.includes('/api/admin/permissions')) return json({ scopes: [], presets: {} })
    return json({})
  }) as unknown as typeof fetch
  return writes
}

describe('Users tab: the department field', () => {
  it('is not offered below the Owner; the department still shows', async () => {
    install()
    render(<UsersTab api="" headers={() => ({})} currentUser={{ role: 'admin' }} />)
    expect(await screen.findByText('pat')).toBeInTheDocument()
    expect(screen.queryByTitle('Department')).toBeNull()
    expect(screen.getByText(/2026-10-31 · general/)).toBeInTheDocument()
  })

  it("writes the Owner's edit once, on blur, not per keystroke", async () => {
    const writes = install()
    render(<UsersTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
    const field = await screen.findByTitle('Department')
    fireEvent.change(field, { target: { value: 'h' } })
    fireEvent.change(field, { target: { value: 'hr' } })
    expect(writes).toHaveLength(0)
    fireEvent.blur(field)
    await vi.waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body).toEqual({ department: 'hr' })
  })

  it('commits on Enter, and an unchanged field writes nothing', async () => {
    const writes = install()
    render(<UsersTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
    const field = await screen.findByTitle('Department')
    fireEvent.blur(field)
    fireEvent.change(field, { target: { value: 'hr' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await vi.waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body).toEqual({ department: 'hr' })
    // The blur after Enter does not send it again.
    fireEvent.blur(field)
    expect(writes).toHaveLength(1)
  })

  it('CONTROL: a refused write shows the stored department again', async () => {
    const writes = install(true)
    render(<UsersTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
    const field = await screen.findByTitle('Department') as HTMLInputElement
    fireEvent.change(field, { target: { value: 'hr' } })
    fireEvent.blur(field)
    await vi.waitFor(() => expect(writes).toHaveLength(1))
    await vi.waitFor(() => expect(field.value).toBe('general'))
  })
})
