// The stream: the transcript, the draft, the notices and the one-turn-at-a-time
// guards, with the SSE reader and every path that starts or stops an answer
// (send, Stop, Regenerate, Edit). Carved out of App.tsx body for body in the
// App split (2026-10-11); the shell owns identity, the view and the sessions,
// and reads this hook once per render.
import { useState, useEffect, useRef } from 'react'
import type { MutableRefObject } from 'react'
import type { ToolCall, ChatMessage } from './types'
import { API, authHeaders } from '../api'
import { actionError, emitError } from '../errorSurface'

export const GUEST_TURN_LIMIT = 10

// The two refusals the server makes before anything is stored or billed, as
// the line under the transcript says them. The 413's own `detail` is preferred
// - it names the figure and the way out - and this is the fallback for a 413
// that carries none (a proxy in front of the app answers with an HTML page).
const TURN_NOTICE = 'Still answering your last message on this conversation. Wait for it to finish, then send again.'
const TOO_LARGE_NOTICE = 'That message was too large to send. Shorten it, or start a new chat.'

// What the stream reads from the shell each render: the conversation it
// belongs to, who is asking, which lane, the model and retrieval choices the
// request carries, and the identity ticket a regenerate checks after its trim.
interface StreamContext {
  sessionId: string
  isGuest: boolean
  helpMode: boolean
  modelTouched: boolean
  model: string
  ragKnown: boolean
  ragTouched: boolean
  useRag: boolean
  identityGen: MutableRefObject<number>
}

