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

/** A stream that stays open until the test pushes to it or closes it, and that
 *  fails the way a real fetch body does when the request is aborted - which a
 *  mocked fetch otherwise ignores, so Stop would have nothing to stop. */
function openStream() {
  const enc = new TextEncoder()
  let controller: ReadableStreamDefaultController<Uint8Array>
  const stream = new ReadableStream<Uint8Array>({ start(c) { controller = c } })
  return {
    respond: (init?: RequestInit) => {
      init?.signal?.addEventListener('abort', () => {
        const aborted = new Error('The user aborted a request.')
        aborted.name = 'AbortError'
        try { controller.error(aborted) } catch { /* already closed */ }
      })
      return new Response(stream, { status: 200 })
    },
    push: (e: string) => controller.enqueue(enc.encode(e)),
    close: () => controller.close(),
  }
}

interface Harness {
  trimCounts: number[]
  chatBodies: Record<string, unknown>[]
  // Every DELETE that is not a trim: what a control removed outright.
  deletes: string[]
  setChat: (r: (init?: RequestInit) => Response | Promise<Response>) => void
  setSessions: (s: Array<{ session: string; first_message: string }>) => void
  // One session's history read, answered by the test (it may hold it open).
  setHistory: (session: string, r: () => Response | Promise<Response>) => void
  // The trim's answer, held by the test when it wants a regenerate mid-trim.
  setTrim: (r: () => Response | Promise<Response>) => void
  setLogout: (r: () => Response | Promise<Response>) => void
  // A conversation delete's answer (the History list's confirm, 2026-10-03).
  setDelete: (r: () => Response | Promise<Response>) => void
}

/** A promise the test resolves by hand - a request held open. */
function held<T>() {
  let release!: (v: T) => void
  const promise = new Promise<T>(r => { release = r })
  return { promise, release }
}

function installFetch(opts: { guestMode?: boolean } = {}): Harness {
  const trimCounts: number[] = []
  const chatBodies: Record<string, unknown>[] = []
  const deletes: string[] = []
  let sessions: Array<{ session: string; first_message: string }> = []
  const histories: Record<string, () => Response | Promise<Response>> = {}
  let trimResponder: (() => Response | Promise<Response>) | null = null
  let logoutResponder: (() => Response | Promise<Response>) | null = null
  let deleteResponder: (() => Response | Promise<Response>) | null = null
  let chatResponder: (init?: RequestInit) => Response | Promise<Response> = () =>
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
                    guest_mode_enabled: opts.guestMode === true, allow_rag_toggle: true })
    }
    if (url.includes('/api/auth/login')) {
      return json({ access_token: 'account-token', refresh_token: 'account-refresh', user: USER })
    }
    if (url.includes('/api/auth/logout')) return logoutResponder ? logoutResponder() : json({})
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
    if (url.includes('/api/sessions/mine')) return json({ sessions })
    if (url.includes('/tail')) {
      // THE OBSERVABLE. Record what the client believes is stored.
      trimCounts.push(Number(new URL(url, 'http://t').searchParams.get('count')))
      return trimResponder ? trimResponder() : json({ status: 'ok', deleted: 0, requested: 0 })
    }
    if (url.includes('/api/history/') && method === 'DELETE') {
      deletes.push(url)
      return deleteResponder ? deleteResponder() : json({ status: 'ok' })
    }
    if (url.includes('/api/history/')) {
      const session = decodeURIComponent(url.split('/api/history/')[1].split('?')[0])
      return histories[session] ? histories[session]() : json({ messages: [] })
    }
    if (url.includes('/api/chat')) {
      chatBodies.push(JSON.parse(String(init?.body ?? '{}')))
      return chatResponder(init)
    }
    return json({})
  }) as unknown as typeof fetch

  return {
    trimCounts, chatBodies, deletes,
    setChat: (r) => { chatResponder = r },
    setSessions: (s) => { sessions = s },
    setHistory: (session, r) => { histories[session] = r },
    setTrim: (r) => { trimResponder = r },
    setLogout: (r) => { logoutResponder = r },
    setDelete: (r) => { deleteResponder = r },
  }
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

