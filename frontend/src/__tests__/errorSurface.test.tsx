import { render, screen, act, cleanup } from '@testing-library/react'
import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { ErrorSurface, guardedPoll, guardedJson, actionError } from '../errorSurface'

// The sticky "Session expired" banner means a session was LOST. A 401 with no
// token in storage is "not signed in" and must leave the banner down; a 401
// with a token in storage is a session that really expired and must raise it.
// This template never reaches chat without a token or the guest flag, so the
// rule is defense here; on the public demo fork, whose authless path 401s its
// polls by design, its absence put the banner over every first visit
// (2026-09-11). Pinned through each of the three guarded helpers.
const unauthorized = () =>
  Promise.resolve({ ok: false, status: 401, json: async () => ({ detail: 'Not authenticated' }) } as unknown as Response)

describe('the session-expired banner', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { cleanup(); localStorage.clear() })

  it('stays down on a 401 when no token is in storage (never signed in)', async () => {
    render(<ErrorSurface onLogout={() => {}} />)
    await act(async () => {
      await guardedPoll(unauthorized())
      await guardedJson(unauthorized(), 'Loading analytics')
      await actionError(unauthorized(), 'Saving')
    })
    expect(screen.queryByText(/Session expired/)).toBeNull()
  })

  it('rises on a 401 when a token is in storage (a session that really expired)', async () => {
    localStorage.setItem('az_jwt_token', 'stale-but-present')
    render(<ErrorSurface onLogout={() => {}} />)
    await act(async () => { await guardedPoll(unauthorized()) })
    expect(screen.getByText(/Session expired/)).toBeInTheDocument()
  })

  it('still returns null to the caller either way, so no data is rendered from a 401', async () => {
    render(<ErrorSurface onLogout={() => {}} />)
    let result: unknown = 'unset'
    await act(async () => { result = await guardedPoll(unauthorized()) })
    expect(result).toBeNull()
    localStorage.setItem('az_jwt_token', 'stale-but-present')
    await act(async () => { result = await guardedPoll(unauthorized()) })
    expect(result).toBeNull()
  })
})
