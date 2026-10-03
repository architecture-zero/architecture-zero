/**
 * THE FOOTER AND THE BADGE NAME THE MODEL THAT ANSWERS (2026-10-02).
 *
 * A picked model is sent, and the server answers with it whenever selection is
 * allowed - but the footer and the header badge kept naming the server's pin,
 * a model that did not answer. And with the server's answer unknown, the badge
 * fell back to the client's own guess, which an untouched request never sends.
 * Both cases fail on the code before this change.
 */
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import App from '../App'

const USER = { id: 1, username: 'tester', role: 'owner', permissions: [] }
const GROUPS = [{ provider: 'ollama', label: 'Local', models: [
  { value: 'first-listed', label: 'First Listed', badge: 'local' },
  { value: 'picked-model', label: 'Picked Model', badge: 'local' },
] }]

function install(config: Record<string, unknown> | null) {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/auth/config')) {
      return json({ needs_setup: false, auth_mode: 'local', guest_mode_enabled: false, allow_rag_toggle: true })
    }
    if (url.includes('/api/auth/me')) return json(USER)
    if (url.includes('/api/config')) return config ? json(config) : json({ detail: 'unavailable' }, 503)
    if (url.includes('/api/models')) return json({ groups: GROUPS })
    if (url.includes('/api/sessions/mine')) return json({ sessions: [] })
    return json({})
  }) as unknown as typeof fetch
}

async function signedIn() {
  localStorage.setItem('az_jwt_token', 'test-token')
  render(<App />)
  await waitFor(() => expect(screen.getByPlaceholderText(/Message/i)).toBeInTheDocument())
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('the model the screen names', () => {
  it('names the pin until a model is picked, then the pick', async () => {
    install({ default_model: 'default-model', chat_model_effective: 'pinned-model',
              allow_model_selection: true, allow_rag_toggle: true, default_rag_enabled: true,
              suggestions: [], instance_name: 'Test' })
    await signedIn()
    await waitFor(() => expect(screen.getByText(/Powered by .* · pinned-model ·/)).toBeInTheDocument())

    await act(async () => { fireEvent.click(await screen.findByText('Picked Model')) })

    // The pick is what this client now sends, and what the server answers with.
    expect(screen.getByText(/Powered by .* · picked-model ·/)).toBeInTheDocument()
    expect(screen.queryByText(/· pinned-model ·/)).toBeNull()
  })

  it('names no model when the server has not said which one answers', async () => {
    install(null)
    await signedIn()
    await screen.findByText('First Listed')   // the picker loaded; the config read did not

    expect(screen.getByText(/Powered by Architecture Zero · responses are AI-generated/)).toBeInTheDocument()
    // The badge does not guess: an untouched request sends no model, so the
    // first listed one is not what answers.
    expect(screen.queryByText('first-listed')).toBeNull()
  })
})