describe('when React renders late', () => {
  it('a stream that ends before React renders still lands every write', async () => {
    await signedInApp()
    h.setChat(() => new Response(sseStream([tok('AAA'), tok('BBB'), DONE]).stream, { status: 200 }))
    // Everything inside act() renders only when act() ends - here, after the
    // whole stream has run and its finally has retired its ticket. That is the
    // state the writes' updaters meet whenever React renders late, and a check
    // on the live ticket inside them dropped the answer's text and both of the
    // turn's "this row is stored" flags.
    await act(async () => {
      await ask('question late')
      await new Promise(r => setTimeout(r, 50))
    })

    expect(screen.getByText(/AAABBB/)).toBeInTheDocument()
    await clickRegenerate()
    await waitFor(() => expect(h.trimCounts).toEqual([2]))
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

describe('a vote lands on a stored row or is not offered', () => {
  it('offers no thumbs on an answer that was never stored', async () => {
    await signedInApp()
    await ask('first')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setChat(() => new Response(
      sseStream([tok('UNSTOREDPART'), errEvent('died')]).stream, { status: 200 }))
    await ask('second')
    await waitFor(() => expect(screen.getByText(/was not saved/i)).toBeInTheDocument())

    // One stored answer, one pair. A vote on the unstored bubble was posted
    // with the ordinal of the NEXT stored row - another turn's label.
    expect(screen.getAllByTitle('Good response')).toHaveLength(1)
    expect(screen.getAllByTitle('Bad response')).toHaveLength(1)
  })

  it('offers a guest no thumbs at all - their vote has nowhere to go', async () => {
    h = installFetch({ guestMode: true })
    render(<App />)
    const guestDoor = await screen.findByRole('button', { name: /Continue as guest/i })
    await act(async () => { fireEvent.click(guestDoor) })
    await screen.findByPlaceholderText(/Message/i)
    await ask('a guest question')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())

    expect(screen.queryByTitle('Good response')).toBeNull()
    expect(screen.queryByTitle('Bad response')).toBeNull()
  })
})

describe('one turn at a time', () => {
  it('a refused turn (409) leaves no bubble, restores the draft and says so', async () => {
    await signedInApp()
    // The server's one-turn-at-a-time refusal: another turn on this
    // conversation is still answering. Nothing was written or billed.
    h.setChat(() => new Response(
      JSON.stringify({ detail: 'Still answering your last message on this conversation - wait for it to finish, then send again.' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } }))
    await ask('question five')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/still answering/i))

    // The draft is back in the box, and no bubble carries it - it was never a
    // turn. (The box itself matches a text query through its value, so the
    // bubble check looks past it.)
    const box = screen.getByPlaceholderText(/Message/i) as HTMLTextAreaElement
    expect(box.value).toBe('question five')
    expect(screen.queryByText('question five', { ignore: 'textarea, script, style' })).toBeNull()
    // Not an error bubble either.
    expect(screen.queryByText(/the server returned/i)).toBeNull()
    // And the composer is live again: Send, not Stop.
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
  })
})

describe('the size refusal (413)', () => {
  it('leaves no bubble, restores the draft and shows the server sentence once', async () => {
    await signedInApp()
    h.setChat(() => new Response(
      JSON.stringify({ detail: 'Message too long: this request carries 200,001 characters and the limit is 200,000 (your message plus the conversation so far). Shorten the message or start a new chat.' }),
      { status: 413, headers: { 'Content-Type': 'application/json' } }))
    await ask('question six')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Message too long/i))

    const box = screen.getByPlaceholderText(/Message/i) as HTMLTextAreaElement
    expect(box.value).toBe('question six')
    expect(screen.queryByText('question six', { ignore: 'textarea, script, style' })).toBeNull()
    // Once: under the transcript, never also as an assistant bubble - which
    // is how the generic refusal branch showed it before, draft gone.
    expect(screen.getAllByText(/Message too long/i)).toHaveLength(1)
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
  })

  it('gives the draft back only into an empty box - never over text typed since', async () => {
    // The box stays live while a request is in flight (00e811a), so a refusal
    // can arrive after the user has started the next thought. Writing the old
    // draft back unconditionally erased it (found 2026-10-02 by the forks'
    // audit of these rounds).
    await signedInApp()
    let release!: (r: Response) => void
    h.setChat(() => new Promise<Response>(res => { release = res }))
    await ask('question nine')
    const box = screen.getByPlaceholderText(/Message/i) as HTMLTextAreaElement
    await waitFor(() => expect(box.value).toBe(''))
    fireEvent.change(box, { target: { value: 'my next thought' } })
    await act(async () => {
      release(new Response(JSON.stringify({ detail: 'busy' }),
        { status: 409, headers: { 'Content-Type': 'application/json' } }))
    })
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
    expect(box.value).toBe('my next thought')
  })

  it('says what happened when the 413 carries no sentence (a proxy page)', async () => {
    await signedInApp()
    h.setChat(() => new Response('<html><body><h1>413 Request Entity Too Large</h1></body></html>',
      { status: 413, headers: { 'Content-Type': 'text/html' } }))
    await ask('question seven')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/too large to send/i))
    expect((screen.getByPlaceholderText(/Message/i) as HTMLTextAreaElement).value).toBe('question seven')
  })

  it('the refusal line belongs to its conversation - leaving it clears the line', async () => {
    await signedInApp()
    h.setChat(() => new Response(JSON.stringify({ detail: 'busy' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } }))
    await ask('question eight')
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New Chat/i })) })

    expect(screen.queryByRole('status')).toBeNull()
  })
})

