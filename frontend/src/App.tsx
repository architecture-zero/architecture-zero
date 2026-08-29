import { useState, useRef, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import { SAFE_MARKDOWN_COMPONENTS } from './markdownSafety'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import Login from './Login'
import AdminPanel from './AdminPanel'
import Profile from './Profile'
import { ErrorSurface, actionError, emitError, guardedJson, guardedPoll } from './errorSurface'

// ── Domain types ───────────────────────────────────────────────────────────



interface ToolCall {
  name: string
  result: string
}

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  toolCalls?: ToolCall[]
  sources?: string[]
}

interface SessionEntry {
  session: string
  first_message?: string
}

interface SysStatus {
  ollama: string
  rag_documents: number
  auth_enabled?: boolean
  rag_only_mode?: boolean
  agent_tools?: { agent_enabled: boolean }
  provider?: { provider: string }
  // Optional on purpose: the slim unauth payload omits most of these already,
  // and a backend predating this key must not become a type error.
  needs_setup?: boolean
}

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
}

interface Analytics {
  total_sessions: number
  requests_today: number
  feedback?: {
    total: number
    thumbs_up: number
    thumbs_down: number
  }
}

// ── Constants ──────────────────────────────────────────────────────────────

const INSTANCE_NAME = import.meta.env.VITE_INSTANCE_NAME || 'Architecture Zero'
const PRIMARY_COLOR = import.meta.env.VITE_PRIMARY_COLOR || '#2563eb'
const INITIALS = INSTANCE_NAME.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()

function contrastColor(hex: string): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 ? '#000000' : '#ffffff'
}
const ON_PRIMARY = contrastColor(PRIMARY_COLOR)

interface ModelOption {
  value: string
  label: string
  badge: string
}

interface ModelGroup {
  provider: string
  label: string
  models: ModelOption[]
}

const DEFAULT_SUGGESTIONS = [
  'What can you help me with?',
  'Summarize or explain a document for me.',
  'Help me draft or improve some text.',
]

// ── Markdown code renderer (extracted to avoid re-creation on render) ───────

function renderCode({
  inline,
  className,
  children,
}: {
  inline?: boolean
  className?: string
  children?: React.ReactNode
}) {
  const match = /language-(\w+)/.exec(className || '')
  return !inline && match ? (
    <SyntaxHighlighter
      style={oneDark}
      language={match[1]}
      PreTag="div"
      customStyle={{ borderRadius: '0.5rem', fontSize: '0.8rem', margin: 0 }}
    >
      {String(children).replace(/\n$/, '')}
    </SyntaxHighlighter>
  ) : (
    <code className={className}>{children}</code>
  )
}

// ── TypingIndicator ────────────────────────────────────────────────────────

function TypingIndicator() {
  return (
    <div className="flex items-end gap-3 mb-6">
      <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0" style={{ backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY }}>
        {INITIALS}
      </div>
      <div className="bg-gray-800 border border-gray-700 px-4 py-3 rounded-2xl rounded-bl-sm flex items-center gap-1.5">
        <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
        <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
        <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
      </div>
    </div>
  )
}

// ── CitationsPanel ─────────────────────────────────────────────────────────

interface CitationsPanelProps {
  sources?: string[]
}

function CitationsPanel({ sources }: CitationsPanelProps) {
  // The chips name what the answer was grounded in - the claim a reader checks
  // it against. They are deliberately NOT clickable: opening one would need a
  // document-read endpoint this API does not expose, and a chip that always
  // answers "preview is not available" is a promise the platform does not keep.
  // Serving GET /api/kb/file would make them openable, and the shape below is
  // ready for it.
  if (!sources?.length) return null

  return (
    <div className="mt-2 pt-2 border-t border-gray-700/50">
      <p className="text-xs text-gray-500 mb-1.5">Sources</p>
      <div className="flex flex-wrap gap-1">
        {sources.map((s, i) => (
          <span key={i} title={s}
            className="text-xs bg-gray-700/50 text-gray-400 px-2 py-0.5 rounded-full border border-gray-600/50 truncate max-w-[200px]">
            {s}
          </span>
        ))}
      </div>
    </div>
  )
}

// ── ToolCallBadge ──────────────────────────────────────────────────────────

