/**
 * AN EDIT TYPED BEFORE THE FIELD SETTLES (2026-10-31).
 *
 * The department field kept a draft and reset it to the stored value from an
 * effect - including the effect React runs once when the field first mounts.
 * When the render that mounts the field runs past React's 5 ms slice, as on a
 * loaded CI runner, that mount effect waits for a later task, and an edit made
 * in between was overwritten by it: the draft went back to the stored value,
 * the blur saw nothing changed, and nothing was sent.
 *
 * Reproduced here on purpose: three hundred rows make the render long, and a
 * MutationObserver types into the first field as soon as it is in the page -
 * its callback runs before the task that holds the mount effects.
 *
 * FIX: the draft is reset only when the stored value changes after mount.
 */
import { render, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { UsersTab } from '../admin/UsersTab'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = globalThis.fetch

const MEMBERS = Array.from({ length: 300 }, (_, i) => ({
  id: i + 1, username: `member${i + 1}`, role: 'member', department: 'general', permissions: [],
  is_active: 1, created_at: '2026-10-31T00:00:00', mfa_enabled: 0, failed_attempts: 0,
  locked_until: null,
}))

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

function install() {
  const writes: { url: string; body: unknown }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method || 'GET').toUpperCase()
    if (method === 'PATCH' && url.includes('/department')) {
      writes.push({ url, body: JSON.parse(String(init?.body)) })
      return json({ status: 'updated' })
    }
    if (url.includes('/api/users')) return json({ users: MEMBERS })
    if (url.includes('/api/ingest/departments')) return json({ departments: ['general', 'hr'] })
    if (url.includes('/api/admin/permissions')) return json({ scopes: [], presets: {} })
    return json({})
  }) as unknown as typeof fetch
  return writes
}

describe('Users tab: the department field as it mounts', () => {
  it('FIX: an edit typed the moment the field appears still writes', async () => {
    const writes = install()
    const container = document.body.appendChild(document.createElement('div'))
    const typed = new Promise<void>(resolve => {
      const observer = new MutationObserver(() => {
        const field = container.querySelector('input[title="Department"]')
        if (!field) return
        observer.disconnect()
        fireEvent.change(field, { target: { value: 'hr' } })
        fireEvent.blur(field)
        resolve()
      })
      observer.observe(container, { childList: true, subtree: true })
    })
    render(<UsersTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />, { container })
    await typed
    await vi.waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0]).toEqual({ url: '/api/users/1/department', body: { department: 'hr' } })
  })
})