describe('Stop is Stop for the whole answer', () => {
  it('stays Stop after the first token, and a second send cannot start', async () => {
    await signedInApp()
    const s = openStream()
    h.setChat(init => s.respond(init))
    await ask('a long one')
    await act(async () => { s.push(tok('FIRSTWORDS')) })
    await waitFor(() => expect(screen.getByText(/FIRSTWORDS/)).toBeInTheDocument())

    // `loading` clears at the first token; were it the only guard, Stop would
    // turn back into Send here and the answer could not be stopped.
    expect(screen.getByTitle('Stop generation')).toBeInTheDocument()
    // Typing is allowed mid-answer; sending is not.
    await ask('second question')
    expect(h.chatBodies).toHaveLength(1)

    await act(async () => { s.push(tok(' and the rest')); s.push(DONE); s.close() })
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
  })

  it('Stop after tokens keeps the partial answer, says it was not saved, and Regenerate trims ONE row', async () => {
    await signedInApp()
    const s = openStream()
    h.setChat(init => s.respond(init))
    await ask('question nine')
    await act(async () => { s.push(tok('PARTIALKEPT')) })
    await waitFor(() => expect(screen.getByText(/PARTIALKEPT/)).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByTitle('Stop generation')) })
    await waitFor(() => expect(screen.getByText(/was not saved/i)).toBeInTheDocument())
    expect(screen.getByText(/PARTIALKEPT/)).toBeInTheDocument()

    h.setChat(() => new Response(sseStream([tok('again'), DONE]).stream, { status: 200 }))
    await clickRegenerate()
    // The user row was stored (the response was OK); the stopped answer was
    // not - counting it is what would delete the previous turn's answer.
    await waitFor(() => expect(h.trimCounts).toEqual([1]))
  })

  it('Stop before the first token says so, and Regenerate trims ONE row', async () => {
    await signedInApp()
    const s = openStream()
    h.setChat(init => s.respond(init))
    await ask('question ten')
    await waitFor(() => expect(screen.getByTitle('Stop generation')).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByTitle('Stop generation')) })
    await waitFor(() => expect(screen.getByText(/Stopped before the answer started/i)).toBeInTheDocument())

    h.setChat(() => new Response(sseStream([tok('again'), DONE]).stream, { status: 200 }))
    await clickRegenerate()
    await waitFor(() => expect(h.trimCounts).toEqual([1]))
  })
})

/** A chat request with no answer yet - the route is still retrieving - that
 *  fails the way a real fetch does when it is aborted before the response:
 *  the fetch promise itself rejects, so no status ever reaches the client. */
const answerNotYet = (init?: RequestInit) => new Promise<Response>((_, reject) => {
  init?.signal?.addEventListener('abort', () => {
    const aborted = new Error('The user aborted a request.')
    aborted.name = 'AbortError'
    reject(aborted)
  })
})

