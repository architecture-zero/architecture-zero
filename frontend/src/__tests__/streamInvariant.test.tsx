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
      return json({ status: 'ok' })
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
