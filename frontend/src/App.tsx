import { useState, useRef, useEffect } from 'react'
import Login from './Login'
import AdminPanel from './AdminPanel'
import { PRIMARY_COLOR, INSTANCE_NAME } from './theme'
import Profile from './Profile'
import { ErrorSurface, actionError, emitError, guardedJson, guardedPoll } from './errorSurface'
import { API, authHeaders, sessionKey, getOrCreateSession } from './api'
import { TypingIndicator } from './chat/TypingIndicator'
import type { ChatMessage, SessionEntry, SysStatus, Analytics, ModelGroup, AuthUser, View } from './chat/types'
import { Message } from './chat/Message'
import { useChatStream, GUEST_TURN_LIMIT } from './chat/useChatStream'
import { Sidebar } from './chat/Sidebar'
import { ChatHeader } from './chat/ChatHeader'

// ── Domain types ───────────────────────────────────────────────────────────



// The WHOLE redirect decision, extracted so it is testable without stubbing
// window.location, which jsdom makes non-configurable. Only the navigation is
// left to the hand check.
//
// `=== true` is an identity check: a missing key on an older backend reads as
// claimed, never as an invitation to claim. wantsLogin is an argument rather
// than a caller-side guard so the precedence is pinned by a test - an explicit
// ?login=1 must outrank a boot-time inference, or an instance that reaches zero
// active admins has no reachable login screen at all.
export function wantsSetupRedirect(
  cfg: { needs_setup?: boolean }, hash: string, wantsLogin: boolean,
): boolean {
  return cfg.needs_setup === true && hash !== '#setup' && !wantsLogin
}

// GET /api/auth/config - the one payload available before a session exists.
// Everything the boot decision needs and nothing that would leak: whether the
// deployment is claimed, and whether an anonymous visitor may chat at all.
interface AuthConfig {
  needs_setup?: boolean
  auth_mode?: string
  guest_mode_enabled?: boolean
  // Delivered on the PUBLIC config read because /api/config is authenticated
  // and a guest therefore never learned it - so the toggle rendered on every
  // instance, including ones where the operator had turned it off.
  allow_rag_toggle?: boolean
  // In-product help: whether the server runs the help lane (HELP_DOCS). On
  // the public read for the same reason as the two above - a guest never
  // reaches /api/config, and a Help button that sends a lane the server
  // ignores would be a control that does nothing.
  help_enabled?: boolean
}

// ── Constants ──────────────────────────────────────────────────────────────

const DEFAULT_SUGGESTIONS = [
  'What can you help me with?',
  'Summarize or explain a document for me.',
  'Help me draft or improve some text.',
]

// In-product help (2026-09-21): the questions a first user asks in week one,
// answered from the product's own help pages - never from the operator's
// documents.
const HELP_SUGGESTIONS = [
  'How do I sign in?',
  'Why can the assistant not find a document I know exists?',
  'What do the Sources under an answer mean?',
  'Where do my questions and the documents go?',
  'What will the assistant refuse to answer, and why?',
]

// ── App ────────────────────────────────────────────────────────────────────