describe('a Stop before the 200 (2026-10-21)', () => {
  // chat.py writes the user row past every refusal and just before it answers,
  // and nothing before that write asks whether the caller is still there - so
  // a Stop that lands while the route is still retrieving leaves the row
  // behind. The client counted that prompt as unstored: Regenerate trimmed
  // nothing and sent it again, and the server held it twice.
  it('FIX: counts the stopped prompt as stored, so Regenerate trims it and sends it once more', async () => {
    await signedInApp()
    await ask('first')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setChat(answerNotYet)
    await ask('question sixteen')
    await waitFor(() => expect(screen.getByTitle('Stop generation')).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByTitle('Stop generation')) })
    await waitFor(() => expect(screen.getByText(/Stopped before the answer started/i)).toBeInTheDocument())

    h.setChat(() => new Response(sseStream([tok('again'), DONE]).stream, { status: 200 }))
    await clickRegenerate()
    // The stopped prompt's row, and only that row (the notice has none).
    await waitFor(() => expect(h.trimCounts).toEqual([1]))
    await waitFor(() => expect(h.chatBodies).toHaveLength(3))
    expect(h.chatBodies[2].prompt).toBe('question sixteen')
    // The re-send carries the turn before it, never the prompt it replaces.
    expect((h.chatBodies[2].history as Array<{ content: string }>).map(m => m.content))
      .toEqual(['first', 'hi'])
  })

  it('CONTROL: a send that never reached the server is still unsaved - Regenerate trims nothing', async () => {
    await signedInApp()
    h.setChat(() => Promise.reject(new TypeError('Failed to fetch')))
    await ask('question seventeen')
    await waitFor(() => expect(screen.getByText(/Could not reach the backend/i)).toBeInTheDocument())

    h.setChat(() => new Response(sseStream([tok('again'), DONE]).stream, { status: 200 }))
    await clickRegenerate()
    await waitFor(() => expect(h.chatBodies).toHaveLength(2))
    // No row was written, so a trim of any size would eat a real one.
    expect(h.trimCounts).toEqual([])
  })
})

