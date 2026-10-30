import { useState, useEffect } from 'react'
import { emitAuthExpired, emitError, guardedJson } from '../errorSurface'
import { PRIMARY_COLOR } from '../theme'

// ── Tab: Audit Log ─────────────────────────────────────────────────────────

// A failed export is said, never saved (2026-10-30). Export CSV wrote whatever
// came back to audit_log.csv without reading the status, so an expired session
// downloaded the error JSON as the file, and a request that never reached the
// server was swallowed - with no word either way. A fixed sentence, never the
// server's words.
const EXPORT_FAILED = 'Exporting the audit log failed. No file was saved.'

interface AuditEntry {
  id: number
  username: string
  session_id: string
  timestamp: string
  prompt_preview: string
  response_length: number
  model: string | null
  use_rag: boolean
  sources: string[]
  // Output-side PII receipt (2026-09-16). null = the output filter was off
  // (or the row predates it); 0 hits = scanned, nothing found.
  pii_out_hits: number | null
  pii_out_redacted: number | null
  pii_out_types: string | null
}

export function AuditTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [entries, setEntries]     = useState<AuditEntry[]>([])
  const [total, setTotal]         = useState(0)
  const [pages, setPages]         = useState(1)
  const [page, setPage]           = useState(1)
  const [loading, setLoading]     = useState(true)
  const [exporting, setExporting] = useState(false)
  const [filterUser, setFilterUser] = useState('')
  const [filterFrom, setFilterFrom] = useState('')
  const [filterTo, setFilterTo]     = useState('')

  const load = (p = page) => {
    setLoading(true)
    const params = new URLSearchParams({ page: String(p), page_size: '50' })
    if (filterUser) params.set('username', filterUser)
    if (filterFrom) params.set('date_from', filterFrom)
    if (filterTo)   params.set('date_to', filterTo)
    guardedJson<{ entries?: AuditEntry[]; total?: number; pages?: number }>(
      fetch(`${api}/api/admin/audit?${params}`, { headers: headers() }), 'Loading audit log')
      .then(d => {
        if (!d) return
        setEntries(d.entries || [])
        setTotal(d.total || 0)
        setPages(d.pages || 1)
        setPage(p)
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => { load(1) }, [])

  const exportCsv = async () => {
    setExporting(true)
    try {
      const params = new URLSearchParams()
      if (filterUser) params.set('username', filterUser)
      if (filterFrom) params.set('date_from', filterFrom)
      if (filterTo)   params.set('date_to', filterTo)
      const res = await fetch(`${api}/api/admin/audit/export?${params}`, { headers: headers() })
      if (!res.ok) {
        // A 401 also raises the session-expired banner, as every action's does.
        if (res.status === 401) emitAuthExpired()
        emitError(EXPORT_FAILED)
        return
      }
      const text = await res.text()
      const blob = new Blob([text], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'audit_log.csv'
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      emitError(EXPORT_FAILED)
    } finally {
      setExporting(false)
    }
  }

  const fmtTs = (ts: string) => {
    try { return new Date(ts + 'Z').toLocaleString() } catch { return ts }
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Username</label>
          <input
            value={filterUser} onChange={e => setFilterUser(e.target.value)}
            placeholder="Filter by user…"
            className="bg-gray-800 border border-gray-700 text-white text-xs rounded-lg px-3 py-2 outline-none focus:border-blue-500/60 w-44"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">From</label>
          <input type="date" value={filterFrom} onChange={e => setFilterFrom(e.target.value)}
            className="bg-gray-800 border border-gray-700 text-white text-xs rounded-lg px-3 py-2 outline-none focus:border-blue-500/60"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">To</label>
          <input type="date" value={filterTo} onChange={e => setFilterTo(e.target.value)}
            className="bg-gray-800 border border-gray-700 text-white text-xs rounded-lg px-3 py-2 outline-none focus:border-blue-500/60"
          />
        </div>
        <button
          onClick={() => load(1)}
          className="text-xs text-white px-4 py-2 rounded-lg transition-colors"
          style={{ backgroundColor: PRIMARY_COLOR }}
        >
          Search
        </button>
        <button
          onClick={exportCsv}
          disabled={exporting}
          className="text-xs text-gray-400 hover:text-white px-4 py-2 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors disabled:opacity-50 ml-auto"
        >
          {exporting ? 'Exporting…' : 'Export CSV'}
        </button>
      </div>

      {/* Summary */}
      <p className="text-xs text-gray-500">{total} total entries</p>

      {/* Table */}
      {loading ? (
        <p className="text-xs text-gray-600">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="text-xs text-gray-600">No audit entries found.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-gray-800">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-800 text-gray-500 uppercase tracking-wider">
                <th className="text-left px-4 py-2 font-medium">Timestamp</th>
                <th className="text-left px-4 py-2 font-medium">User</th>
                <th className="text-left px-4 py-2 font-medium">Session</th>
                <th className="text-left px-4 py-2 font-medium">Model</th>
                <th className="text-left px-4 py-2 font-medium">RAG</th>
                <th className="text-right px-4 py-2 font-medium">Chars</th>
                <th className="text-right px-4 py-2 font-medium" title="Output-side PII: hits / redacted">PII</th>
                <th className="text-left px-4 py-2 font-medium">Prompt</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id} className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
                  <td className="px-4 py-2 text-gray-400 whitespace-nowrap">{fmtTs(e.timestamp)}</td>
                  <td className="px-4 py-2 text-white">{e.username}</td>
                  <td className="px-4 py-2 text-gray-500 font-mono">{e.session_id.slice(0, 8)}…</td>
                  <td className="px-4 py-2 text-gray-400">{e.model || '-'}</td>
                  <td className="px-4 py-2">
                    {e.use_rag
                      ? <span className="text-blue-400">Yes</span>
                      : <span className="text-gray-600">No</span>}
                  </td>
                  <td className="px-4 py-2 text-right text-gray-500">{e.response_length.toLocaleString()}</td>
                  <td className="px-4 py-2 text-right whitespace-nowrap" title={e.pii_out_types || (e.pii_out_hits === null ? 'output filter off' : 'nothing detected')}>
                    {e.pii_out_hits === null
                      ? <span className="text-gray-600">-</span>
                      : e.pii_out_hits === 0
                        ? <span className="text-gray-500">0</span>
                        : <span className={e.pii_out_redacted ? 'text-amber-400' : 'text-gray-300'}>{e.pii_out_hits} / {e.pii_out_redacted}</span>}
                  </td>
                  <td className="px-4 py-2 text-gray-300 max-w-xs truncate" title={e.prompt_preview}>
                    {e.prompt_preview.length > 80 ? e.prompt_preview.slice(0, 80) + '…' : e.prompt_preview}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {pages > 1 && (
        <div className="flex items-center gap-3 justify-end">
          <button
            onClick={() => load(page - 1)} disabled={page <= 1}
            className="text-xs text-gray-400 hover:text-white disabled:opacity-30 px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors"
          >
            ← Prev
          </button>
          <span className="text-xs text-gray-500">Page {page} of {pages}</span>
          <button
            onClick={() => load(page + 1)} disabled={page >= pages}
            className="text-xs text-gray-400 hover:text-white disabled:opacity-30 px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors"
          >
            Next →
          </button>
        </div>
      )}
    </div>
  )
}
