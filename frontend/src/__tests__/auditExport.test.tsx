/**
 * THE AUDIT EXPORT SAYS IT FAILED INSTEAD OF SAVING THE FAILURE (2026-10-30).
 *
 * Export CSV saved whatever came back as audit_log.csv without reading the
 * status, so an expired session downloaded the error JSON as the file, and a
 * request that never reached the server was swallowed - in both cases with no
 * word that the export failed. A failure is now a toast with a fixed sentence
 * and no file; a 200 still downloads.
 */
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { AuditTab } from '../admin/AuditTab'
import { ErrorSurface } from '../errorSurface'

const FAILED = 'Exporting the audit log failed. No file was saved.'
const CSV = 'id,username,timestamp\n1,someone,2026-10-30T07:00:00\n'

const realFetch = globalThis.fetch
const realCreate = URL.createObjectURL
const realRevoke = URL.revokeObjectURL

let created: Blob[]
let saved: string[]
// The toast surface drops a repeat of the same message inside 5 s, so each
// test gets its own minute on a frozen clock (Date only; timers stay real).
let minute = 0

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(2026, 9, 30, 7, minute++))
  created = []
  saved = []
  URL.createObjectURL = vi.fn((b: Blob) => { created.push(b); return 'blob:audit' }) as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn() as typeof URL.revokeObjectURL
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download)
  })
})

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  URL.createObjectURL = realCreate
  URL.revokeObjectURL = realRevoke
  vi.restoreAllMocks()
  vi.useRealTimers()
  localStorage.clear()
})

function install(exportAnswer: () => Promise<Response>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/admin/audit/export')) return exportAnswer()
    if (url.includes('/api/admin/audit')) {
      return new Response(JSON.stringify({ entries: [], total: 0, pages: 1 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    return new Response('{}', { status: 200 })
  }) as unknown as typeof fetch
}

function renderTab() {
  render(<><ErrorSurface /><AuditTab api="" headers={() => ({})} /></>)
}

describe('Audit Log tab: Export CSV', () => {
  it('an export answering 401 toasts the failure and saves no file', async () => {
    // A session was held: the 401 also raises the expired banner.
    localStorage.setItem('az_jwt_token', 'stale-but-present')
    install(async () => new Response(JSON.stringify({ detail: 'Token has expired' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } }))
    renderTab()
    fireEvent.click(await screen.findByRole('button', { name: 'Export CSV' }))
    // Wait for the export to land either way: a saved file or a toast.
    await waitFor(() => expect(saved.length + screen.queryAllByText(FAILED).length).toBeGreaterThan(0))
    expect(saved).toHaveLength(0)
    expect(created).toHaveLength(0)
    expect(screen.getByText(FAILED)).toBeInTheDocument()
    expect(screen.getByText(/Session expired/)).toBeInTheDocument()
    expect(screen.queryByText(/Token has expired/)).toBeNull()
  })

  it('an export that never reaches the server toasts the failure and saves no file', async () => {
    install(async () => { throw new TypeError('Failed to fetch') })
    renderTab()
    fireEvent.click(await screen.findByRole('button', { name: 'Export CSV' }))
    expect(await screen.findByText(FAILED)).toBeInTheDocument()
    expect(created).toHaveLength(0)
    expect(saved).toHaveLength(0)
  })

  it('a 200 CSV still downloads as audit_log.csv, with no toast', async () => {
    install(async () => new Response(CSV, { status: 200, headers: { 'Content-Type': 'text/csv' } }))
    renderTab()
    fireEvent.click(await screen.findByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(saved).toEqual(['audit_log.csv']))
    expect(created).toHaveLength(1)
    expect(created[0].type).toBe('text/csv')
    expect(created[0].size).toBe(CSV.length)
    expect(screen.queryByText(FAILED)).toBeNull()
  })
})