describe('a trim the server refuses while a turn is in flight (2026-10-21)', () => {
  // The tail route answers 409 while a turn on this conversation still holds
  // its slot - the one a Stop leaves running until the route's late write.
  // Nothing was trimmed, so a re-send would store the prompt a second time.
  const refusedTrim = () => new Response(
    JSON.stringify({ detail: 'Still answering your last message on this conversation - wait for it to finish, then send again.' }),
    { status: 409, headers: { 'Content-Type': 'application/json' } })

  it('FIX: Regenerate sends nothing after a refused trim, keeps the turn and says why', async () => {
    await signedInApp()
    await ask('question eighteen')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(refusedTrim)

    await clickRegenerate()
    await waitFor(() => expect(h.trimCounts).toEqual([2]))
    expect(await screen.findByText(/Trimming history failed: Still answering/i)).toBeInTheDocument()
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(h.chatBodies).toHaveLength(1)
    // The turn stays on screen as it is stored, and Regenerate is free again.
    expect(screen.getByText('hi')).toBeInTheDocument()
    expect(screen.getByText('question eighteen', { ignore: 'textarea, script, style' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
  })

  it('CONTROL: a trim that fails any other way still regenerates, as before', async () => {
    await signedInApp()
    await ask('question nineteen')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(() => new Response(JSON.stringify({ detail: 'database is locked' }),
      { status: 500, headers: { 'Content-Type': 'application/json' } }))

    await clickRegenerate()
    await waitFor(() => expect(h.chatBodies).toHaveLength(2))
    expect(h.chatBodies[1].prompt).toBe('question nineteen')
  })
})

describe('Edit after a trim the server refuses (2026-10-21)', () => {
  // Edit trims the rows from the edited message on, then sends the new text.
  // It went on after a 409 trim the way Regenerate did before the case above:
  // nothing had been trimmed, so the edited prompt landed as an extra row after
  // the very rows the trim was meant to remove. The refusal's sentence differs
  // from the Regenerate case only because the toast drops a repeat of the same
  // line inside five seconds; the client shows whatever sentence the route sends.
  const refusedTrim = () => new Response(
    JSON.stringify({ detail: 'A turn on this conversation is still running - wait for it to finish, then edit again.' }),
    { status: 409, headers: { 'Content-Type': 'application/json' } })
  const landedTrim = () => new Response(
    JSON.stringify({ status: 'ok', deleted: 2, requested: 2 }),
    { status: 200, headers: { 'Content-Type': 'application/json' } })

  async function editTo(from: string, to: string) {
    await act(async () => { fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]) })
    fireEvent.change(screen.getByDisplayValue(from), { target: { value: to } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
  }

  it('FIX: sends nothing after a refused trim, says why and keeps the edited text - pressed again once the turn is over, it sends', async () => {
    await signedInApp()
    await ask('question twenty')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(refusedTrim)

    await editTo('question twenty', 'question twenty, edited')
    await waitFor(() => expect(h.trimCounts).toEqual([2]))
    expect(await screen.findByText(/Trimming history failed: A turn on this conversation is still running/i)).toBeInTheDocument()
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(h.chatBodies).toHaveLength(1)
    // The edited text is back where it was typed, the turn as it is stored,
    // and the composer is free.
    expect(await screen.findByDisplayValue('question twenty, edited')).toBeInTheDocument()
    expect(screen.getByText('hi')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())

    // That turn is over: the same press trims and sends the edit.
    h.setTrim(landedTrim)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await waitFor(() => expect(h.chatBodies).toHaveLength(2))
    expect(h.trimCounts).toEqual([2, 2])
    expect(h.chatBodies[1].prompt).toBe('question twenty, edited')
    expect(h.chatBodies[1].history).toEqual([])
  })

  it('CONTROL: a trim that lands sends the edit, as before', async () => {
    await signedInApp()
    await ask('question twenty-one')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(landedTrim)

    await editTo('question twenty-one', 'question twenty-one, edited')
    await waitFor(() => expect(h.chatBodies).toHaveLength(2))
    expect(h.trimCounts).toEqual([2])
    expect(h.chatBodies[1].prompt).toBe('question twenty-one, edited')
    expect(h.chatBodies[1].history).toEqual([])
    // The editor closed on the send and stays closed.
    expect(screen.queryByDisplayValue('question twenty-one, edited')).toBeNull()
    expect(await screen.findByText('question twenty-one, edited', { ignore: 'textarea, script, style' })).toBeInTheDocument()
  })

  it('CONTROL: a trim that fails any other way still sends the edit, as before', async () => {
    await signedInApp()
    await ask('question twenty-two')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(() => new Response(JSON.stringify({ detail: 'database is locked' }),
      { status: 500, headers: { 'Content-Type': 'application/json' } }))

    await editTo('question twenty-two', 'question twenty-two, edited')
    await waitFor(() => expect(h.chatBodies).toHaveLength(2))
    expect(h.chatBodies[1].prompt).toBe('question twenty-two, edited')
  })

  // Edit opened again (2026-10-21). The editor's text was seeded once, from
  // the content the bubble mounted with, and the send reset it to that same
  // old content - so after an edit landed, the bubble showed the new text and
  // Edit opened on the one it replaced.
  it('FIX: Edit opened again after an edit that landed shows the edited text', async () => {
    await signedInApp()
    await ask('question twenty-three')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(landedTrim)

    await editTo('question twenty-three', 'question twenty-three, edited')
    await waitFor(() => expect(h.chatBodies).toHaveLength(2))
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
    expect(screen.getByText('question twenty-three, edited', { ignore: 'textarea, script, style' })).toBeInTheDocument()

    await act(async () => { fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]) })
    expect(screen.getByDisplayValue('question twenty-three, edited')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('question twenty-three')).toBeNull()
  })

  it('CONTROL: a refused edit still reopens on the text as typed; Cancel, then Edit, opens on the stored text', async () => {
    await signedInApp()
    await ask('question twenty-four')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(refusedTrim)

    await editTo('question twenty-four', 'question twenty-four, edited')
    await waitFor(() => expect(h.trimCounts).toEqual([2]))
    // The typed text, not the message's - the edit never landed.
    expect(await screen.findByDisplayValue('question twenty-four, edited')).toBeInTheDocument()
    expect(h.chatBodies).toHaveLength(1)

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })) })
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
    await act(async () => { fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]) })
    expect(screen.getByDisplayValue('question twenty-four')).toBeInTheDocument()
  })
})

