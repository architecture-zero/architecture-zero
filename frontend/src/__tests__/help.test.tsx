/**
 * In-product help (2026-09-21). The Help pill on the shell is a toggle:
 * pressed, the next question goes to the reserved help department with
 * retrieval forced on and the federation off; a second press returns to the
 * knowledge base and restores the conversation the visitor left. These pin
 * the request the server sees, which a screenshot cannot, and the one thing
 * the pill must never do - delete the conversation it was opened over.
 */
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import App from '../App'

const USER = { id: 1, username: 'tester', role: 'owner', permissions: [] }

interface Call { url: string; method: string; body?: string }

function sse(events: string[]) {
  const enc = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(c) {
      for (const e of events) c.enqueue(enc.encode(e))
      c.close()
    },
  })
}

function installFetch(opts: { helpEnabled?: boolean } = {}): Call[] {
  const calls: Call[] = []
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method || 'GET').toUpperCase()
    calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : undefined })

    if (url.includes('/api/auth/config')) {
      return json({ needs_setup: false, auth_mode: 'local', guest_mode_enabled: false,
                    allow_rag_toggle: true,
                    ...(opts.helpEnabled === undefined ? {} : { help_enabled: opts.helpEnabled }) })
    }
    if (url.includes('/api/auth/me')) return json(USER)
    if (url.includes('/api/config')) {
      return json({ default_model: 'test-model', default_rag_enabled: true,
                    allow_model_selection: true, allow_rag_toggle: true,
                    instance_name: 'Test', suggestions: [],
                    chat_model_effective: 'test-model' })
    }
    if (url.includes('/api/models')) return json({ groups: [] })
    if (url.includes('/api/status')) return json({})
    if (url.includes('/api/analytics')) return json({})
    if (url.includes('/api/sessions/mine')) return json({ sessions: [] })
    if (url.includes('/api/help/page')) {
      return json({ name: 'help/getting-started.md', content: '# Getting started\n\nOpen the address in your browser.' })
    }
    if (url.includes('/api/history/')) return json({ messages: [] })
    if (url.includes('/api/chat')) {
      return new Response(sse([
        `data: ${JSON.stringify({ sources: ['help/getting-started.md'] })}\n\n`,
        `data: ${JSON.stringify({ token: 'Open the address.' })}\n\n`,
        'data: [DONE]\n\n',
      ]), { status: 200 })
    }
    return json({})
  }) as unknown as typeof fetch

  return calls
}

async function signedInApp() {
  localStorage.setItem('az_jwt_token', 'test-token')
  render(<App />)
  await waitFor(() => expect(screen.getByPlaceholderText(/Message Test/)).toBeInTheDocument())
}

const helpPill = () => screen.getByRole('button', { name: 'Help' })

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('help mode', () => {
  it('the Help pill enters help mode: the question asks the help department, no peers, retrieval on', async () => {
    const calls = installFetch()
    await signedInApp()
    expect(helpPill()).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(helpPill())
    expect(helpPill()).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByPlaceholderText(/Ask how Test works/)).toBeInTheDocument()
    expect(screen.getByTestId('help-card')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'How do I sign in?' }))
    await waitFor(() => expect(calls.some(c => c.url.includes('/api/chat'))).toBe(true))
    const body = JSON.parse(calls.find(c => c.url.includes('/api/chat'))!.body!)
    expect(body.department).toBe('help')
    expect(body.use_peers).toBe(false)
    expect(body.use_rag).toBe(true)
    // entering help never deletes the conversation the visitor was in
    expect(calls.filter(c => c.url.includes('/api/history/') && c.method === 'DELETE')).toHaveLength(0)
  })

  it('a second press leaves help mode and restores the session it was opened over', async () => {
    installFetch()
    await signedInApp()
    const before = localStorage.getItem('az_session_id_1')
    fireEvent.click(helpPill())
    expect(localStorage.getItem('help_mode')).toBe('1')
    const during = localStorage.getItem('az_session_id_1')
    expect(during).not.toBe(before)                           // help gets its own session
    fireEvent.click(helpPill())
    expect(helpPill()).toHaveAttribute('aria-pressed', 'false')
    expect(localStorage.getItem('help_mode')).toBeNull()
    const after = localStorage.getItem('az_session_id_1')
    expect(after).toBe(before)                                // and the old one comes back
    expect(screen.getByPlaceholderText(/Message Test/)).toBeInTheDocument()
    expect(screen.queryByTestId('help-card')).toBeNull()
  })

  it('a help citation opens the page through its own read', async () => {
    const calls = installFetch()
    await signedInApp()
    fireEvent.click(helpPill())
    fireEvent.click(screen.getByRole('button', { name: 'How do I sign in?' }))
    // A button's accessible name is its text; the tooltip is decoration.
    const chip = await screen.findByRole('button', { name: 'help/getting-started.md' })
    fireEvent.click(chip)
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(screen.getByText(/Open the address in your browser/)).toBeInTheDocument()
    expect(calls.some(c => c.url.includes('/api/help/page?name=help%2Fgetting-started.md'))).toBe(true)
  })

  it('the pill is hidden when the server runs without the help lane', async () => {
    installFetch({ helpEnabled: false })
    await signedInApp()
    expect(screen.queryByRole('button', { name: 'Help' })).toBeNull()
  })
})
