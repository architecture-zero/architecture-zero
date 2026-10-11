// The transcript's shapes: a tool call the agent made and the bubble it sits
// under - read by the shell, the stream hook and the Message component (the App
// split, 2026-10-11).
export interface ToolCall {
  name: string
  result: string
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  toolCalls?: ToolCall[]
  sources?: string[]
  // TRUE when this answer came from the help lane: its help/ sources open
  // through the help page read. Keyed on the lane, not the source name - an
  // operator file under knowledge/help/ carries the same prefix in a normal
  // answer and must not render as an openable product page.
  help?: boolean
  // TRUE while this bubble has NO row in stored history. That is the whole
  // meaning - not "is a notice", which is what it meant when it was introduced
  // and why it missed the cases that mattered. The distinction is load-bearing
  // in two places: the regenerate/edit trim sends a COUNT to
  // DELETE /api/history/{id}/tail, which deletes N rows by id with no role
  // awareness, and send() posts the transcript back as model context. Counting
  // an unstored bubble as a stored row deletes one row too many - permanently,
  // silently, and one row too far back is somebody's previous answer.
  //
  // The server writes the user row before the stream opens (chat.py) and the
  // assistant row only after the stream completes cleanly, so bubbles START
  // ephemeral and are cleared when the write is known to have happened. The
  // previous version flagged the three notice bubbles and trusted everything
  // else, which left the streaming bubble itself - unstored on every abort and
  // every mid-stream provider failure - counted as a stored row.
  ephemeral?: boolean
  // Presentational text shown under the bubble and NEVER posted back as model
  // context. It exists because the mid-stream failure notice used to be
  // concatenated into `content`: flagging that bubble ephemeral would then have
  // dropped the genuine partial answer from context, and leaving it unflagged
  // posted "the provider failed mid-response" to the model as a turn it never
  // took. Keeping the two in separate fields is what lets both be correct.
  notice?: string
}

export interface SessionEntry {
  session: string
  first_message?: string
  // The conversation's name (renamed in the sidebar, or the template's
  // auto-name from its first prompt) and its last activity, which the list
  // groups by (2026-10-03).
  name?: string | null
  last_at?: string | null
}

export interface SysStatus {
  ollama: string
  rag_documents: number
  auth_enabled?: boolean
  rag_only_mode?: boolean
  agent_tools?: { agent_enabled: boolean }
  provider?: { provider: string }
  // Optional on purpose: /api/status requires a login on this backend, so the
  // boot decision never reads it from here (it comes from /api/auth/config),
  // and a backend predating this key must not become a type error.
  needs_setup?: boolean
}

export interface Analytics {
  total_sessions: number
  requests_today: number
  feedback?: {
    total: number
    thumbs_up: number
    thumbs_down: number
  }
}

export interface ModelOption {
  value: string
  label: string
  badge: string
}

export interface ModelGroup {
  provider: string
  label: string
  models: ModelOption[]
}

export interface AuthUser {
  id: number
  username: string
  role: string
  permissions: string[]
}

export type View = 'loading' | 'login' | 'chat' | 'admin'
