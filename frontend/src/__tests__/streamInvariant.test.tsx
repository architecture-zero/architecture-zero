/**
 * THE STORED-ROW INVARIANT, tested as an invariant rather than as five bugs.
 *
 * Every HIGH-severity defect found in this repo's seven review rounds lived in
 * this file's subject - the chat client - and until now no test mounted it. The
 * suite could report 578 passing while the client was broken in a way that
 * silently deleted people's conversations, and it twice did exactly that.
 *
 * The rule under test: a chat bubble is `ephemeral` exactly when NO row for it
 * exists on the server. Regenerate turns that into a number - it sends
 * `DELETE /api/history/{id}/tail?count=N` where N is the count of STORED rows
 * in the tail - and the endpoint deletes N rows by id with no role awareness.
 * So an over-count silently destroys the previous turn's answer, and an
 * under-count orphans a row. The count is the observable, which is what makes
 * this testable end to end without reaching into React state.
 *
 * Each case drives one stream outcome and asserts the resulting count. Adding a
 * new way for a stream to end means adding a row here; the bug class cannot
 * come back one instance at a time.
 */
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import App from '../App'

const USER = { id: 1, username: 'tester', role: 'owner', permissions: [] }

/** One scripted SSE body. `events` are pushed in order; `abortable` exposes the
 *  reader so a test can leave the stream open and press Stop. */
function sseStream(events: string[], opts: { hang?: boolean } = {}) {
  let controller: ReadableStreamDefaultController<Uint8Array>
  const enc = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
      for (const e of events) c.enqueue(enc.encode(e))
      // hang = leave the stream open, the way a real generator does while the
      // model is still producing. Stop is only meaningful against an open one.
      if (!opts.hang) c.close()
    },
  })
  return { stream, push: (e: string) => controller!.enqueue(enc.encode(e)), close: () => controller!.close() }
}

const tok = (t: string) => `data: ${JSON.stringify({ token: t })}\n\n`
const errEvent = (m: string) => `data: ${JSON.stringify({ error: m })}\n\n`
const DONE = 'data: [DONE]\n\n'

interface Harness {
  trimCounts: number[]
  chatBodies: Record<string, unknown>[]
  setChat: (r: () => Response | Promise<Response>) => void
}

function installFetch(): Harness {
  const trimCounts: number[] = []
  const chatBodies: Record<string, unknown>[] = []
  let chatResponder: () => Response | Promise<Response> = () =>
    new Response(sseStream([tok('hi'), DONE]).stream, { status: 200 })

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status, headers: { 'Content-Type': 'application/json' },
    })

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method || 'GET').toUpperCase()

    if (url.includes('/api/auth/config')) {
      return json({ needs_setup: false, auth_mode: 'local',
                    guest_mode_enabled: false, allow_rag_toggle: true })
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
    if (url.includes('/tail')) {
      // THE OBSERVABLE. Record what the client believes is stored.
      trimCounts.push(Number(new URL(url, 'http://t').searchParams.get('count')))
      return json({ status: 'ok', deleted: 0, requested: 0 })
    }
    if (url.includes('/api/history/')) return json({ messages: [] })
    if (url.includes('/api/chat')) {
      chatBodies.push(JSON.parse(String(init?.body ?? '{}')))
      return chatResponder()
    }
    return json({})
  }) as unknown as typeof fetch

  return { trimCounts, chatBodies, setChat: (r) => { chatResponder = r } }
}

async function signedInApp() {
  localStorage.setItem('az_jwt_token', 'test-token')
  render(<App />)
  // The composer only exists once boot resolved to the chat view.
  await waitFor(() => expect(screen.getByPlaceholderText(/Message/i)).toBeInTheDocument())
}

async function ask(text: string) {
  const box = screen.getByPlaceholderText(/Message/i)
  fireEvent.change(box, { target: { value: text } })
  fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true })
}

async function clickRegenerate() {
  const btn = await screen.findByRole('button', { name: /regenerate/i })
  await act(async () => { fireEvent.click(btn) })
}

let h: Harness

beforeEach(() => {
  localStorage.clear()
  h = installFetch()
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('the stored-row invariant', () => {
  it('counts BOTH rows after a clean answer', async () => {
    await signedInApp()
    await ask('question one')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())

    await clickRegenerate()

    // user row + assistant row are both stored.
    await waitFor(() => expect(h.trimCounts).toEqual([2]))
  })

  it('counts ONE row when the provider dies after tokens have flowed', async () => {
    await signedInApp()
    h.setChat(() => new Response(
      sseStream([tok('FRAGMENTKEPT'), errEvent('provider exploded')]).stream,
      { status: 200 }))
    await ask('question two')
    // "was not saved" is the notice's own text. Matching /stopped early/ alone
    // is ambiguous: the error TOAST says that too, and both firing is correct.
    await waitFor(() =>
      expect(screen.getByText(/was not saved/i)).toBeInTheDocument())
    // The partial answer itself must survive - it is real output. Marker is
    // deliberately not a word the notice also uses, or the query matches both.
    expect(screen.getByText(/FRAGMENTKEPT/)).toBeInTheDocument()

    await clickRegenerate()

    // The assistant row is NEVER written when the generator raises, so only the
    // user row is stored. Counting the visible partial answer as a stored row
    // is what deleted the PREVIOUS turn's answer.
    await waitFor(() => expect(h.trimCounts).toEqual([1]))
  })

  it('counts ONE row when the stream errors before any token', async () => {
    await signedInApp()
    h.setChat(() => new Response(
      sseStream([errEvent('nothing came back')]).stream, { status: 200 }))
    await ask('question three')
    await waitFor(() =>
      expect(screen.getByText(/nothing came back/i)).toBeInTheDocument())

    await clickRegenerate()

    await waitFor(() => expect(h.trimCounts).toEqual([1]))
  })

  it('sends NO trim at all when the request was rejected before storage', async () => {
    await signedInApp()
    h.setChat(() => new Response(JSON.stringify({ detail: 'Guest limit reached.' }),
      { status: 429, headers: { 'Content-Type': 'application/json' } }))
    await ask('question four')
    await waitFor(() =>
      expect(screen.getByText(/Guest limit reached/i)).toBeInTheDocument())

    await clickRegenerate()

    // Every rejection gate runs BEFORE the user row is written, so nothing at
    // all is stored for this turn - a trim of any size would eat a real row
    // from the turn before it.
    await waitFor(() => expect(h.trimCounts).toEqual([]))
  })
})

describe('what reaches the model', () => {
  it('never posts an unstored bubble back as conversation', async () => {
    await signedInApp()
    h.setChat(() => new Response(
      sseStream([tok('partial'), errEvent('died')]).stream, { status: 200 }))
    await ask('first')
    await waitFor(() => expect(screen.getByText(/stopped early/i)).toBeInTheDocument())

    // Next turn succeeds; inspect the history it carries.
    h.setChat(() => new Response(sseStream([tok('ok'), DONE]).stream, { status: 200 }))
    await ask('second')
    await waitFor(() => expect(h.chatBodies.length).toBe(2))

    const history = (h.chatBodies[1].history ?? []) as Array<{ content: string }>
    const blob = history.map(m => m.content).join(' ')
    // The failure NOTICE must never be posted as the assistant's own words -
    // that teaches the model it said something it never said. It lives in its
    // own field precisely so this can be true.
    expect(blob).not.toMatch(/stopped early/i)
    expect(blob).not.toMatch(/provider failed/i)
  })
})