describe('Edit refused by the chat POST itself (2026-10-22)', () => {
  // The edit's own send can be the refused one: the trim found no stored row
  // to remove (an all-unsaved tail - here a send that never reached the
  // server - or a guest, whose edit skips the trim), and the POST met the
  // one-turn guard: a second tab's turn, or the one a Stop leaves running.
  // By then the transcript had been cut to the edit and the editor had
  // closed, so the refusal gave the text back into an EMPTY composer only -
  // with the next thought already in the box the edited text was gone, and
  // the question it replaced was off the screen with its answer. Now the
  // transcript goes back as it was and the editor reopens on the text; the
  // composer is not written on this path.
  const refusedTurn = () => new Response(
    JSON.stringify({ detail: 'Still answering your last message on this conversation - wait for it to finish, then send again.' }),
    { status: 409, headers: { 'Content-Type': 'application/json' } })
  const landedTrim = () => new Response(
    JSON.stringify({ status: 'ok', deleted: 2, requested: 2 }),
    { status: 200, headers: { 'Content-Type': 'application/json' } })

  async function editTo(from: string, to: string) {
    await act(async () => { fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]) })
    fireEvent.change(screen.getByDisplayValue(from), { target: { value: to } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
  }

  /** An all-unsaved tail: a send that never reached the server leaves its
   *  bubble and the error under it unstored, so an edit of it trims nothing. */
  async function unsavedTurn(text: string) {
    h.setChat(() => Promise.reject(new TypeError('Failed to fetch')))
    await ask(text)
    await waitFor(() => expect(screen.getByText(/Could not reach the backend/i)).toBeInTheDocument())
  }

  /** The next thought, typed into the composer before the edit is sent. */
  function nextThought() {
    const box = screen.getByPlaceholderText(/Message/i) as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'my next thought' } })
    return box
  }

  it('FIX: a no-trim edit the POST refuses reopens the editor on the edited text, over the transcript as it was, and leaves the composer alone', async () => {
    await signedInApp()
    await unsavedTurn('question twenty-five')
    h.setChat(refusedTurn)
    const box = nextThought()

    await editTo('question twenty-five', 'question twenty-five, edited')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/still answering/i))
    // The edited text is back in the editor it was typed in - not the composer,
    // which kept the next thought.
    expect(await screen.findByDisplayValue('question twenty-five, edited')).not.toBe(box)
    expect(box.value).toBe('my next thought')
    // The transcript is as it was before the edit: the error under the
    // question is back on screen. Nothing was trimmed; the edit was posted once.
    expect(screen.getByText(/Could not reach the backend/i)).toBeInTheDocument()
    expect(h.trimCounts).toEqual([])
    expect(h.chatBodies).toHaveLength(2)
    expect(h.chatBodies[1].prompt).toBe('question twenty-five, edited')
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
  })

  it('FIX: a guest edit the POST refuses (no trim for a guest) reopens the editor and keeps the turn - pressed again once the turn is over, it sends', async () => {
    h = installFetch({ guestMode: true })
    render(<App />)
    const guestDoor = await screen.findByRole('button', { name: /Continue as guest/i })
    await act(async () => { fireEvent.click(guestDoor) })
    await screen.findByPlaceholderText(/Message/i)
    await ask('a guest question')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setChat(refusedTurn)
    const box = nextThought()

    await editTo('a guest question', 'a guest question, edited')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/still answering/i))
    expect(await screen.findByDisplayValue('a guest question, edited')).not.toBe(box)
    expect(box.value).toBe('my next thought')
    // The turn is back as it was - its rows were never trimmed.
    expect(screen.getByText('hi')).toBeInTheDocument()
    expect(h.trimCounts).toEqual([])
    expect(h.chatBodies).toHaveLength(2)

    // That turn is over: the same press sends the edit.
    h.setChat(() => new Response(sseStream([tok('again'), DONE]).stream, { status: 200 }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await waitFor(() => expect(h.chatBodies).toHaveLength(3))
    expect(h.chatBodies[2].prompt).toBe('a guest question, edited')
    expect(h.chatBodies[2].history).toEqual([])
    expect(h.trimCounts).toEqual([])
  })

  it('FIX: after a trim that landed, a POST the server refuses puts the turn back unstored and reopens the editor - a Regenerate then trims nothing more', async () => {
    // The race: the trim found the slot free and the POST did not (a second
    // tab's turn began between them). The trimmed rows are gone from the
    // server, so the turn comes back on screen as unstored - counted as stored,
    // the next trim would reach one turn too far back.
    await signedInApp()
    await ask('question twenty-six')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    h.setTrim(landedTrim)
    h.setChat(refusedTurn)
    const box = nextThought()

    await editTo('question twenty-six', 'question twenty-six, edited')
    await waitFor(() => expect(h.trimCounts).toEqual([2]))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/still answering/i))
    expect(await screen.findByDisplayValue('question twenty-six, edited')).not.toBe(box)
    expect(box.value).toBe('my next thought')
    expect(h.chatBodies).toHaveLength(2)
    // The answer is back on screen, with no thumbs: an unstored answer offers none.
    expect(screen.getByText('hi')).toBeInTheDocument()
    expect(screen.queryByTitle('Good response')).toBeNull()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })) })
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
    h.setChat(() => new Response(sseStream([tok('again'), DONE]).stream, { status: 200 }))
    await clickRegenerate()
    await waitFor(() => expect(h.chatBodies).toHaveLength(3))
    expect(h.chatBodies[2].prompt).toBe('question twenty-six')
    expect(h.chatBodies[2].history).toEqual([])
    expect(h.trimCounts).toEqual([2])
  })

  it('CONTROL: a no-trim edit the server accepts lands as before, and the editor stays closed', async () => {
    await signedInApp()
    await unsavedTurn('question twenty-seven')
    h.setChat(() => new Response(sseStream([tok('hi'), DONE]).stream, { status: 200 }))

    await editTo('question twenty-seven', 'question twenty-seven, edited')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    expect(h.trimCounts).toEqual([])
    expect(h.chatBodies).toHaveLength(2)
    expect(h.chatBodies[1].prompt).toBe('question twenty-seven, edited')
    expect(h.chatBodies[1].history).toEqual([])
    expect(screen.queryByDisplayValue('question twenty-seven, edited')).toBeNull()
    expect(screen.getByText('question twenty-seven, edited', { ignore: 'textarea, script, style' })).toBeInTheDocument()
    // The error the edit replaced went with the question it hung under.
    expect(screen.queryByText(/Could not reach the backend/i)).toBeNull()
    // Both rows stored now: a Regenerate trims two.
    await clickRegenerate()
    await waitFor(() => expect(h.trimCounts).toEqual([2]))
  })
})