export default function App() {
  const [view, setView] = useState<View>('loading')
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null)
  const [isGuest, setIsGuest] = useState(false)
  const [guestModeEnabled, setGuestModeEnabled] = useState(false)
  // In-product help. helpEnabled comes from the public auth config (the
  // server may run with HELP_DOCS=false). helpMode is the toggle: pressed,
  // the next question asks the product's own help pages (the reserved `help`
  // department, retrieval forced on, never a peer) instead of the knowledge
  // base. Persisted beside the session pointer so a reload mid-help comes
  // back IN help mode with the help thread, not in normal mode with a help
  // transcript riding along as history.
  const [helpEnabled, setHelpEnabled] = useState(true)
  const [helpMode, setHelpMode] = useState(() => localStorage.getItem('help_mode') === '1')
  // The conversation the visitor left to open help, restored when they leave it.
  const preHelp = useRef<{ id: string; messages: ChatMessage[] } | null>(null)
  const [profileOpen, setProfileOpen] = useState(false)

  const [modelGroups, setModelGroups] = useState<ModelGroup[]>([])
  const [model, setModel] = useState('')
  // Did the USER pick this model from the selector? Same distinction as
  // ragTouched: `model` is seeded from the server's default, so its mere
  // presence says nothing about intent, and sending it unconditionally
  // suppressed the operator's chat_model pin for every signed-in caller.
  const [modelTouched, setModelTouched] = useState(false)
  const [useRag, setUseRag] = useState(true)
  // Whether useRag reflects the SERVER's configured default or is still this
  // component's optimistic initial value. Guests never learn it - /api/config
  // is authenticated - so they must not assert one.
  const [ragKnown, setRagKnown] = useState(false)
  // Did the USER express a preference? Distinct from ragKnown: a guest never
  // learns the server default, but if they flip the toggle they have still
  // said something, and a control that animates while being ignored is worse
  // than no control at all.
  const [ragTouched, setRagTouched] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [sessionId, setSessionId] = useState<string>(getOrCreateSession)
  const [sessionList, setSessionList] = useState<SessionEntry[]>([])
  const [sysStatus, setSysStatus] = useState<SysStatus | null>(null)
  const [analytics, setAnalytics] = useState<Analytics | null>(null)
  const [suggestions, setSuggestions] = useState<string[]>(DEFAULT_SUGGESTIONS)
  const [allowModelSelection, setAllowModelSelection] = useState(true)
  const [allowRagToggle, setAllowRagToggle] = useState(true)
  const [instanceName, setInstanceName] = useState(INSTANCE_NAME)
  // What the SERVER will actually answer with. It can differ from `model` -
  // an operator can pin a model in admin config - and the backend sends it so
  // the client does not display its own copy and misreport the pin.
  const [effectiveModel, setEffectiveModel] = useState('')
  const initials = instanceName.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()
  const bottomRef = useRef<HTMLDivElement>(null)
  // IDENTITY TICKET, same idea as the stream ticket above and for the same
  // reason. clearIdentityState resets eleven pieces of state, but the polls
  // that fill three of them are already in flight when it runs - so a
  // /api/status, /api/analytics or /api/sessions/mine response belonging to the
  // account that just signed out could land afterwards and put its data back on
  // a screen that had been cleared. Every identity transition takes a new
  // ticket; a response holding a stale one is dropped in silence.
  const identityGen = useRef(0)

  const {
    messages, setMessages, input, setInput, turnNotice, loading, contextWarning, setContextWarning,
    contextSummarized, setContextSummarized, textareaRef, activeStream, abandonStream, busy, guestAtLimit,
    send, stopGeneration, regenerate, editAndRegenerate, onKeyDown, onInput,
  } = useChatStream({ sessionId, isGuest, helpMode, modelTouched, model, ragKnown, ragTouched, useRag, identityGen })

  // Everything that belongs to ONE identity and must not survive into the next
  // one at this browser. Sign-out used to reset only currentUser / isGuest /
  // messages / input / view, so sessionList, analytics, sysStatus, the RAG
  // preference and an open profile modal all carried across it. The RAG
  // preference is not display-only: it travels in the next occupant's request
  // body.
  //
  // The sessionId rotation is separately load-bearing. regenerate and edit bail
  // after their trim by comparing sessionIdRef against the id they captured,
  // and sign-out is declared a leaving-the-conversation path like the other
  // three - but unlike them it never changed sessionId, so that guard could not
  // see it. A sign-out landing inside the trim round-trip therefore let the
  // regenerate run to completion and re-POST the signed-out user's turn with no
  // credentials. The rotated id is deliberately NOT written to localStorage:
  // the next sign-in restores that account's own stored session.
  const clearIdentityState = () => {
    // Invalidate every identity-scoped read already in flight - see identityGen.
    identityGen.current += 1
    setMessages([])
    setInput('')
    setSessionId(crypto.randomUUID())
    setSessionList([])
    setSysStatus(null)
    setAnalytics(null)
    setProfileOpen(false)
    setContextWarning(false)
    setContextSummarized(false)
    setUseRag(true)
    setRagKnown(false)
    setRagTouched(false)
    setModelTouched(false)
  }

  // Auth bootstrap - runs once on load.
  // Demo instances run ENABLE_AUTH=false (frictionless guest tour), but the owner
  // still has a real admin account: /api/auth/login + /api/auth/me are
  // self-contained endpoints that work regardless of the middleware flag. So a
  // stored session is restored EVEN when auth is "off" (a signed-in owner must
  // not be demoted to guest on refresh), and ?login=1 is the bookmarkable owner
  // door to the login screen on an otherwise guest-first instance.
  useEffect(() => {
    const wantsLogin = new URLSearchParams(window.location.search).has('login')
    // PUBLIC endpoint on purpose. /api/status and /api/config both require auth
    // here, and a 401 from either PARSES as JSON - so a boot built on them
    // resolves, reads undefined for every field, never trips .catch, and lands
    // on the chat view with nothing anywhere to observe.
    fetch(`${API}/api/auth/config`)
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then(async (cfg: AuthConfig) => {
        // The guest door is reported by the same expression the chat gate
        // reads, so the login screen cannot offer one the server refuses.
        setGuestModeEnabled(cfg.guest_mode_enabled === true)
        if (cfg.help_enabled !== undefined) setHelpEnabled(cfg.help_enabled)
        // Guests get the operator's answer here or nowhere. The server enforces
        // the setting on the chat route regardless, so this only stops the UI
        // offering a control that cannot do anything.
        if (cfg.allow_rag_toggle !== undefined) setAllowRagToggle(cfg.allow_rag_toggle)
        const token = localStorage.getItem('az_jwt_token')
        if (token) {
          try {
            const res = await fetch(`${API}/api/auth/me`, {
              headers: { 'Authorization': `Bearer ${token}` },
            })
            if (res.ok) {
              const user: AuthUser = await res.json()
              setCurrentUser(user)
              setSessionId(getOrCreateSession(user.id))
              setView('chat')
              return
            }
            localStorage.removeItem('az_jwt_token')
          } catch { /* fall through to the unauthenticated paths */ }
        }
        // Nobody to sign in as yet: route the operator to the claim screen
        // instead of a login screen with no account behind it. AFTER the token
        // restore, not before - the backend refuses to deactivate or demote the
        // last Owner (routers/users.py), so needs_setup flips back to true on a
        // CLAIMED deployment only by a database edit, but a signed-in user must
        // still never be bounced to a claim form whose code was burned at first
        // claim.
        //
        // The reload is load-bearing: main.tsx builds `page` once at module
        // scope with no hashchange listener, so setting the hash alone renders
        // nothing. It cannot loop - at '#setup' main.tsx picks Claim over App,
        // so this effect never mounts again; the hash check in
        // wantsSetupRedirect is belt-and-braces for whoever adds a hashchange
        // listener later.
        // replace(), not a hash assignment: assigning to location.hash PUSHES a
        // history entry, so Back would traverse to '/', re-run this effect and
        // bounce forward again, one entry deeper each press. The reload stays
        // load-bearing either way - a fragment change alone does not re-execute
        // main.tsx, which is where the route is decided.
        if (wantsSetupRedirect(cfg, window.location.hash, wantsLogin)) {
          window.location.replace(
            window.location.pathname + window.location.search + '#setup')
          window.location.reload()
          return
        }
        setView('login')
      })
      // Reaching here means the public config itself failed - the backend is
      // unreachable. The login screen is the honest landing: it says so on the
      // first submit rather than rendering a chat that cannot answer.
      .catch(() => setView('login'))
  }, [])

  // Fetch available models and public config (provider-aware; sends the auth
  // token when present - the endpoint 401s signed-out callers under ENABLE_AUTH)
  useEffect(() => {
    // BOTH READS ARE AUTHENTICATED, so a caller with no token must not make
    // them. The comment here used to say a 401 "resolves before any
    // ErrorSurface host is mounted, so it stays quiet by construction" - that
    // was true of the FIRST run and untrue of every later one, because this
    // effect is keyed on [view] and re-runs when a guest crosses from login to
    // chat, where the surface IS mounted. guardedJson maps 401 to
    // emitAuthExpired, so every guest session opened under a sticky
    // "Session expired - your login is no longer valid" banner with no dismiss,
    // before they had done anything at all.
    //
    // A guest keeps the client-side defaults, which is what they got anyway:
    // the two calls could only ever 401 for them.
    const signedIn = !!localStorage.getItem('az_jwt_token')
    if (!signedIn) return
    guardedJson<{ groups?: ModelGroup[] }>(
      fetch(`${API}/api/models`, { headers: authHeaders() }), 'Loading models')
      .then(d => {
        if (!d) return
        const groups = d.groups || []
        setModelGroups(groups)
        const first = groups[0]?.models[0]?.value
        if (first) setModel(prev => prev || first)
      })
    if (view !== 'chat' && view !== 'admin') return
    guardedJson<{
      suggestions?: string[]
        allow_model_selection?: boolean
        allow_rag_toggle?: boolean
        default_model?: string
        default_rag_enabled?: boolean
        guest_mode_enabled?: boolean
      instance_name?: string
      chat_model_effective?: string
      chat_model_resolved?: string
    }>(fetch(`${API}/api/config`, { headers: authHeaders() }), 'Loading settings')
      .then(d => {
        if (!d) return
        if (d.suggestions && d.suggestions.length > 0) setSuggestions(d.suggestions)
        if (d.allow_model_selection !== undefined) setAllowModelSelection(d.allow_model_selection)
        if (d.allow_rag_toggle !== undefined) setAllowRagToggle(d.allow_rag_toggle)
        // The picker's highlight starts on what an untouched request gets: the
        // operator's chat_model pin when there is one, which default_model is
        // not - seeding from default_model highlighted a model that would not
        // answer.
        if (d.chat_model_effective || d.default_model) setModel(d.chat_model_effective || d.default_model || '')
        if (d.default_rag_enabled !== undefined) { setUseRag(d.default_rag_enabled); setRagKnown(true) }
        if (d.guest_mode_enabled !== undefined) setGuestModeEnabled(d.guest_mode_enabled)
        if (d.instance_name) setInstanceName(d.instance_name)
        // The server's EFFECTIVE model - what actually answers. The backend
        // sends it precisely so a client does not display its own copy and
        // quietly misreport a server-side pin.
        // A family alias ("claude-opus-latest") arrives resolved too - the
        // version answering today is the one named.
        if (d.chat_model_resolved || d.chat_model_effective)
          setEffectiveModel(d.chat_model_resolved || d.chat_model_effective || '')
      })
  }, [view])

  // Keep the browser tab title in sync with the (config-driven) brand name.
  useEffect(() => { if (instanceName) document.title = instanceName }, [instanceName])

  const handleLogin = async (token: string, refreshToken: string, user: AuthUser) => {
    localStorage.setItem('az_jwt_token', token)
    localStorage.setItem('az_jwt_refresh', refreshToken)
    // The login payload has no `permissions` - that array only comes back from
    // /api/auth/me. Without this refetch a permitted non-owner gets no admin
    // entry point until the next reload, when the boot path fetches it anyway.
    let resolved = user
    try {
      const me = await fetch(`${API}/api/auth/me`, {
        headers: { 'Authorization': `Bearer ${token}` },
      })
      if (me.ok) resolved = await me.json()
    } catch { /* keep the login payload; the boot path fills it in on reload */ }
    // Signing IN is an identity transition too. A guest can be mid-answer when
    // they click Sign in, and that stream keeps its ticket, keeps owning
    // streaming/loading, and goes on writing into the transcript the new
    // account is about to be shown. Sign-out was fixed for this; the door in
    // the other direction was not.
    abandonStream()
    // EVERY IDENTITY TRANSITION STARTS FROM NOTHING, in as well as out. The
    // history effect replaces messages only `if (data?.messages?.length)`, so
    // signing into an account whose session is empty left the PREVIOUS
    // account's conversation rendered - and send() posted it as the new
    // account's history. Clearing only the transcript also left the previous
    // visitor's unsent draft in the box and their RAG choice in the next
    // request body, and did not move the identity ticket that a regenerate in
    // mid-trim checks before it re-sends (the 2026-10-02 defensive read).
    clearIdentityState()
    setCurrentUser(resolved)
    setIsGuest(false)
    setSessionId(getOrCreateSession(resolved.id))
    setView('chat')
  }

  const handleGuest = () => {
    // Same reason as handleLogin: continuing as guest leaves whatever
    // conversation was on screen, so any stream still running must be dropped
    // rather than left writing into the guest's fresh transcript, and nothing
    // of the previous visitor rides along.
    abandonStream()
    clearIdentityState()
    setIsGuest(true)
    setCurrentUser(null)
    setSessionId(crypto.randomUUID())
    setView('chat')
  }

  const handleLogout = async () => {
    // ABANDON BEFORE THE ROUND-TRIP, not after. The logout POST can hang for
    // the whole fetch timeout on an unreachable backend, and until it returned
    // the outgoing user's answer kept streaming into a screen the next person
    // was about to use.
    abandonStream()
    // THE CREDENTIALS LEAVE STORAGE BEFORE THE SCREEN CHANGES. They used to go
    // after the POST returned, with the login screen already up: every request
    // reads its header from storage, so "Continue as guest" in that window
    // chatted as the outgoing account, and a sign-in in that window had its
    // fresh tokens removed when the old POST finished (the 2026-10-02
    // defensive read). The POST still carries the outgoing token - read here,
    // before it goes.
    const outgoing = authHeaders()
    localStorage.removeItem('az_jwt_token')
    localStorage.removeItem('az_jwt_refresh')
    // LEAVE THE CHAT VIEW in the same synchronous batch as the state reset
    // below. clearIdentityState rotates sessionId, and the history effect keys
    // on [sessionId, view, isGuest] - rotating while `view` was still 'chat'
    // fired a fetch for a session id that had existed for microseconds.
    setView('login')
    // Signing out has to leave NOTHING on screen or in state for the next
    // person at this machine - see clearIdentityState for what "nothing" turned
    // out to include beyond the transcript.
    clearIdentityState()
    setCurrentUser(null)
    setIsGuest(false)
    try {
      await fetch(`${API}/api/auth/logout`, { method: 'POST', headers: outgoing })
    } catch { /* ignore */ }
  }

  // Chat data polling - hooks must be declared before any early returns
  useEffect(() => {
    if (view !== 'chat') return
    // Captured once per effect run. Each of the three polls below checks it
    // before writing, so a response that outlives its identity is dropped
    // rather than repainting the previous account's data.
    const myIdentity = identityGen.current
    const stillMine = () => identityGen.current === myIdentity
    const fetchStatus = () => {
      // /api/status is authenticated here, so a guest has nothing to send.
      if (isGuest || !currentUser) return
      guardedPoll<SysStatus>(fetch(`${API}/api/status`, { headers: authHeaders() }))
        .then(d => { if (d && stillMine()) setSysStatus(d) })
    }
    const fetchAnalytics = () => {
      // Guests have no view_analytics permission - their 401 here is by design,
      // not an expired session; skip instead of tripping the expired banner.
      if (isGuest) return
      guardedPoll<Analytics>(fetch(`${API}/api/analytics`, { headers: authHeaders() }))
        .then(d => { if (d && stillMine()) setAnalytics(d) })
    }
    const fetchSessions = () => {
      if (isGuest) return
      // /api/sessions/mine, NOT /api/sessions. The latter is the operator
      // analytics view - view_analytics-gated and all_users=True - so pointing a
      // personal history sidebar at it gave ordinary members a silent 403 and no
      // history at all, while operators got other people's conversations listed
      // as their own, titled with those conversations' first messages, each one
      // opening empty because /api/history is correctly owner-scoped.
      guardedPoll<{ sessions?: SessionEntry[] }>(fetch(`${API}/api/sessions/mine`, { headers: authHeaders() }))
        .then(d => { if (d && stillMine()) setSessionList(d.sessions || []) })
    }
    fetchStatus()
    fetchAnalytics()
    fetchSessions()
    const interval = setInterval(() => { fetchStatus(); fetchAnalytics(); fetchSessions() }, 30000)
    return () => clearInterval(interval)
  }, [view])

  useEffect(() => {
    if (view !== 'chat') return
    if (isGuest) return
    // NEVER RELOAD OVER A LIVE STREAM. This effect re-runs on `view`, so simply
    // opening and closing the Admin panel mid-answer replaced `messages`
    // wholesale - which discards the partial answer AND desynchronises `slot`,
    // the index the running stream is writing into. The stream then wrote its
    // remaining tokens over whatever row happened to land at that index.
    // A stream is authoritative for its own conversation while it runs: the
    // rows it is about to produce do not exist server-side yet, so the server's
    // copy is strictly staler than what is on screen.
    if (activeStream.current !== null) return
    // A LOAD THAT LANDS LATE IS DROPPED. This read is for ONE session as ONE
    // identity; when either changes the effect re-runs and this run is
    // superseded. Without the flag, a read that stalled past a sign-out and the
    // next sign-in painted the previous account's conversation on the new
    // account's screen - and send() would post it as their history (found by
    // the 2026-10-02 defensive read; the same for a conversation switch).
    let superseded = false
    guardedJson<{ messages?: Array<{ role: 'user' | 'assistant'; content: string }> }>(
      fetch(`${API}/api/history/${sessionId}`, { headers: authHeaders() }), 'Loading chat history')
      .then(data => {
        // Re-check after the await: a send() can start between the request and
        // its response, and the guard above cannot see the future.
        if (superseded || activeStream.current !== null) return
        if (data?.messages?.length) {
          setMessages(data.messages.map(m => ({ role: m.role, content: m.content })))
        }
      })
    return () => { superseded = true }
  }, [sessionId, view, isGuest])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  if (view === 'loading') {
    return (
      <div className="flex h-screen bg-gray-950 items-center justify-center">
        <div className="w-6 h-6 border-2 border-gray-600 border-t-white rounded-full animate-spin" />
      </div>
    )
  }

  if (view === 'login') {
    return <Login api={API} onLogin={handleLogin} onGuest={guestModeEnabled ? handleGuest : undefined} />
  }

  if (view === 'admin' && currentUser) {
    return (
      <>
        <ErrorSurface onLogout={handleLogout} />
        {/* The view guard above already requires a session, so currentUser is
            non-null here - the panel has no anonymous mode to fall back to. */}
        <AdminPanel
          api={API}
          headers={authHeaders}
          currentUser={currentUser}
          onClose={() => setView('chat')}
          onLogout={handleLogout}
        />
      </>
    )
  }

  const handleUsernameChange = (newUsername: string, accessToken: string, refreshToken: string) => {
    localStorage.setItem('az_jwt_token', accessToken)
    localStorage.setItem('az_jwt_refresh', refreshToken)
    setCurrentUser(u => u ? { ...u, username: newUsername } : u)
  }

  const handlePasswordChange = () => {
    // Changing a password invalidates the session and drops to the login
    // screen, which makes this a leaving-the-conversation path exactly like
    // sign-out. It was missed when sign-out was fixed, and the commit that
    // fixed sign-out asserted it was "the one" such path - it was not. The
    // Profile button is not gated on `busy`, so this is reachable mid-stream.
    abandonStream()
    clearIdentityState()
    localStorage.removeItem('az_jwt_token')
    localStorage.removeItem('az_jwt_refresh')
    setCurrentUser(null)
    setView('login')
  }

  const switchSession = (id: string) => {
    abandonStream()
    // Opening a past conversation is leaving help: a help-lane request must
    // never be sent into a conversation held under the knowledge base.
    if (helpMode) {
      localStorage.removeItem('help_mode')
      setHelpMode(false)
      preHelp.current = null
    }
    localStorage.setItem(sessionKey(currentUser?.id), id)
    setSessionId(id)
    setMessages([])
  }

  // Reads the answer (2026-10-03). The row used to go whatever the server said,
  // so a refused delete - an expired session, a 5xx - came back on the next
  // poll as though the click had never happened. Resolves true only on a 2xx,
  // which is what the History list's confirm waits for.
  const deleteSession = async (id: string): Promise<boolean> => {
    if (id === sessionId) abandonStream()
    const err = await actionError(
      fetch(`${API}/api/history/${encodeURIComponent(id)}`, { method: 'DELETE', headers: authHeaders() }),
      'Deleting the conversation')
    if (err) { emitError(err); return false }
    setSessionList(prev => prev.filter(s => s.session !== id))
    if (id === sessionId) {
      const newId = crypto.randomUUID()
      localStorage.setItem(sessionKey(currentUser?.id), newId)
      setSessionId(newId)
      setMessages([])
    }
    return true
  }

  // The name the sidebar shows. Resolves true only when the server stored it;
  // a refusal is a toast and the old name stays.
  const renameSession = async (id: string, name: string): Promise<boolean> => {
    const err = await actionError(fetch(`${API}/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ name }),
    }), 'Renaming the conversation')
    if (err) { emitError(err); return false }
    setSessionList(prev => prev.map(s => (s.session === id ? { ...s, name } : s)))
    return true
  }

  const handleFeedback = async (msgIndex: number, value: number) => {
    // The user clicked a thumb - a swallowed failure means they believe the
    // label landed when it didn't (the training-label primitive, silently lost).
    const err = await actionError(fetch(`${API}/api/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      // STORED-ROW ORDINAL, NOT THE BUBBLE INDEX. turn_index is meant to
      // identify a row on the server, and the client's array now legitimately
      // contains bubbles that have no row - the stopped notice, error bubbles,
      // an unsaved partial answer. Counting them shifts every later index, so
      // a thumb landed on the wrong turn. Same non-ephemeral counting the trims
      // use.
      body: JSON.stringify({
        session_id: sessionId,
        turn_index: messages.slice(0, msgIndex).filter(m => !m.ephemeral).length,
        value,
      }),
    }), 'Saving feedback')
    if (err) emitError(err)
  }

  const newChat = () => {
    abandonStream()
    // NO DELETE HERE. This used to fire DELETE /api/history/{sessionId} before
    // rotating the id, which hard-deletes the caller's message rows server-side
    // (chat.py delete_history -> clear_session). So the control labelled
    // "+ New Chat", sitting directly above a History list whose whole purpose is
    // returning to past conversations, destroyed the conversation it was
    // leaving - no confirmation, no undo, and the failure swallowed by a bare
    // .catch. Starting a new conversation is not a request to erase the old one;
    // the sidebar already has an explicit per-session delete for that.
    const newId = crypto.randomUUID()
    localStorage.setItem(sessionKey(currentUser?.id), newId)
    // Reset in place - NO full page reload. (The reload also wiped the View-as
    // persona back to its default, so a persona switch never appeared to change.)
    setSessionId(newId)
    setMessages([])
    setInput('')
    setContextWarning(false)
    setContextSummarized(false)
  }

  // In-product help. A fresh session WITHOUT touching the current one on the
  // server: entering or leaving help must not erase the conversation the
  // visitor was in the middle of (the sidebar's per-session delete is the only
  // thing that deletes).
  const freshSession = () => {
    const newId = crypto.randomUUID()
    localStorage.setItem(sessionKey(currentUser?.id), newId)
    setSessionId(newId)
    setMessages([])
    setInput('')
    setContextWarning(false)
    setContextSummarized(false)
  }
  const enterHelp = () => {
    abandonStream()
    preHelp.current = { id: sessionId, messages }
    localStorage.setItem('help_mode', '1')
    setHelpMode(true)
    setView('chat')
    freshSession()
  }
  const leaveHelp = () => {
    abandonStream()
    localStorage.removeItem('help_mode')
    setHelpMode(false)
    const prev = preHelp.current
    preHelp.current = null
    if (!prev) { freshSession(); return }
    // Back to where they were: the same session id and the messages they had
    // (a signed-in user's copy reloads from the server on the id change).
    localStorage.setItem(sessionKey(currentUser?.id), prev.id)
    setSessionId(prev.id)
    setMessages(prev.messages)
    setInput('')
  }

  // NAME THE MODEL, NOT A GUESSED VENDOR. The first attempt at this read
  // status.provider.provider - which is the ENABLED-PROVIDER SET, not the
  // provider that answered: it reports the single enabled name, or literally
  // "multi", and routing picks a provider per model prefix. So an instance with
  // Ollama enabled and a cloud model pinned would have been labelled "local
  // model via Ollama" while a hosted API answered - a worse lie than the
  // hardcoded "Claude API" it replaced, because it looks specific.
  // chat_model_effective is what the server says actually answers.
  //
  // AND NAME THE ONE THAT ANSWERS THIS REQUEST. A picked model is sent, and
  // the server answers with it whenever selection is allowed - so naming the
  // pin after a pick named a model that did not answer. Untouched (nothing
  // sent), or with selection off (the server ignores what is sent), the
  // server's own effective model answers. Unknown is empty, never a guess.
  const answeringModel = modelTouched && allowModelSelection ? model : effectiveModel
  const providerLabel = answeringModel || ''
  return (
    <div className="flex h-screen bg-gray-950 text-white overflow-hidden">
      <ErrorSurface onLogout={handleLogout} />
      {profileOpen && currentUser && (
        <Profile
          api={API}
          headers={authHeaders}
          user={currentUser}
          onClose={() => setProfileOpen(false)}
          onUsernameChange={handleUsernameChange}
          onPasswordChange={handlePasswordChange}
        />
      )}

      {/* Sidebar */}
      {sidebarOpen && (
        <Sidebar
          initials={initials}
          instanceName={instanceName}
          newChat={newChat}
          isGuest={isGuest}
          sessionList={sessionList}
          sessionId={sessionId}
          switchSession={switchSession}
          renameSession={renameSession}
          deleteSession={deleteSession}
          allowModelSelection={allowModelSelection}
          modelGroups={modelGroups}
          model={model}
          setModel={setModel}
          setModelTouched={setModelTouched}
          allowRagToggle={allowRagToggle}
          useRag={useRag}
          setUseRag={setUseRag}
          setRagTouched={setRagTouched}
          sysStatus={sysStatus}
          analytics={analytics}
        />
      )}

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">

        {/* Header */}
        <ChatHeader
          sidebarOpen={sidebarOpen}
          setSidebarOpen={setSidebarOpen}
          helpEnabled={helpEnabled}
          helpMode={helpMode}
          enterHelp={enterHelp}
          leaveHelp={leaveHelp}
          instanceName={instanceName}
          answeringModel={answeringModel}
          currentUser={currentUser}
          setView={setView}
          isGuest={isGuest}
          setProfileOpen={setProfileOpen}
          handleLogout={handleLogout}
        />

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-6 py-8">
          {messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center max-w-lg mx-auto">
              <div className="w-16 h-16 rounded-2xl flex items-center justify-center text-xl font-bold mb-5 shadow-lg" style={{ backgroundColor: PRIMARY_COLOR }}>
                {initials}
              </div>
              <h2 className="text-xl font-semibold text-white mb-2">{instanceName}</h2>
              <p className="text-gray-500 text-sm mb-8">
                {/* NEVER PROMISE PRIVACY WE CANNOT SEE. This read
                    `(sysStatus?.provider?.provider ?? 'ollama') === 'ollama'`,
                    and a guest never receives /api/status - it is authenticated
                    and 401s for them - so sysStatus is null and the fallback
                    made EVERY anonymous visitor read "your data never leaves
                    this machine", on any deployment, including one answering
                    from a hosted API. That is the one claim on this screen a
                    visitor cannot check for themselves, on a platform whose
                    whole pitch is that trust is measured rather than asserted.
                    Unknown now says something true instead of something
                    flattering, and the local case drops the absolute
                    data-egress guarantee: the answering model being local does
                    not by itself prove nothing else is remote. */}
                {!sysStatus?.provider?.provider
                  ? 'Ask a question to get started.'
                  : sysStatus.provider.provider === 'ollama'
                    ? 'Answers come from a model running on this server.'
                    : 'A cloud-powered AI assistant. Conversations are processed via a provider API.'}
              </p>
              {helpMode && (
                <div className="w-full mb-6 rounded-xl border border-gray-700/50 bg-gray-800/40 px-4 py-3 text-left" data-testid="help-card">
                  <p className="text-xs text-gray-400"><span className="font-medium text-white">Help</span> <span className="text-gray-500">&middot; ask how {instanceName} works.</span></p>
                  <p className="text-[11px] text-gray-600 mt-1">Answers here come from the assistant's own help pages, not from your organization's documents. Click Help again to go back.</p>
                </div>
              )}
              <div className="w-full space-y-2">
                {(helpMode ? HELP_SUGGESTIONS : suggestions).map(s => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="w-full text-left text-sm text-gray-400 hover:text-white bg-gray-800/50 hover:bg-gray-800 border border-gray-700/50 hover:border-gray-600 px-4 py-3 rounded-xl transition-all"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto">
              {messages.map((m, i) => (
                <Message key={i} role={m.role} content={m.content} toolCalls={m.toolCalls}
                  sources={m.sources} helpLane={m.help} notice={m.notice} msgIndex={i}
                  isStreaming={busy}
                  // Guests have no session; /api/feedback needs one, and a 401 here
                    // raises the sticky session-expired banner at someone who never
                    // logged in. Withhold the control rather than the error.
                    // An UNSTORED answer has no row to vote on: its turn_index is
                    // the count of stored rows before it, which names the NEXT
                    // stored row - the vote would land on another turn.
                    onFeedback={m.role === 'assistant' && !isGuest && !m.ephemeral ? handleFeedback : undefined}
                  onRegenerate={m.role === 'assistant' && i === messages.length - 1 ? regenerate : undefined}
                  onEdit={m.role === 'user' ? (newContent) => editAndRegenerate(i, newContent) : undefined}
                />
              ))}
              {loading && <TypingIndicator />}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {/* Context window banner */}
        {(contextWarning || contextSummarized) && (
          <div className="border-t border-yellow-700/30 bg-yellow-900/10 px-6 py-2 flex items-center justify-between gap-4 flex-shrink-0">
            <span className="text-xs text-yellow-400">
              {contextSummarized
                ? 'Older messages were summarized to stay within context limits.'
                : 'This conversation is getting long - responses may be less accurate. Consider starting a new chat.'}
            </span>
            <button
              onClick={() => { setContextWarning(false); setContextSummarized(false) }}
              className="text-yellow-600 hover:text-yellow-300 text-xs shrink-0 transition-colors"
            >
              ✕
            </button>
          </div>
        )}

        {/* Guest limit banner */}
        {guestAtLimit && (
          <div className="border-t border-gray-700/50 bg-gray-900/60 px-6 py-3 flex items-center justify-between gap-4 flex-shrink-0">
            <span className="text-xs text-gray-400">
              Guest limit reached ({GUEST_TURN_LIMIT} messages).{' '}
              <button onClick={() => setView('login')} className="text-blue-400 hover:text-blue-300 underline transition-colors">
                Sign in
              </button>{' '}
              to keep chatting.
            </span>
          </div>
        )}

        {/* Input */}
        <div className="border-t border-gray-800 bg-gray-900/30 p-4">
          <div className="max-w-3xl mx-auto">
            {turnNotice && (
              <div className="text-xs text-gray-300 px-1 pb-2" role="status">{turnNotice}</div>
            )}
            <div className="flex gap-3 items-end bg-gray-800 border border-gray-700 focus-within:border-blue-500/60 rounded-2xl px-4 py-3 transition-colors">
              <textarea
                ref={textareaRef}
                className="flex-1 bg-transparent text-white text-sm resize-none outline-none placeholder-gray-500 leading-relaxed disabled:opacity-50"
                placeholder={guestAtLimit ? 'Sign in to continue chatting…' : helpMode ? `Ask how ${instanceName} works...  (Ctrl+Enter to send)` : `Message ${instanceName}...  (Ctrl+Enter to send)`}
                value={input}
                rows={1}
                // Typing is allowed mid-answer; SENDING is not. send() carries
                // the real guard (busy), so a draft cannot become a second
                // concurrent stream. Disabling the box outright also prevented
                // the overlap, but it locked the composer for the whole answer -
                // on a long one you could not even draft the next question, and
                // an unlucky keystroke landed nowhere. Before the streaming fix
                // this box re-enabled a second in, at the first token, which is
                // what let a second stream start; the fix belongs on the send
                // path, not on the keyboard.
                disabled={guestAtLimit}
                onChange={onInput}
                onKeyDown={onKeyDown}
              />
              <button
                onClick={busy ? stopGeneration : () => send()}
                disabled={guestAtLimit || (!busy && !input.trim())}
                className="w-9 h-9 disabled:bg-gray-700 disabled:cursor-not-allowed rounded-xl flex items-center justify-center transition-colors flex-shrink-0 shadow-sm"
                style={busy ? { backgroundColor: '#dc2626' } : (!input.trim() ? {} : { backgroundColor: PRIMARY_COLOR })}
                title={busy ? 'Stop generation' : 'Send message'}
              >
                {busy ? (
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor">
                    <rect x="3" y="3" width="18" height="18" rx="2" />
                  </svg>
                ) : (
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="22" y1="2" x2="11" y2="13" />
                    <polygon points="22 2 15 22 11 13 2 9 22 2" />
                  </svg>
                )}
              </button>
            </div>
            {/* Names the MODEL actually answering (answeringModel): the user's
                pick when one is sent and honoured, else chat_model_effective
                from /api/config - the server's own resolved value, not an
                inference this client makes. (The comment here used to say /api/status;
                that is where an earlier version read status.provider.provider,
                which is the ENABLED-PROVIDER SET rather than the thing that
                answered, and reports the literal string "multi" when more than
                one is on.) It read "Claude API" hardcoded before that - on a
                template that speaks to Ollama, Anthropic, OpenAI, Gemini,
                Mistral, Groq, xAI and DeepSeek, and whose shipped default is
                local Ollama with no Anthropic key configured at all. So the
                stock deployment credited a vendor it was not using, in the one
                line a visitor reads to find out what answered them.
                This client never sends `model` unless the user picked one, so
                the server's chat_model pin resolves and what is named here is
                what ran. A guest's boot read of /api/config is skipped (no
                token), so effectiveModel stays empty and the segment is omitted
                rather than guessed. */}
            <p className="text-center text-xs text-gray-600 mt-2">
              Powered by Architecture Zero{providerLabel ? ` · ${providerLabel}` : ''} · responses are AI-generated
            </p>
          </div>
        </div>

      </div>
    </div>
  )
}
