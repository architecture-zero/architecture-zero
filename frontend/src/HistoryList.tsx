/**
 * The chat sidebar's History list (2026-10-03, the 2026-08-01 UI audit's
 * sessions drawer): conversations grouped Today / Last 7 days / Older by
 * their last activity, each renamable in place, and a delete that
 * asks once and leaves the row where it was when the server refuses - it used
 * to drop the row whatever the response said, and the next poll put it back.
 *
 * One file, the same text on every surface that ships the chassis chat. The
 * caller owns the requests: onRename and onDelete resolve true only when the
 * server accepted, and the caller updates the list it passes back in.
 */
import { useState } from 'react'
import type { CSSProperties } from 'react'

export interface HistoryEntry {
  session: string
  first_message?: string | null
  name?: string | null
  last_at?: string | null
}

// The server stamps naive UTC ISO strings (datetime.utcnow().isoformat()). A
// stamp with no zone is UTC - parsed as local time it would move the day's edge.
export function parseStamp(stamp: string | null | undefined): number | null {
  if (!stamp) return null
  const iso = /(Z|[+-]\d\d:?\d\d)$/i.test(stamp) ? stamp : `${stamp}Z`
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

const DAY_MS = 86_400_000

// Today is since local midnight; Last 7 days the seven days before it; a row
// with no readable stamp counts as Older rather than disappearing.
export function groupByActivity(entries: HistoryEntry[], now: Date = new Date()) {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const groups: { label: string; items: HistoryEntry[] }[] = [
    { label: 'Today', items: [] }, { label: 'Last 7 days', items: [] }, { label: 'Older', items: [] },
  ]
  for (const e of entries) {
    const t = parseStamp(e.last_at)
    groups[t === null ? 2 : t >= today ? 0 : t >= today - 7 * DAY_MS ? 1 : 2].items.push(e)
  }
  return groups.filter(g => g.items.length > 0)
}

export const titleOf = (e: HistoryEntry) =>
  e.name?.trim() || e.first_message?.trim() || 'New conversation'

const ROW_BUTTON = 'sm:opacity-0 sm:group-hover:opacity-100 focus-visible:opacity-100 text-gray-500 transition-all flex-shrink-0'

export default function HistoryList({ entries, activeId, activeStyle, onSelect, onRename, onDelete }: {
  entries: HistoryEntry[]
  activeId: string
  activeStyle: CSSProperties
  onSelect: (id: string) => void
  onRename: (id: string, name: string) => Promise<boolean>
  onDelete: (id: string) => Promise<boolean>
}) {
  const [renaming, setRenaming] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [confirming, setConfirming] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const startRename = (e: HistoryEntry) => {
    setConfirming(null)
    setRenaming(e.session)
    setDraft(titleOf(e))
  }

  const commitRename = async (e: HistoryEntry) => {
    const name = draft.trim()
    setRenaming(null)
    if (!name || name === titleOf(e)) return
    setBusy(e.session)
    await onRename(e.session, name)
    setBusy(null)
  }

  const confirmDelete = async (id: string) => {
    setBusy(id)
    await onDelete(id)
    setBusy(null)
    setConfirming(null)
  }

  return (
    <div className="space-y-4">
      {groupByActivity(entries).map(g => (
        <div key={g.label}>
          <p className="text-xs text-gray-500 uppercase tracking-widest mb-2 px-1">{g.label}</p>
          <div className="space-y-0.5">
            {g.items.map(e => {
              if (renaming === e.session) {
                return (
                  <input
                    key={e.session}
                    autoFocus
                    value={draft}
                    maxLength={300}
                    aria-label="Conversation name"
                    onChange={ev => setDraft(ev.target.value)}
                    onKeyDown={ev => {
                      if (ev.key === 'Enter') { ev.preventDefault(); void commitRename(e) }
                      if (ev.key === 'Escape') setRenaming(null)
                    }}
                    onBlur={() => setRenaming(null)}
                    className="w-full bg-gray-900 border border-gray-600 focus:border-gray-400 rounded-lg px-3 py-1.5 text-xs text-white outline-none"
                  />
                )
              }
              if (confirming === e.session) {
                return (
                  <div key={e.session} className="flex items-center gap-3 px-3 py-1.5 rounded-lg bg-gray-800 text-xs">
                    <span className="flex-1 min-w-0 text-gray-300 truncate">Delete this chat?</span>
                    <button onClick={() => setConfirming(null)} className="text-gray-400 hover:text-white">
                      Cancel
                    </button>
                    <button
                      onClick={() => void confirmDelete(e.session)}
                      disabled={busy === e.session}
                      className="text-red-400 hover:text-red-300 font-medium disabled:opacity-50"
                    >
                      Delete
                    </button>
                  </div>
                )
              }
              const active = e.session === activeId
              return (
                <div
                  key={e.session}
                  className={`group flex items-center rounded-lg text-xs transition-all ${
                    active ? '' : 'text-gray-400 hover:bg-gray-800 hover:text-white'
                  } ${busy === e.session ? 'opacity-60' : ''}`}
                  style={active ? activeStyle : {}}
                >
                  <button
                    onClick={() => onSelect(e.session)}
                    className="flex-1 min-w-0 text-left px-3 py-2 truncate"
                    title={titleOf(e)}
                  >
                    {titleOf(e)}
                  </button>
                  <button
                    onClick={() => startRename(e)}
                    className={`${ROW_BUTTON} px-1 hover:text-white`}
                    title="Rename conversation"
                    aria-label="Rename conversation"
                  >
                    ✎
                  </button>
                  <button
                    onClick={() => { setRenaming(null); setConfirming(e.session) }}
                    className={`${ROW_BUTTON} pl-1 pr-2 hover:text-red-400`}
                    title="Delete conversation"
                    aria-label="Delete conversation"
                  >
                    ✕
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}