describe('leaving a conversation mid-answer', () => {
  it('a chunk already in flight lands nowhere once the conversation is left', async () => {
    h.setSessions([{ session: 'older-session', first_message: 'The older chat' }])
    await signedInApp()
    // A body that ignores the abort: the chunk that was already on its way
    // when the user left. The stream ticket, not the abort, is what keeps it
    // out of the conversation they went to.
    const { stream, push } = sseStream([], { hang: true })
    h.setChat(() => new Response(stream, { status: 200 }))
    await ask('question eleven')
    await act(async () => { push(tok('EARLYTOKEN')) })
    await waitFor(() => expect(screen.getByText(/EARLYTOKEN/)).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByText('The older chat')) })
    await act(async () => { push(tok('LATETOKEN')) })

    expect(screen.queryByText(/LATETOKEN/)).toBeNull()
    expect(screen.queryByText(/EARLYTOKEN/)).toBeNull()
    // Leaving released the composer; the abandoned stream holds nothing.
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
  })

  it('a stream whose FIRST chunk arrives after the visitor left writes nothing', async () => {
    h.setSessions([{ session: 'older-session', first_message: 'The older chat' }])
    await signedInApp()
    const { stream, push } = sseStream([], { hang: true })
    h.setChat(() => new Response(stream, { status: 200 }))
    await ask('question fourteen')
    await waitFor(() => expect(screen.getByTitle('Stop generation')).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByText('The older chat')) })
    await act(async () => {
      push(`data: ${JSON.stringify({ context_warning: true })}\n\n`)
      push(tok('LATEFIRST'))
    })

    // The bubble's creation was the one write without the ticket check: it
    // appended an empty assistant bubble - with a Regenerate under it - to
    // the conversation the visitor went to. The context banner likewise.
    expect(screen.queryByText(/LATEFIRST/)).toBeNull()
    expect(screen.queryByRole('button', { name: /regenerate/i })).toBeNull()
    expect(screen.queryByText(/getting long/i)).toBeNull()
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
  })

  it('the abandoned stream writes no Stopped notice into the next conversation', async () => {
    h.setSessions([{ session: 'older-session', first_message: 'The older chat' }])
    await signedInApp()
    const s = openStream()
    h.setChat(init => s.respond(init))
    await ask('question twelve')
    await act(async () => { s.push(tok('EARLYTOKEN')) })
    await waitFor(() => expect(screen.getByText(/EARLYTOKEN/)).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByText('The older chat')) })

    // The abort reaches the reader as an AbortError; the branch that says
    // "Stopped" must see that the stream is no longer the conversation's.
    await waitFor(() => expect(screen.getByTitle('Send message')).toBeInTheDocument())
    expect(screen.queryByText(/Stopped/i)).toBeNull()
    expect(screen.queryByText(/was not saved/i)).toBeNull()
  })

  it('a history read that lands after the conversation changed is dropped', async () => {
    h.setSessions([{ session: 'older-session', first_message: 'The older chat' }])
    const slow = held<Response>()
    h.setHistory('older-session', () => slow.promise)
    await signedInApp()

    const older = await screen.findByText('The older chat')
    await act(async () => { fireEvent.click(older) })
    // Its read is still out when the visitor moves on.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New Chat/i })) })
    await act(async () => {
      slow.release(new Response(JSON.stringify({ messages: [
        { role: 'user', content: 'OLDQUESTION' }, { role: 'assistant', content: 'OLDANSWER' }] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }))
    })

    // Painted, it would be posted as the new conversation's history too.
    expect(screen.queryByText('OLDQUESTION')).toBeNull()
  })

  it('New Chat starts a conversation and deletes none', async () => {
    await signedInApp()
    await ask('question thirteen')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New Chat/i })) })

    // The old conversation is left, not erased: History is how you get back.
    expect(h.deletes).toEqual([])
    expect(screen.queryByText('question thirteen', { ignore: 'textarea, script, style' })).toBeNull()
  })
})