export function useChatStream({ sessionId, isGuest, helpMode, modelTouched, model, ragKnown, ragTouched, useRag, identityGen }: StreamContext) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  // The one-line notice under the transcript when the server refused a turn:
  // this conversation is still answering (409, one turn at a time), or the
  // request was too large to send (413, the size bound). Cleared on the next
  // send and on leaving the conversation (abandonStream); never an error
  // bubble, because nothing was written and nothing was billed.
  const [turnNotice, setTurnNotice] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  // TRUE for the whole stream, not just until the first token. `loading` flips
  // false the moment anything arrives (that is what swaps the thinking dots for
  // text), and it was ALSO the only guard on send / regenerate / edit / the
  // textarea / the send-stop button flip. So from the first token onward the
  // composer re-enabled mid-answer, Stop turned back into Send - making a long
  // generation unstoppable, which is the one thing Stop exists for - and a
  // second request could be started into the same message slot.
  const [streaming, setStreaming] = useState(false)
  const [contextWarning, setContextWarning] = useState(false)
  const [contextSummarized, setContextSummarized] = useState(false)

  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  // STREAM IDENTITY. A captured bubble index alone is not enough: it only
  // notices that the array SHRANK, never that it was REPLACED by a different
  // conversation, and it says nothing about WHICH stream a late write belongs
  // to. Both gaps were real - an in-flight answer kept writing into the
  // conversation the user switched TO, and the first of two overlapping streams
  // cleared the busy flag for the second on its way out.
  //
  // Every stream takes a ticket. Only the holder of the current ticket may
  // write to messages or clear the guards; an abandoned stream is ignored in
  // silence, which is what an abandoned stream deserves.
  const streamSeq = useRef(0)
  const activeStream = useRef<number | null>(null)
  // The newest ticket a leaving path has abandoned. Unlike activeStream, a
  // stream that simply FINISHES does not move it - which is what a check made
  // inside a state updater needs. React may run an updater after the stream's
  // own finally has retired its ticket (any render that lands late), and a
  // check on activeStream there dropped the stream's writes: measured, the
  // whole answer's text and both of the turn's "this row is stored" flags.
  const abandonedThrough = useRef(0)
  // sessionId as a ref so a running stream reads the CURRENT conversation
  // rather than the one captured in its closure.
  const sessionIdRef = useRef(sessionId)

  // Leaving a conversation ABANDONS its stream. Without this the in-flight
  // answer kept running, kept holding the guards, and (before the identity
  // check in sendCore) wrote into whichever transcript was loaded next.
  //
  // DECLARED IN THIS HOOK, WHICH THE SHELL CALLS ABOVE ITS EARLY RETURNS, AND
  // THAT PLACEMENT IS THE FIX.
  // It used to sit below `if (view === 'admin') return ...`, so the statement
  // initialising it was never reached while the Admin panel was open - and
  // handleLogout, which the panel's own Sign out button calls, hit the
  // temporal dead zone and threw a ReferenceError before it cleared a single
  // token. The change that added abandonStream() to sign-out to stop a stream
  // leaking across accounts is what broke sign-out on the privileged surface.
  const abandonStream = () => {
    abandonedThrough.current = streamSeq.current
    activeStream.current = null
    abortRef.current?.abort()
    abortRef.current = null
    setStreaming(false)
    setLoading(false)
    // The refusal line belongs to the conversation being left. Every caller
    // of this is a leaving path, and "still answering your last message on
    // this conversation" under a different conversation is a false statement.
    setTurnNotice(null)
  }

  useEffect(() => { sessionIdRef.current = sessionId }, [sessionId])

  // Core SSE stream handler - prompt is already in messages state when called
  const sendCore = async (prompt: string, historyForRequest: Array<{ role: string; content: string }>) => {
    const controller = new AbortController()
    abortRef.current = controller
    const myStream = ++streamSeq.current
    activeStream.current = myStream
    // The conversation this answer belongs to. If it changes underneath us the
    // stream would be writing into somebody else's transcript, which is worse
    // than losing the answer.
    const myConversation = sessionId
    const mine = () => activeStream.current === myStream
      && sessionIdRef.current === myConversation
    // The check for INSIDE a state updater, which React may run after this
    // stream's finally has retired its ticket: only leaving the conversation
    // makes a stream's writes stale, and every leaving path abandons it (see
    // abandonedThrough). mine() stays the check at the moment of writing.
    const live = () => myStream > abandonedThrough.current
    // The prompt's row is stored: clear the flag on the most recent user
    // bubble, which from here counts as a stored row. Two callers - the 200
    // below, and a Stop (the AbortError branch, 2026-10-21).
    const markPromptStored = (prev: ChatMessage[]) => {
      if (!live()) return prev
      const next = [...prev]
      for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].role === 'user') {
          if (next[i].ephemeral) next[i] = { ...next[i], ephemeral: false }
          break
        }
      }
      return next
    }
    setStreaming(true)
    // The index of THIS stream's assistant bubble, captured when it is created.
    // Every write below used to target `next[next.length - 1]` - whatever
    // happened to be last at that instant - and to write its own accumulated
    // string. With two streams alive that meant the older one dumped its whole
    // answer into the newer one's bubble and abandoned its own: whole answers
    // swapped places rather than interleaving, which is exactly why it read as
    // a harmless duplicate. The guard above should now prevent a second stream,
    // and this makes the write correct even if one ever gets through.
    let slot = -1
    // Hoisted out of the try: the AbortError handler needs to know whether an
    // assistant bubble was ever created, and a stop before the first token is
    // exactly the case where it was not.
    let assistantStarted = false

    try {
      const res = await fetch(`${API}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        // OMIT use_rag when this client was never told what the default is.
        // /api/config is authenticated, so a guest's boot read never happens and
        // useRag is only the hardcoded initial true - which would override an
        // operator who turned retrieval off, for exactly the visitors who have
        // no way to know. Omitted, the server applies its own configured
        // default; a signed-in client that HAS read the config still sends the
        // explicit value, including whatever the user toggled.
        body: JSON.stringify({
          prompt, history: historyForRequest, session_id: sessionId,
          // SEND A MODEL ONLY IF THE USER PICKED ONE. `model` is seeded from
          // /api/config (chat_model_effective since 2026-10-02, default_model
          // before), which is never empty, so this client
          // always sent an explicit model - and chat.py applies the operator's
          // `chat_model` pin only `if not request.model`. The pin was therefore
          // inert for every signed-in user, while the header badge and footer
          // rendered `chat_model_effective` (the pin) as the model that
          // answered. The instance named one model and used another. Omitting
          // the field lets the server's own resolution chain run, which is what
          // the badge is already reporting.
          ...(modelTouched ? { model } : {}),
          // Sent when the server told us its default (so we can echo the
          // user's choice), OR when the user has actually touched the toggle -
          // an explicit choice must reach the server even from a guest whose
          // client was never told the default. Omitted only when this client
          // has no basis for an opinion, which is when the server should decide.
          ...(ragKnown || ragTouched ? { use_rag: useRag } : {}),
          // Help mode: the product's own help pages, retrieval forced on and
          // never a peer. Last, so it wins over the toggle's value above.
          ...(helpMode ? { department: 'help', use_rag: true, use_peers: false } : {}),
        }),
        signal: controller.signal,
      })

      // The two refusals made before anything is stored or billed. Each is a
      // state, not an error: take back the ephemeral user bubble this send
      // added, put the draft back in the box, say why under the transcript. No
      // retry loop - the user sends again. mine() for the same reason every
      // other branch has it.
      const takeBack = (notice: string) => {
        if (!mine()) return
        setMessages(prev => {
          const last = prev[prev.length - 1]
          return last && last.role === 'user' && last.ephemeral && last.content === prompt
            ? prev.slice(0, -1) : prev
        })
        // Into an EMPTY box only. The box stays live while a request is in
        // flight, so the user may have started the next thought; writing the
        // old draft back unconditionally erased it.
        setInput(cur => (cur.trim() ? cur : prompt))
        setTurnNotice(notice)
        setLoading(false)
        // The draft came back without a keystroke, so onInput never sized the
        // box for it - and a 413's draft is by definition a long one.
        setTimeout(() => {
          const box = textareaRef.current
          if (box) { box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight, 180) + 'px' }
        }, 0)
      }
      if (res.status === 409) {
        // ONE TURN AT A TIME: another turn on this conversation is still
        // answering (a second tab, a resend, a tap that beat the Stop swap).
        takeBack(TURN_NOTICE)
        return
      }
      if (res.status === 413) {
        // THE SIZE BOUND: this request carried more than one turn may - the
        // message plus the conversation sent back with it - or its body
        // crossed the byte ceiling, and it was refused before anything was
        // scanned or stored. The server's own sentence is the notice: it names
        // the figure and the way out (shorten the message, or start a new
        // chat). Before this branch the generic one below showed it as an
        // assistant bubble and the draft was gone.
        const refusal = await res.json().catch(() => ({}))
        takeBack(typeof refusal.detail === 'string' && refusal.detail ? refusal.detail : TOO_LARGE_NOTICE)
        return
      }
      if (!res.ok) {
        // Surface the server's own message (guest limit, widget-only guest scope,
        // guest mode off) instead of masking it as a connectivity failure.
        const errData = await res.json().catch(() => ({}))
        const fallback = res.status === 429
          ? 'Guest limit reached. Sign in to continue.'
          : `Error: the server returned ${res.status}.`
        // mine(), like the other two error appends. This was the third one and
        // it was the one still missing the guard, so a rejected send from an
        // abandoned stream wrote its error into whatever conversation was on
        // screen by then and cleared that conversation's loading state.
        if (mine()) {
          setMessages(prev => [...prev, { role: 'assistant', content: errData.detail || fallback, ephemeral: true }])
          setLoading(false)
        }
        return
      }
      if (!res.body) throw new Error('No response body')

      // THE USER ROW IS NOW STORED. The route writes it before it returns the
      // streaming response, so an OK status is proof the write happened - and
      // this is the only point at which the client can know it.
      setMessages(markPromptStored)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      // Carry buffer: a `data:` line can straddle network chunks, and
      // splitting each chunk in isolation silently drops the straddled line's
      // tokens. The answer still renders, just missing pieces - which is why
      // this is easy to ship and hard to notice.
      let buf = ''
      let assistantMsg = ''
      let toolCalls: ToolCall[] = []
      let sources: string[] = []
      // Did the server emit an error event? The read loop ends normally in that
      // case too - the generator yields the error and stops - so reaching the
      // end of the loop is NOT proof the answer completed. The assistant row is
      // written only on the clean path, so this flag is what decides whether
      // the bubble stops being ephemeral.
      let streamFailed = false

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const payload = line.slice(6)
          if (payload === '[DONE]') break
          try {
            const data = JSON.parse(payload)

            if (data.error) {
              streamFailed = true
              if (!assistantStarted) {
                if (mine()) {
                  setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${data.error}`, ephemeral: true }])
                  setLoading(false)
                }
                assistantStarted = true
              } else if (mine()) {
                // AFTER tokens have flowed this used to `continue` in silence,
                // so a provider dying mid-answer rendered as a complete answer
                // that just stopped early.
                //
                // The comment that used to sit here said the backend emits this
                // event "precisely when an exception escapes mid-generation,
                // which is always after tokens". Both halves were wrong. The
                // route's try opens before the first provider call, every
                // adapter raises before yielding text on a connect-time
                // failure, and `assistantStarted` flips on data.sources - which
                // the route yields ABOVE the try. So on any retrieval turn a
                // refused connection or an unpulled model lands here with
                // assistantMsg still empty, which is the common first-run state,
                // not the rare one.
                //
                // The notice goes in its own field, NOT concatenated into
                // content. Content is what send() posts back as model context,
                // so appending the notice there taught the model it had said
                // "the provider failed mid-response" - and marking the bubble
                // ephemeral to fix the trim would then have thrown away the
                // genuine partial answer along with it. Separate fields let the
                // trim, the context and the display each be correct.
                setMessages(prev => {
                  const next = [...prev]
                  if (slot < 0 || slot >= next.length) return prev
                  next[slot] = {
                    ...next[slot],
                    content: assistantMsg,
                    toolCalls,
                    sources,
                    // Say it was not saved. The Stop notice tells the user that
                    // ("This partial answer was not saved."); this one did not,
                    // and it is the same fact - no assistant row is written when
                    // the generator raises. A partial answer that silently
                    // vanishes on the next reload is the thing to warn about.
                    notice: assistantMsg
                      ? 'The answer stopped early: the provider failed mid-response. This partial answer was not saved.'
                      : 'The provider failed before any of the answer arrived. Nothing was saved.',
                  }
                  return next
                })
                emitError('The answer stopped early - the provider failed mid-response.')
              }
              continue
            }

            if (!assistantStarted && (data.sources || data.tool_call || data.token)) {
              assistantStarted = true
              // mine(), like every other write. The bubble's creation was the
              // one write without it: a stream whose FIRST event arrived after
              // the visitor had left appended an empty assistant bubble to the
              // conversation they went to, and cleared that conversation's
              // loading state. Left unset, `slot` keeps every later write of an
              // abandoned stream out as well.
              if (mine()) {
                setMessages(prev => {
                  if (!live()) return prev
                  slot = prev.length            // this stream's own bubble, for good
                  // EPHEMERAL UNTIL THE STREAM COMPLETES CLEANLY. The assistant
                  // row is written after the generator finishes, so until then
                  // no row exists: an abort cancels the generator before the
                  // write, and a mid-stream provider failure escapes to a
                  // handler that has no save and no finally. This bubble was
                  // the single largest hole in the stored-row model - it is
                  // unstored on every abort and every provider death, and it
                  // was never flagged, because the flag was being set on notice
                  // bubbles rather than on bubbles with no row.
                  return [...prev, { role: 'assistant', content: '', toolCalls: [], sources: [], ephemeral: true, help: helpMode }]
                })
                setLoading(false)
              }
            }

            if (data.sources) {
              sources = data.sources
              setMessages(prev => {
                if (!live()) return prev
                const next = [...prev]
                if (slot < 0 || slot >= next.length) return prev
                // Spread, do not rebuild. A literal drops `ephemeral` and
                // `notice` on every token, which would silently re-mark an
                // unstored bubble as stored mid-stream.
                next[slot] = { ...next[slot], content: assistantMsg, toolCalls, sources }
                return next
              })
            }

            if (data.tool_call) {
              toolCalls = [...toolCalls, data.tool_call]
              setMessages(prev => {
                if (!live()) return prev
                const next = [...prev]
                if (slot < 0 || slot >= next.length) return prev
                // Spread, do not rebuild. A literal drops `ephemeral` and
                // `notice` on every token, which would silently re-mark an
                // unstored bubble as stored mid-stream.
                next[slot] = { ...next[slot], content: assistantMsg, toolCalls, sources }
                return next
              })
            }

            // The banner belongs to this stream's conversation, so mine() too.
            if (data.context_warning && mine()) setContextWarning(true)
            if (data.context_summarized && mine()) setContextSummarized(true)

            if (data.token) {
              assistantMsg += data.token
              setMessages(prev => {
                if (!live()) return prev
                const next = [...prev]
                if (slot < 0 || slot >= next.length) return prev
                // Spread, do not rebuild. A literal drops `ephemeral` and
                // `notice` on every token, which would silently re-mark an
                // unstored bubble as stored mid-stream.
                next[slot] = { ...next[slot], content: assistantMsg, toolCalls, sources }
                return next
              })
            }
          } catch { /* malformed chunk, skip */ }
        }
      }

      // THE STREAM COMPLETED CLEANLY, so the assistant row was written and this
      // bubble stops being ephemeral. Guarded on streamFailed because the read
      // loop ALSO ends normally after an error event, and on that path no row
      // exists - which is exactly the case that made Regenerate delete the
      // previous turn's answer.
      if (!streamFailed && assistantStarted && mine()) {
        setMessages(prev => {
          const next = [...prev]
          if (slot < 0 || slot >= next.length) return prev
          next[slot] = { ...next[slot], ephemeral: false }
          return next
        })
      }
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') {
        // A STOP COUNTS THE PROMPT AS STORED, the way the route counts it
        // (2026-10-21). chat.py writes the user row past every refusal and
        // just before it answers, and nothing before that write asks whether
        // the caller is still there - so a Stop that lands before the 200,
        // while the route is still retrieving, leaves the row behind all the
        // same. Left ephemeral, the next Regenerate trimmed nothing and sent
        // the prompt again, and the server held it twice. After the 200 this
        // is a no-op: the flag is already clear. fetch cannot say whether the
        // request ever left, so this is the route's common case, not proof: a
        // Stop that beat the request out or landed on a refusal is counted one
        // row high, and a Regenerate whose trim reaches the server before the
        // route's late write takes the row before it.
        if (mine()) setMessages(markPromptStored)
        // Stopping BEFORE the first token means no assistant bubble was ever
        // created, so the transcript ended on the user's question with nothing
        // saying it had been cancelled - and Regenerate refuses to run unless
        // the last message is an assistant one, so there was no way back to it
        // either. Say what happened, in the shape the rest of the UI can act on.
        if (!assistantStarted && mine()) {
          setMessages(prev => [...prev, { role: 'assistant', content: '_Stopped before the answer started._', ephemeral: true }])
        } else if (mine()) {
          // Stopped AFTER tokens arrived. The partial text is real and stays,
          // but the abort cancelled the generator before the row was written,
          // so the bubble keeps its ephemeral flag - it was created with one
          // and nothing on this path clears it. Say so on the bubble, because
          // "this is not in your history" is not something a partial answer
          // communicates on its own.
          setMessages(prev => {
            const next = [...prev]
            if (slot < 0 || slot >= next.length) return prev
            next[slot] = { ...next[slot], notice: 'Stopped. This partial answer was not saved.' }
            return next
          })
        }
        // mine() here too: an abandoned stream's abort lands after the visitor
        // may have sent in the next conversation, and clearing `loading` there
        // dropped the new answer's thinking dots while it was still pending.
        if (mine()) setLoading(false)
        return
      }
      // mine() for the same reason the branch above has it: an abandoned
      // stream must not write its failure into whichever conversation is on
      // screen now, nor clear that conversation's loading state.
      if (mine()) {
        setMessages(prev => [...prev, { role: 'assistant', content: 'Error: Could not reach the backend.', ephemeral: true }])
        setLoading(false)
      }
    } finally {
      // Only retract the controller if it is still OURS. Clearing it blind let
      // an older stream's finally null a newer stream's controller, which left
      // Stop inert for the rest of the conversation once two had ever overlapped.
      // Only the CURRENT stream retracts state. Clearing unconditionally let
      // the older of two overlapping streams turn Stop back into Send while the
      // newer one was still writing - exactly the state the guard exists to
      // prevent.
      if (abortRef.current === controller) abortRef.current = null
      if (activeStream.current === myStream) {
        activeStream.current = null
        setStreaming(false)
        setLoading(false)
      }
    }
  }

  const guestTurnCount = isGuest ? messages.filter(m => m.role === 'user').length : 0
  const guestAtLimit = isGuest && guestTurnCount >= GUEST_TURN_LIMIT
  // One name for "a request is in flight", covering both the pre-first-token
  // wait and the stream itself. Every entry point guards on this.
  const busy = loading || streaming

  const send = async (text?: string) => {
    const prompt = (text || input).trim()
    if (!prompt || busy || guestAtLimit) return
    setInput('')
    setTurnNotice(null)
    setContextWarning(false)
    setContextSummarized(false)
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    // Local-only bubbles are UI, not conversation. Posting "Stopped before the
    // answer started." or an error string back as an assistant turn teaches the
    // model it said something it never said.
    const history = messages.filter(m => !m.ephemeral).map(m => ({ role: m.role, content: m.content }))
    // EPHEMERAL UNTIL THE SERVER ACCEPTS IT. Every rejection gate in the chat
    // route - injection screen, origin check, expired session, guest limit,
    // budget - runs BEFORE the user row is written, so a refused send leaves a
    // user bubble on screen with no row behind it. Counted as stored, the next
    // Regenerate trims one row too far back and takes the previous turn's
    // answer with it. sendCore clears the flag once the response is OK, or on
    // a Stop (the route stores a prompt whose caller has left).
    setMessages(prev => [...prev, { role: 'user', content: prompt, ephemeral: true }])
    setLoading(true)
    await sendCore(prompt, history)
  }

  const stopGeneration = () => {
    abortRef.current?.abort()
  }

  const regenerate = async () => {
    if (busy || messages.length < 2) return
    // CLAIM THE GUARD BEFORE AWAITING. The history trim below is a network
    // round-trip, and until 5d80283's successor neither loading nor streaming
    // was set across it - so `busy` stayed false for ~100ms and a Ctrl+Enter in
    // that window started a second concurrent stream. Set here, and every exit
    // path below must clear it.
    setLoading(true)
    // Both early exits happen AFTER the guard was claimed, so they have to
    // give it back or the composer stays dead with no stream running.
    if (messages[messages.length - 1].role !== 'assistant') { setLoading(false); return }
    const lastUserMsg = messages[messages.length - 2]
    if (lastUserMsg.role !== 'user') { setLoading(false); return }

    const truncated = messages.slice(0, -2)
    const history = truncated.filter(m => !m.ephemeral).map(m => ({ role: m.role, content: m.content }))
    const prompt = lastUserMsg.content

    // COUNT STORED ROWS, NOT BUBBLES. The tail endpoint deletes N rows by id
    // with no role awareness, so sending a client message count deletes one row
    // too many whenever a local-only bubble is in the tail - and one row too far
    // back is the PREVIOUS turn's answer, gone permanently with nothing shown.
    const trimCount = messages.slice(-2).filter(m => !m.ephemeral).length
    const mySession = sessionId
    // The identity this regenerate belongs to. sessionIdRef alone catches a
    // conversation switch only after React has committed it, and a password
    // change (a timer) or a sign-in (a fetch callback) can land before that -
    // the identity ticket moves synchronously on every transition.
    const myIdentity = identityGen.current
    // GUESTS SKIP THE TRIM. A guest holds no token, the tail route depends on
    // get_current_user and 401s at route level, and actionError maps any 401 to
    // the sticky, dismiss-less "Session expired" banner - raised at someone who
    // never signed in. The feedback control already withholds itself from
    // guests with that exact reasoning; regenerate and edit were the two
    // siblings that never got the guard. Nothing is lost by skipping: a guest's
    // rows are anonymous and they cannot read them back.
    if (trimCount > 0 && !isGuest) {
      const trim = fetch(`${API}/api/history/${mySession}/tail?count=${trimCount}`, {
        method: 'DELETE', headers: authHeaders(),
      })
      const trimErr = await actionError(trim, 'Trimming history')
      if (trimErr) emitError(trimErr) // proceed - regeneration still works, server history may duplicate
      // REFUSED, NOT FAILED (2026-10-21): the route answers 409 while a turn
      // on this conversation still holds its slot - the one a Stop leaves
      // running until its late write. Nothing was trimmed, so sending now
      // would store the prompt a second time behind the one still stored.
      // Stop here with the turn as it was; the toast says to wait, and
      // Regenerate works again once that turn is over.
      if ((await trim.catch(() => null))?.status === 409) {
        if (activeStream.current === null) setLoading(false)   // see the leaving path below
        return
      }
    }
    // The trim was a network round-trip and nothing disables the sidebar during
    // it, so the user can be in a different conversation by now. Writing into it
    // would put this conversation's turn in that one - and post it there too.
    if (sessionIdRef.current !== mySession || identityGen.current !== myIdentity) {
      // SAY IT. The rows the trim removed are already gone and nothing on this
      // path re-sends them, so bailing in silence looked like a no-op while a
      // turn had actually been deleted.
      emitError('You left that conversation while it was regenerating, so the turn was not re-sent.')
      // Leaving already released the guard (abandonStream). Clearing it again
      // here hid the thinking dots of an answer already started in the
      // conversation the visitor went to - only release it if nothing runs.
      if (activeStream.current === null) setLoading(false)
      return
    }

    setContextWarning(false)
    setContextSummarized(false)
    setMessages([...truncated, { role: 'user', content: prompt, ephemeral: true }])
    await sendCore(prompt, history)
  }

  // false = refused now (a stream is in flight): the editor keeps the draft.
  // The promise answers false when the server refused the trim the edit needs
  // (409, see below) and nothing was sent: the editor takes the draft back.
  const editAndRegenerate = (msgIndex: number, newContent: string): false | Promise<boolean> => {
    // Refusal is REPORTED, not silent - the caller keeps the user's draft.
    if (busy) {
      emitError('Still answering - stop the current reply before editing.')
      return false
    }
    return _editAndRegenerate(msgIndex, newContent)
  }

  const _editAndRegenerate = async (msgIndex: number, newContent: string): Promise<boolean> => {
    // Same await window as regenerate: claim the guard before the trim.
    setLoading(true)
    const truncated = messages.slice(0, msgIndex)
    // Stored rows only - see regenerate above. `messages.length - msgIndex`
    // counted local bubbles against server rows and orphaned real ones.
    const countToRemove = messages.slice(msgIndex).filter(m => !m.ephemeral).length
    const history = truncated.filter(m => !m.ephemeral).map(m => ({ role: m.role, content: m.content }))
    const mySession = sessionId
    const myIdentity = identityGen.current   // see regenerate above

    // Guests skip the trim - see regenerate above for why.
    if (countToRemove > 0 && !isGuest) {
      const trim = fetch(`${API}/api/history/${mySession}/tail?count=${countToRemove}`, {
        method: 'DELETE', headers: authHeaders(),
      })
      const trimErr = await actionError(trim, 'Trimming history')
      if (trimErr) emitError(trimErr) // any other failure proceeds - the edit still sends, server history may duplicate
      // REFUSED, NOT FAILED (2026-10-21) - regenerate's rule above, which Edit
      // was still missing: nothing was trimmed, so sending now would land the
      // edited prompt as an extra row after the very rows the trim was meant
      // to remove. Nothing is sent and the transcript stays as stored; the
      // toast carries the route's words, the editor gets the text back
      // (Message's submitEdit), and Send works again once that turn is over.
      if ((await trim.catch(() => null))?.status === 409) {
        if (activeStream.current === null) setLoading(false)   // see regenerate
        return false
      }
    }
    if (sessionIdRef.current !== mySession || identityGen.current !== myIdentity) {
      // Wider blast radius than regenerate: countToRemove reaches back to the
      // edited message, and submitEdit has already closed the editor and
      // discarded the draft. Silence here lost an arbitrary number of turns
      // with nothing on screen to say so.
      emitError('You left that conversation while the edit was sending, so it was not re-sent.')
      if (activeStream.current === null) setLoading(false)   // see regenerate
      return true
    }

    setContextWarning(false)
    setContextSummarized(false)
    setMessages([...truncated, { role: 'user', content: newContent, ephemeral: true }])
    await sendCore(newContent, history)
    return true
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send()
  }

  const onInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = Math.min(e.target.scrollHeight, 180) + 'px'
  }

  return {
    messages, setMessages, input, setInput, turnNotice, loading, contextWarning, setContextWarning,
    contextSummarized, setContextSummarized, textareaRef, activeStream, abandonStream, busy, guestAtLimit,
    send, stopGeneration, regenerate, editAndRegenerate, onKeyDown, onInput,
  }
}
