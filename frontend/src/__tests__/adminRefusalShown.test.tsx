/**
 * TWO ADMIN ACTIONS SHOW THEIR REFUSAL (2026-10-29).
 *
 * actionError returns display-ready failure text and shows nothing itself;
 * each caller toasts it. Two callers dropped it:
 * - the Quarantine tab's release / discard (`if (!err) load()`): a 403 on a
 *   release by a non-Owner, or a 404 on a row someone else already reviewed,
 *   left the row in place with nothing said - while the comment above the
 *   action said such a refusal "must SAY so";
 * - the Trust tab's first measurement (`if (synced) return`): a refused
 *   question-set sync ended the run with the button back and no word why.
 * Both now toast the text through the page's error surface, as the KB page's
 * remove and the Users tab do. A 2xx is unchanged.
 */
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { QuarantineTab } from '../admin/QuarantineTab'
import { TrustTab } from '../admin/TrustTab'
import { ErrorSurface } from '../errorSurface'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = globalThis.fetch

const HELD = {
  id: 7, source: 'held.md', department: 'general', trust_tier: 'untrusted',
  findings: [{ type: 'instruction' }], text_preview: 'preview', text_length: 7,
  status: 'held', created_at: '2026-10-29T10:00:00', reviewed_at: null,
}

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

describe('Quarantine tab: a refused release or discard is shown', () => {
  function install(action: () => Response) {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method || 'GET').toUpperCase()
      if (method !== 'GET' && url.includes('/api/admin/kb/quarantine/')) return action()
      if (url.includes('/api/admin/kb/quarantine')) return json({ items: [HELD] })
      return json({})
    }) as unknown as typeof fetch
  }

  it('a release refused with 403 is toasted', async () => {
    install(() => json({ detail: 'Owner only' }, 403))
    render(<><ErrorSurface /><QuarantineTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} /></>)
    fireEvent.click(await screen.findByRole('button', { name: 'View' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Release into the knowledge base' }))
    expect(await screen.findByText(/Releasing document failed: Owner only/)).toBeInTheDocument()
  })

  it('a discard answering 404 is toasted', async () => {
    install(() => json({ detail: 'Already reviewed' }, 404))
    render(<><ErrorSurface /><QuarantineTab api="" headers={() => ({})} currentUser={{ role: 'admin' }} /></>)
    fireEvent.click(await screen.findByRole('button', { name: 'View' }))
    fireEvent.click(await screen.findByRole('button', { name: /Discard/ }))
    expect(await screen.findByText(/Discarding document failed: Already reviewed/)).toBeInTheDocument()
  })
})

describe('Trust tab: a refused question-set sync is shown', () => {
  it('the first measurement toasts the sync refusal and starts no run', async () => {
    const runs: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method || 'GET').toUpperCase()
      if (url.includes('/api/admin/evals/questions/sync')) return json({ detail: 'Owner only' }, 403)
      if (method === 'POST' && url.includes('/api/admin/evals/run')) { runs.push(url); return json({}) }
      if (url.includes('/api/admin/trust')) return json({ available: false })
      return json({})
    }) as unknown as typeof fetch
    render(<><ErrorSurface /><TrustTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} /></>)
    fireEvent.click(await screen.findByRole('button', { name: 'Run the first measurement' }))
    expect(await screen.findByText(/Preparing the question set failed: Owner only/)).toBeInTheDocument()
    expect(runs).toHaveLength(0)
  })
})