describe('identity transitions (the 2026-10-02 defensive read)', () => {
  it('the credentials leave storage the moment Sign out is pressed', async () => {
    const pending = held<Response>()
    h.setLogout(() => pending.promise)
    await signedInApp()

    await act(async () => { fireEvent.click(screen.getAllByRole('button', { name: /Sign out/i })[0]) })

    // The logout POST is still out, and no token is left for anything else to
    // send meanwhile - a guest opened in this window used to chat as the
    // outgoing account.
    expect(localStorage.getItem('az_jwt_token')).toBeNull()
    expect(localStorage.getItem('az_jwt_refresh')).toBeNull()
    // The POST itself still carried the outgoing token.
    const calls = (globalThis.fetch as unknown as { mock: { calls: [unknown, RequestInit?][] } }).mock.calls
    const logout = calls.find(([u]) => String(u).includes('/api/auth/logout'))
    expect((logout?.[1]?.headers as Record<string, string>).Authorization).toBe('Bearer test-token')
    await act(async () => { pending.release(new Response('{}', { status: 200 })) })
  })

  it("a guest's unsent draft does not follow them into the account they sign into", async () => {
    h = installFetch({ guestMode: true })
    render(<App />)
    const guestDoor = await screen.findByRole('button', { name: /Continue as guest/i })
    await act(async () => { fireEvent.click(guestDoor) })
    const box = await screen.findByPlaceholderText(/Message/i)
    fireEvent.change(box, { target: { value: 'A HALF-WRITTEN DRAFT' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Sign in$/i })) })
    fireEvent.change(await screen.findByPlaceholderText('Username'), { target: { value: 'tester' } })
    fireEvent.change(screen.getByPlaceholderText('Password'), { target: { value: 'a-password' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Sign in$/i })) })

    const accountBox = await screen.findByPlaceholderText(/Message/i) as HTMLTextAreaElement
    expect(accountBox.value).toBe('')
  })

  it('a regenerate still trimming when the account signs out never re-sends', async () => {
    await signedInApp()
    await ask('question fifteen')
    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    const trim = held<Response>()
    h.setTrim(() => trim.promise)

    await clickRegenerate()
    await act(async () => { fireEvent.click(screen.getAllByRole('button', { name: /Sign out/i })[0]) })
    await act(async () => {
      trim.release(new Response(JSON.stringify({ status: 'ok' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }))
    })

    // Only the original turn was ever posted - never the signed-out account's
    // turn again, with or without its token.
    expect(h.chatBodies).toHaveLength(1)
  })
})

describe('deleting a conversation from the History list (2026-10-03)', () => {
  // The row used to go whatever the server said, so a refused delete came back
  // on the next poll as though the click had never happened.
  it('a refused delete keeps the conversation listed and says why', async () => {
    h.setSessions([{ session: 'older-session', first_message: 'The older chat' }])
    h.setDelete(() => new Response(JSON.stringify({ detail: 'nope' }), {
      status: 500, headers: { 'Content-Type': 'application/json' },
    }))
    await signedInApp()
    await screen.findByText('The older chat')
    fireEvent.click(screen.getByRole('button', { name: 'Delete conversation' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(h.deletes).toHaveLength(1))
    expect(await screen.findByText(/Deleting the conversation failed: nope/)).toBeInTheDocument()
    expect(screen.getByText('The older chat')).toBeInTheDocument()
  })

  it('an accepted delete takes it off the list', async () => {
    h.setSessions([{ session: 'older-session', first_message: 'The older chat' }])
    await signedInApp()
    await screen.findByText('The older chat')
    fireEvent.click(screen.getByRole('button', { name: 'Delete conversation' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByText('The older chat')).toBeNull())
    expect(h.deletes).toHaveLength(1)
    expect(h.deletes[0]).toContain('/api/history/older-session')
  })
})