interface ToolCallBadgeProps {
  name: string
  result: string
}

function ToolCallBadge({ name, result }: ToolCallBadgeProps) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="mb-1.5 rounded-lg border border-yellow-700/40 bg-yellow-950/30 text-xs overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-yellow-900/20 transition-colors"
      >
        <span className="text-yellow-500">⚙</span>
        <span className="text-yellow-300 font-mono">{name}()</span>
        <span className="ml-auto text-yellow-700">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <pre className="px-3 pb-2 pt-1 text-gray-400 font-mono text-xs whitespace-pre-wrap break-words border-t border-yellow-800/30 overflow-auto max-h-48">
          {result}
        </pre>
      )}
    </div>
  )
}

// ── Message ────────────────────────────────────────────────────────────────

interface MessageProps {
  role: 'user' | 'assistant'
  content: string
  toolCalls?: ToolCall[]
  sources?: string[]
  msgIndex: number
  onFeedback?: (msgIndex: number, value: number) => void
  onRegenerate?: () => void
  onEdit?: (newContent: string) => void
  isStreaming?: boolean
}

function Message({ role, content, toolCalls, sources, msgIndex, onFeedback, onRegenerate, onEdit, isStreaming }: MessageProps) {
  const isUser = role === 'user'
  const [copied, setCopied] = useState(false)
  const [voted, setVoted] = useState<1 | -1 | null>(null)
  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState(content)

  const copy = () => {
    navigator.clipboard.writeText(content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const vote = (value: 1 | -1) => {
    if (voted === value) return
    setVoted(value)
    onFeedback?.(msgIndex, value)
  }

  const submitEdit = () => {
    if (!editText.trim()) return
    setEditing(false)
    setEditText(content)
    onEdit?.(editText.trim())
  }

  // Edit mode - replaces the user bubble with an inline textarea
  if (isUser && editing) {
    return (
      <div className="flex items-end gap-3 mb-6 flex-row-reverse">
        <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0" style={{ backgroundColor: '#4b5563' }}>
          You
        </div>
        <div className="max-w-[75%] flex flex-col gap-2 w-full">
          <textarea
            autoFocus
            value={editText}
            onChange={e => setEditText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitEdit() }}
            rows={Math.max(2, editText.split('\n').length)}
            className="w-full bg-gray-800 border border-blue-500/60 text-white text-sm rounded-xl px-3 py-2 resize-none outline-none leading-relaxed"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => { setEditing(false); setEditText(content) }}
              className="text-xs text-gray-500 hover:text-gray-300 px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={submitEdit}
              disabled={!editText.trim()}
              className="text-xs px-3 py-1.5 rounded-lg transition-colors disabled:opacity-40"
              style={{ backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY }}
            >
              Send
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={`flex items-end gap-3 mb-6 ${isUser ? 'flex-row-reverse' : ''}`}>
      <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0"
        style={{ backgroundColor: isUser ? '#4b5563' : PRIMARY_COLOR }}>
        {isUser ? 'You' : INITIALS}
      </div>
      <div className={`group relative max-w-[75%] ${isUser ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
        {!isUser && toolCalls && toolCalls.length > 0 && (
          <div className="w-full mb-1">
            {toolCalls.map((tc, i) => <ToolCallBadge key={i} name={tc.name} result={tc.result} />)}
          </div>
        )}
        <div className={`px-4 py-3 rounded-2xl text-sm leading-relaxed ${
          isUser
            ? 'rounded-br-sm whitespace-pre-wrap'
            : 'bg-gray-800 border border-gray-700 text-gray-100 rounded-bl-sm'
        }`} style={isUser ? { backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY } : {}}>
          {isUser ? content : (
            <ReactMarkdown
              className="prose prose-invert prose-sm max-w-none
                prose-p:my-1 prose-p:leading-relaxed
                prose-headings:text-white prose-headings:font-semibold prose-headings:mt-3 prose-headings:mb-1
                prose-ul:my-1 prose-ul:pl-4 prose-ol:my-1 prose-ol:pl-4
                prose-li:my-0.5
                prose-strong:text-white
                prose-code:text-blue-300 prose-code:bg-gray-900 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs
                prose-pre:p-0 prose-pre:bg-transparent prose-pre:my-2"
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              components={{ ...SAFE_MARKDOWN_COMPONENTS, code: renderCode } as any}
            >
              {content}
            </ReactMarkdown>
          )}
          {!isUser && <CitationsPanel sources={sources} />}
        </div>
        {isUser && onEdit && !isStreaming && (
          <div className="flex justify-end opacity-0 group-hover:opacity-100 transition-opacity px-1">
            <button onClick={() => setEditing(true)} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
              Edit
            </button>
          </div>
        )}
        {!isUser && (
          <div className="flex items-center gap-3 opacity-0 group-hover:opacity-100 transition-opacity px-1">
            <div className="flex items-center gap-1">
              <button
                onClick={() => vote(1)}
                title="Good response"
                className={`text-base transition-colors ${voted === 1 ? 'text-green-400' : 'text-gray-600 hover:text-gray-300'}`}
              >
                &#128077;
              </button>
              <button
                onClick={() => vote(-1)}
                title="Bad response"
                className={`text-base transition-colors ${voted === -1 ? 'text-red-400' : 'text-gray-600 hover:text-gray-300'}`}
              >
                &#128078;
              </button>
            </div>
            <button onClick={copy} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
              {copied ? '✓ Copied' : 'Copy'}
            </button>
            {onRegenerate && !isStreaming && (
              <button onClick={onRegenerate} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
                Regenerate
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Toggle ─────────────────────────────────────────────────────────────────

interface ToggleProps {
  enabled: boolean
  onToggle: () => void
}

function Toggle({ enabled, onToggle }: ToggleProps) {
  return (
    <button
      onClick={onToggle}
      className={`relative w-10 h-5 rounded-full transition-colors duration-200 flex-shrink-0 ${enabled ? '' : 'bg-gray-600'}`}
      style={enabled ? { backgroundColor: PRIMARY_COLOR } : {}}
    >
      <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${enabled ? 'left-5' : 'left-0.5'}`} />
    </button>
  )
}

// ── App ────────────────────────────────────────────────────────────────────

const API = import.meta.env.VITE_API_URL || ''

const authHeaders = (): Record<string, string> => {
  const token = localStorage.getItem('az_jwt_token')
  return token ? { 'Authorization': `Bearer ${token}` } : {}
}

function sessionKey(userId?: number) {
  return userId ? `az_session_id_${userId}` : 'az_session_id'
}

function getOrCreateSession(userId?: number): string {
  const key = sessionKey(userId)
  let id = localStorage.getItem(key)
  if (!id) {
    id = crypto.randomUUID()
    localStorage.setItem(key, id)
  }
  return id
}

interface AuthUser {
  id: number
  username: string
  role: string
  permissions: string[]
}

const GUEST_TURN_LIMIT = 10

type View = 'loading' | 'login' | 'chat' | 'admin'

export default function App() {
  const [view, setView] = useState<View>('loading')
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null)
  const [isGuest, setIsGuest] = useState(false)
  const [guestModeEnabled, setGuestModeEnabled] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
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
  const [modelGroups, setModelGroups] = useState<ModelGroup[]>([])
  const [model, setModel] = useState('')
  const [useRag, setUseRag] = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [sessionId, setSessionId] = useState<string>(getOrCreateSession)
  const [sessionList, setSessionList] = useState<SessionEntry[]>([])
  const [sysStatus, setSysStatus] = useState<SysStatus | null>(null)
  // Display names for the providers this template can route to. Anything not
  // listed renders under its own id rather than being silently relabelled -
  // a wrong provider name is the bug this replaced.
  const PROVIDER_LABELS: Record<string, string> = {
    ollama: 'local model via Ollama', anthropic: 'Claude API', openai: 'OpenAI API',
    gemini: 'Gemini API', mistral: 'Mistral API', groq: 'Groq API',
    xai: 'xAI API', deepseek: 'DeepSeek API',
  }
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
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

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
        // Nobody to sign in as yet: route the operator to the wizard instead of
        // a login screen with no account behind it. AFTER the token restore, not
        // before - this repo has no last-admin guard, so deactivating the last
        // admin flips needs_setup back to true on a CLAIMED instance, and a
        // signed-in user must not be bounced to a claim form whose code was
        // burned at first claim.
        //
        // The reload is load-bearing: main.tsx builds `page` once at module
        // scope with no hashchange listener, so setting the hash alone renders
        // nothing. It cannot loop - at '#setup' main.tsx picks Setup over App,
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
    }>(fetch(`${API}/api/config`, { headers: authHeaders() }), 'Loading settings')
      .then(d => {
        if (!d) return
        if (d.suggestions && d.suggestions.length > 0) setSuggestions(d.suggestions)
        if (d.allow_model_selection !== undefined) setAllowModelSelection(d.allow_model_selection)
        if (d.allow_rag_toggle !== undefined) setAllowRagToggle(d.allow_rag_toggle)
        if (d.default_model) setModel(d.default_model)
        if (d.default_rag_enabled !== undefined) setUseRag(d.default_rag_enabled)
        if (d.guest_mode_enabled !== undefined) setGuestModeEnabled(d.guest_mode_enabled)
        if (d.instance_name) setInstanceName(d.instance_name)
        // The server's EFFECTIVE model - what actually answers. The backend
        // sends it precisely so a client does not display its own copy and
        // quietly misreport a server-side pin.
        if (d.chat_model_effective) setEffectiveModel(d.chat_model_effective)
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
    setCurrentUser(resolved)
    setIsGuest(false)
    // Clear the transcript on the way IN as well as out. The history effect
    // below replaces messages only `if (data?.messages?.length)`, so signing
    // into an account whose session is empty left the PREVIOUS account's
    // conversation rendered - and since send() builds its history array from
    // messages, that transcript was then posted with the new account's token
    // and read by the model as context for their questions.
    setMessages([])
    setSessionId(getOrCreateSession(resolved.id))
    setView('chat')
  }

  const handleGuest = () => {
    setIsGuest(true)
    setCurrentUser(null)
    setMessages([])
    setSessionId(crypto.randomUUID())
    setView('chat')
  }

  const handleLogout = async () => {
    try {
      await fetch(`${API}/api/auth/logout`, { method: 'POST', headers: authHeaders() })
    } catch { /* ignore */ }
    localStorage.removeItem('az_jwt_token')
    localStorage.removeItem('az_jwt_refresh')
    setCurrentUser(null)
    setIsGuest(false)
    // Signing out has to leave nothing on screen for the next person at this
    // machine. Only handleGuest cleared this before, so a shared browser kept
    // the previous account's conversation visible behind the login form and
    // straight through the next sign-in.
    setMessages([])
    setInput('')
    setView('login')
  }

  // Chat data polling - hooks must be declared before any early returns
  useEffect(() => {
    if (view !== 'chat') return
    const fetchStatus = () => {
      // /api/status is authenticated here, so a guest has nothing to send.
      if (isGuest || !currentUser) return
      guardedPoll<SysStatus>(fetch(`${API}/api/status`, { headers: authHeaders() }))
        .then(d => { if (d) setSysStatus(d) })
    }
    const fetchAnalytics = () => {
      // Guests have no view_analytics permission - their 401 here is by design,
      // not an expired session; skip instead of tripping the expired banner.
      if (isGuest) return
      guardedPoll<Analytics>(fetch(`${API}/api/analytics`, { headers: authHeaders() }))
        .then(d => { if (d) setAnalytics(d) })
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
        .then(d => { if (d) setSessionList(d.sessions || []) })
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
    guardedJson<{ messages?: Array<{ role: 'user' | 'assistant'; content: string }> }>(
      fetch(`${API}/api/history/${sessionId}`, { headers: authHeaders() }), 'Loading chat history')
      .then(data => {
        if (data?.messages?.length) {
          setMessages(data.messages.map(m => ({ role: m.role, content: m.content })))
        }
      })
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
    localStorage.removeItem('az_jwt_token')
    localStorage.removeItem('az_jwt_refresh')
    setCurrentUser(null)
    setProfileOpen(false)
    setView('login')
  }

  const switchSession = (id: string) => {
    localStorage.setItem(sessionKey(currentUser?.id), id)
    setSessionId(id)
    setMessages([])
  }

  const deleteSession = async (id: string) => {
    await fetch(`${API}/api/history/${id}`, { method: 'DELETE', headers: authHeaders() })
    setSessionList(prev => prev.filter(s => s.session !== id))
    if (id === sessionId) {
      const newId = crypto.randomUUID()
      localStorage.setItem(sessionKey(currentUser?.id), newId)
      setSessionId(newId)
      setMessages([])
    }
  }

  const handleFeedback = async (msgIndex: number, value: number) => {
    // The user clicked a thumb - a swallowed failure means they believe the
    // label landed when it didn't (the training-label primitive, silently lost).
    const err = await actionError(fetch(`${API}/api/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ session_id: sessionId, turn_index: msgIndex, value }),
    }), 'Saving feedback')
    if (err) emitError(err)
  }

  const newChat = () => {
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

  // Core SSE stream handler - prompt is already in messages state when called
  const sendCore = async (prompt: string, historyForRequest: Array<{ role: string; content: string }>) => {
    const controller = new AbortController()
    abortRef.current = controller
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

    try {
      const res = await fetch(`${API}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ prompt, model, use_rag: useRag, history: historyForRequest, session_id: sessionId }),
        signal: controller.signal,
      })

      if (!res.ok) {
        // Surface the server's own message (guest limit, widget-only guest scope,
        // guest mode off) instead of masking it as a connectivity failure.
        const errData = await res.json().catch(() => ({}))
        const fallback = res.status === 429
          ? 'Guest limit reached. Sign in to continue.'
          : `Error: the server returned ${res.status}.`
        setMessages(prev => [...prev, { role: 'assistant', content: errData.detail || fallback }])
        setLoading(false)
        return
      }
      if (!res.body) throw new Error('No response body')

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      // Carry buffer: a `data:` line can straddle network chunks, and
      // splitting each chunk in isolation silently drops the straddled line's
      // tokens. The answer still renders, just missing pieces - which is why
      // this is easy to ship and hard to notice.
      let buf = ''
      let assistantStarted = false
      let assistantMsg = ''
      let toolCalls: ToolCall[] = []
      let sources: string[] = []

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
              if (!assistantStarted) {
                setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${data.error}` }])
                setLoading(false)
                assistantStarted = true
              }
              continue
            }

            if (!assistantStarted && (data.sources || data.tool_call || data.token)) {
              setMessages(prev => {
                slot = prev.length            // this stream's own bubble, for good
                return [...prev, { role: 'assistant', content: '', toolCalls: [], sources: [] }]
              })
              setLoading(false)
              assistantStarted = true
            }

            if (data.sources) {
              sources = data.sources
              setMessages(prev => {
                const next = [...prev]
                if (slot < 0 || slot >= next.length) return prev
                next[slot] = { role: 'assistant', content: assistantMsg, toolCalls, sources }
                return next
              })
            }

            if (data.tool_call) {
              toolCalls = [...toolCalls, data.tool_call]
              setMessages(prev => {
                const next = [...prev]
                if (slot < 0 || slot >= next.length) return prev
                next[slot] = { role: 'assistant', content: assistantMsg, toolCalls, sources }
                return next
              })
            }

            if (data.context_warning) setContextWarning(true)
            if (data.context_summarized) setContextSummarized(true)

            if (data.token) {
              assistantMsg += data.token
              setMessages(prev => {
                const next = [...prev]
                if (slot < 0 || slot >= next.length) return prev
                next[slot] = { role: 'assistant', content: assistantMsg, toolCalls, sources }
                return next
              })
            }
          } catch { /* malformed chunk, skip */ }
        }
      }
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') {
        setLoading(false)
        return
      }
      setMessages(prev => [...prev, { role: 'assistant', content: 'Error: Could not reach the backend.' }])
      setLoading(false)
    } finally {
      // Only retract the controller if it is still OURS. Clearing it blind let
      // an older stream's finally null a newer stream's controller, which left
      // Stop inert for the rest of the conversation once two had ever overlapped.
      if (abortRef.current === controller) abortRef.current = null
      setStreaming(false)
      setLoading(false)
    }
  }

  const providerId = sysStatus?.provider?.provider
  const providerLabel = providerId ? (PROVIDER_LABELS[providerId] || providerId) : ''
  const guestTurnCount = isGuest ? messages.filter(m => m.role === 'user').length : 0
  const guestAtLimit = isGuest && guestTurnCount >= GUEST_TURN_LIMIT
  // One name for "a request is in flight", covering both the pre-first-token
  // wait and the stream itself. Every entry point guards on this.
  const busy = loading || streaming

  const send = async (text?: string) => {
    const prompt = (text || input).trim()
    if (!prompt || busy || guestAtLimit) return
    setInput('')
    setContextWarning(false)
    setContextSummarized(false)
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(prev => [...prev, { role: 'user', content: prompt }])
    setLoading(true)
    await sendCore(prompt, history)
  }

  const stopGeneration = () => {
    abortRef.current?.abort()
  }

  const regenerate = async () => {
    if (busy || messages.length < 2) return
    if (messages[messages.length - 1].role !== 'assistant') return
    const lastUserMsg = messages[messages.length - 2]
    if (lastUserMsg.role !== 'user') return

    const truncated = messages.slice(0, -2)
    const history = truncated.map(m => ({ role: m.role, content: m.content }))
    const prompt = lastUserMsg.content

    const trimErr = await actionError(fetch(`${API}/api/history/${sessionId}/tail?count=2`, {
      method: 'DELETE', headers: authHeaders(),
    }), 'Trimming history')
    if (trimErr) emitError(trimErr) // proceed - regeneration still works, server history may duplicate

    setContextWarning(false)
    setContextSummarized(false)
    setMessages([...truncated, { role: 'user', content: prompt }])
    setLoading(true)
    await sendCore(prompt, history)
  }

  const editAndRegenerate = async (msgIndex: number, newContent: string) => {
    if (busy) return
    const truncated = messages.slice(0, msgIndex)
    const countToRemove = messages.length - msgIndex
    const history = truncated.map(m => ({ role: m.role, content: m.content }))

    const trimErr = await actionError(fetch(`${API}/api/history/${sessionId}/tail?count=${countToRemove}`, {
      method: 'DELETE', headers: authHeaders(),
    }), 'Trimming history')
    if (trimErr) emitError(trimErr) // proceed - the edit still sends, server history may duplicate

    setContextWarning(false)
    setContextSummarized(false)
    setMessages([...truncated, { role: 'user', content: newContent }])
    setLoading(true)
    await sendCore(newContent, history)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send()
  }

  const onInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = Math.min(e.target.scrollHeight, 180) + 'px'
  }

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
        <aside className="w-64 bg-gray-900 border-r border-gray-800 flex flex-col flex-shrink-0">
          <div className="p-5 border-b border-gray-800">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl flex items-center justify-center font-bold text-sm" style={{ backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY }}>{initials}</div>
              <div>
                <p className="font-semibold text-sm leading-tight">{instanceName}</p>
                <p className="text-xs text-gray-500">Secure AI over your knowledge base</p>
              </div>
            </div>
          </div>


          <div className="p-3 border-b border-gray-800">
            <button
              onClick={newChat}
              className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-gray-400 hover:text-white hover:bg-gray-800 border border-gray-700 hover:border-gray-600 transition-all"
            >
              <span className="text-base font-light">+</span> New Chat
            </button>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-5">
            {!isGuest && sessionList.length > 0 && (
              <div>
                <p className="text-xs text-gray-500 uppercase tracking-widest mb-2 px-1">History</p>
                <div className="space-y-0.5">
                  {sessionList.map(s => (
                    <div
                      key={s.session}
                      className={`group flex items-center rounded-lg text-xs transition-all ${
                        s.session === sessionId ? '' : 'text-gray-400 hover:bg-gray-800 hover:text-white'
                      }`}
                      style={s.session === sessionId ? { backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY } : {}}
                    >
                      <button
                        onClick={() => switchSession(s.session)}
                        className="flex-1 text-left px-3 py-2 truncate"
                        title={s.first_message || 'Empty session'}
                      >
                        {(s.first_message || 'New conversation').slice(0, 36)}
                        {(s.first_message || '').length > 36 ? '…' : ''}
                      </button>
                      <button
                        onClick={() => deleteSession(s.session)}
                        className="opacity-0 group-hover:opacity-100 pr-2 text-gray-500 hover:text-red-400 transition-all flex-shrink-0"
                        title="Delete conversation"
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {allowModelSelection && modelGroups.length > 0 && (
              <div className="space-y-3">
                {modelGroups.map(group => (
                  <div key={group.provider}>
                    <p className="text-xs text-gray-500 uppercase tracking-widest mb-1 px-1">{group.label}</p>
                    <div className="space-y-1">
                      {group.models.map((m: ModelOption) => (
                        <button
                          key={m.value}
                          onClick={() => setModel(m.value)}
                          className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm transition-all ${
                            model === m.value ? '' : 'text-gray-400 hover:bg-gray-800 hover:text-white'
                          }`}
                          style={model === m.value ? { backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY } : {}}
                        >
                          <span className="truncate text-left">{m.label}</span>
                          <span className={`text-xs px-1.5 py-0.5 rounded ml-2 flex-shrink-0 ${
                            model === m.value ? 'bg-blue-500/60 text-blue-100' : 'bg-gray-700 text-gray-400'
                          }`}>{m.badge}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {allowRagToggle && (
              <div>
                <p className="text-xs text-gray-500 uppercase tracking-widest mb-2 px-1">Knowledge Base</p>
                <div className={`flex items-center justify-between px-3 py-2.5 rounded-lg border text-sm transition-all ${
                  useRag ? 'bg-blue-600/10 border-blue-500/40 text-blue-300' : 'bg-gray-800/50 border-gray-700 text-gray-400'
                }`}>
                  <span>RAG Mode</span>
                  <Toggle enabled={useRag} onToggle={() => setUseRag(!useRag)} />
                </div>
              </div>
            )}
          </div>

          <div className="p-4 border-t border-gray-800 space-y-2">
            {(() => {
              const _prov = sysStatus?.provider?.provider ?? 'ollama'
              const _cloud = _prov !== 'ollama'
              const _ok = _cloud ? !!sysStatus : sysStatus?.ollama === 'connected'
              const _label = _cloud ? 'connected' : (sysStatus?.ollama ?? '…')
              return (
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className={`w-2 h-2 rounded-full ${_ok ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
                    <span className="text-xs text-gray-500 capitalize">{_prov}</span>
                  </div>
                  <span className={`text-xs ${_ok ? 'text-green-500' : 'text-red-400'}`}>{_label}</span>
                </div>
              )
            })()}
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500" title="Answers are grounded in this many documents - the assistant won't make things up beyond them">RAG Docs</span>
              <span className="text-xs text-gray-400">{sysStatus?.rag_documents ?? '…'}</span>
            </div>
            {sysStatus?.auth_enabled && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-500">Auth</span>
                <span className="text-xs text-blue-400">enabled</span>
              </div>
            )}
            {sysStatus?.rag_only_mode && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-500" title="Strict grounding: if the answer isn't in the documents, the assistant says so instead of guessing">RAG Only</span>
                <span className="text-xs text-orange-400">enforced</span>
              </div>
            )}
            {sysStatus?.agent_tools?.agent_enabled && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-500">Agent Tools</span>
                <span className="text-xs text-yellow-400">active</span>
              </div>
            )}
            {analytics && (
              <>
                <div className="border-t border-gray-800 my-1" />
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Sessions</span>
                  <span className="text-xs text-gray-400">{analytics.total_sessions}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Requests today</span>
                  <span className="text-xs text-gray-400">{analytics.requests_today}</span>
                </div>
                {analytics.feedback && analytics.feedback.total > 0 && (
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-gray-500">Feedback</span>
                    <span className="text-xs text-gray-400">
                      👍 {analytics.feedback.thumbs_up} · 👎 {analytics.feedback.thumbs_down}
                    </span>
                  </div>
                )}
              </>
            )}
          </div>
        </aside>
      )}

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">

        {/* Header */}
        <header className="h-14 border-b border-gray-800 bg-gray-900/40 flex items-center px-4 gap-3 flex-shrink-0 backdrop-blur-sm">
          <button onClick={() => setSidebarOpen(!sidebarOpen)} className="text-gray-500 hover:text-white transition-colors p-1 rounded">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
          <div className="flex-1" />
          {/* The measured-trust page is the campaign's proof surface - it must be
              reachable from the front door, not only from inside the admin tour. */}
          <a
            href="/#trust"
            target="_blank"
            rel="noopener noreferrer"
            title="How much should you trust this assistant? Live-derived evaluation numbers - honesty, correctness, a locked holdout exam, grounding, freshness"
            className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
          >
            Trust
          </a>
          {/* The server's effective model when it has told us one - a pinned
              model must not be misreported as whatever this client last chose. */}
          <span className="text-xs text-gray-500 bg-gray-800 px-2.5 py-1 rounded-full border border-gray-700">{effectiveModel || model}</span>
          {currentUser && (currentUser.role === 'owner' || currentUser.role === 'admin' || currentUser.permissions?.some(p => ['manage_users', 'manage_system', 'manage_kb', 'view_analytics'].includes(p))) && (
            <button
              onClick={() => setView('admin')}
              title={currentUser ? 'Admin panel' : 'Read-only tour of the system behind this assistant: settings, knowledge base, models, and federation'}
              className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
            >
              {currentUser ? 'Admin' : 'System'}
            </button>
          )}
          {isGuest && (
            <span className="text-xs text-gray-500 bg-gray-800 px-2.5 py-1 rounded-full border border-gray-700">
              Guest
            </span>
          )}
          {/* Any unauthenticated viewer gets the door. A signed-out visitor
              who is not flagged as a guest is still someone who may have an
              account, and keying this off anything narrower has already
              produced an instance whose owner could not reach a login screen. */}
          {!currentUser && (
            <button
              onClick={() => setView('login')}
              title="Owner / admin sign in"
              className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
            >
              Sign in
            </button>
          )}
          {currentUser && (
            <>
              <button
                onClick={() => setProfileOpen(true)}
                className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
                title="Account settings"
              >
                {currentUser.username}
              </button>
              <button
                onClick={handleLogout}
                className="text-xs text-gray-500 hover:text-red-400 transition-colors"
              >
                Sign out
              </button>
            </>
          )}
        </header>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-6 py-8">
          {messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center max-w-lg mx-auto">
              <div className="w-16 h-16 rounded-2xl flex items-center justify-center text-xl font-bold mb-5 shadow-lg" style={{ backgroundColor: PRIMARY_COLOR }}>
                {initials}
              </div>
              <h2 className="text-xl font-semibold text-white mb-2">{instanceName}</h2>
              <p className="text-gray-500 text-sm mb-8">
                {(sysStatus?.provider?.provider ?? 'ollama') === 'ollama'
                  ? 'A private, local AI assistant. Your data never leaves this machine.'
                  : 'A cloud-powered AI assistant. Conversations are processed securely via API.'}
              </p>
              <div className="w-full space-y-2">
                {suggestions.map(s => (
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
                  sources={m.sources} msgIndex={i}
                  isStreaming={busy}
                  // Guests have no session; /api/feedback needs one, and a 401 here
                    // raises the sticky session-expired banner at someone who never
                    // logged in. Withhold the control rather than the error.
                    onFeedback={m.role === 'assistant' && !isGuest ? handleFeedback : undefined}
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
            <div className="flex gap-3 items-end bg-gray-800 border border-gray-700 focus-within:border-blue-500/60 rounded-2xl px-4 py-3 transition-colors">
              <textarea
                ref={textareaRef}
                className="flex-1 bg-transparent text-white text-sm resize-none outline-none placeholder-gray-500 leading-relaxed disabled:opacity-50"
                placeholder={guestAtLimit ? 'Sign in to continue chatting…' : `Message ${instanceName}...  (Ctrl+Enter to send)`}
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
            {/* Names the provider ACTUALLY answering, from /api/status. It read
                "Claude API" hardcoded - on a template that speaks to Ollama,
                Anthropic, OpenAI, Gemini, Mistral, Groq, xAI and DeepSeek, and
                whose shipped default is local Ollama with no Anthropic key
                configured at all. So the stock deployment credited a vendor it
                was not using, in the one line a visitor reads to find out what
                answered them. Guests get no /api/status, so the segment is
                omitted rather than guessed. */}
            <p className="text-center text-xs text-gray-600 mt-2">
              Powered by Architecture Zero{providerLabel ? ` · ${providerLabel}` : ''} · responses are AI-generated
            </p>
          </div>
        </div>

      </div>
    </div>
  )
}
