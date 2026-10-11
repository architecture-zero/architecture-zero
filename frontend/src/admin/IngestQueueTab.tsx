import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'
import { PRIMARY_COLOR } from '../theme'

// ── Tab: Ingestion Queue ───────────────────────────────────────────────────

interface IngestJob {
  job_id: string
  status: 'queued' | 'running' | 'complete' | 'failed'
  source: string
  department: string
  chunks_processed: number
  chunks_total: number | null
  error: string | null
  created_at: string
  completed_at: string | null
}

export function IngestQueueTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [jobs, setJobs] = useState<IngestJob[]>([])
  const [enabled, setEnabled] = useState(false)
  const [expandedJob, setExpandedJob] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = () => {
    guardedJson<{ enabled?: boolean; jobs?: IngestJob[] }>(
      fetch(`${api}/api/admin/jobs`, { headers: headers() }), 'Loading ingestion jobs')
      .then(d => {
        if (d) {
          setEnabled(d.enabled ?? false)
          setJobs(d.jobs ?? [])
        }
        setLoading(false)
      })
  }

  useEffect(() => {
    load()
    // Auto-refresh while any job is queued or running
    const id = setInterval(() => {
      setJobs(prev => {
        const active = prev.some(j => j.status === 'queued' || j.status === 'running')
        if (active) load()
        return prev
      })
    }, 3000)
    return () => clearInterval(id)
  }, [])

  const statusBadge = (status: IngestJob['status']) => {
    const styles: Record<string, string> = {
      queued:   'bg-gray-700 text-gray-300',
      running:  'bg-blue-900/60 text-blue-300',
      complete: 'bg-green-900/60 text-green-300',
      failed:   'bg-red-900/60 text-red-400',
    }
    return (
      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${styles[status] ?? 'bg-gray-700 text-gray-400'}`}>
        {status === 'running' ? '⟳ running' : status}
      </span>
    )
  }

  const pct = (j: IngestJob) => {
    if (!j.chunks_total || j.chunks_total === 0) return 0
    return Math.round((j.chunks_processed / j.chunks_total) * 100)
  }

  const fmt = (iso: string | null) => {
    if (!iso) return '-'
    try { return new Date(iso).toLocaleString() } catch { return iso }
  }

  if (loading) return <p className="text-sm text-gray-400">Loading…</p>

  return (
    <div className="space-y-4">
      {!enabled && (
        <div className="bg-yellow-900/30 border border-yellow-700/50 rounded-xl p-4">
          <p className="text-sm text-yellow-300 font-medium">Async jobs disabled</p>
          <p className="text-xs text-yellow-500 mt-1">
            Set <span className="font-mono">ENABLE_ASYNC_JOBS=true</span> and restart the backend to
            enable background ingestion. Uploads currently run synchronously - the request stays open
            until the document is indexed. There is no worker service to start: ingestion runs on a
            thread inside the backend.
          </p>
        </div>
      )}

      <div className="bg-gray-800/50 border border-gray-700 rounded-xl overflow-hidden">
        <div className="p-4 border-b border-gray-700 flex items-center justify-between">
          <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Recent Jobs</p>
          <button
            onClick={load}
            className="text-xs text-gray-500 hover:text-white transition-colors"
          >
            Refresh
          </button>
        </div>

        {jobs.length === 0 ? (
          <p className="text-sm text-gray-500 p-4">No ingestion jobs yet.</p>
        ) : (
          <div className="divide-y divide-gray-700/50">
            {jobs.map(j => (
              <div key={j.job_id} className="p-4 space-y-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm text-white truncate font-medium">{j.source}</p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      dept: {j.department} · {fmt(j.created_at)}
                    </p>
                  </div>
                  <div className="flex-shrink-0">{statusBadge(j.status)}</div>
                </div>

                {/* Progress bar (running/complete) */}
                {(j.status === 'running' || j.status === 'complete') && j.chunks_total !== null && (
                  <div className="space-y-1">
                    <div className="w-full bg-gray-700 rounded-full h-1.5">
                      <div
                        className="h-1.5 rounded-full transition-all"
                        style={{
                          width: `${pct(j)}%`,
                          backgroundColor: j.status === 'complete' ? '#22c55e' : PRIMARY_COLOR,
                        }}
                      />
                    </div>
                    <p className="text-xs text-gray-500">
                      {j.chunks_processed} / {j.chunks_total} chunks ({pct(j)}%)
                    </p>
                  </div>
                )}

                {/* Error detail */}
                {j.status === 'failed' && j.error && (
                  <div>
                    <button
                      onClick={() => setExpandedJob(expandedJob === j.job_id ? null : j.job_id)}
                      className="text-xs text-red-400 hover:text-red-300"
                    >
                      {expandedJob === j.job_id ? '▲ hide error' : '▼ show error'}
                    </button>
                    {expandedJob === j.job_id && (
                      <pre className="mt-2 text-xs text-red-300 bg-red-900/20 border border-red-800/40 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">
                        {j.error}
                      </pre>
                    )}
                  </div>
                )}

                {j.completed_at && (
                  <p className="text-xs text-gray-600">Completed: {fmt(j.completed_at)}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="bg-gray-800/30 border border-gray-700/50 rounded-xl p-4 space-y-2">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Setup</p>
        <p className="text-xs text-gray-500">
          Set one variable and restart the backend. There is no worker container
          and no broker to run:
        </p>
        <pre className="text-xs text-gray-400 bg-gray-800 rounded-lg p-3 overflow-x-auto whitespace-pre">{`# .env
ENABLE_ASYNC_JOBS=true

docker compose up -d backend`}</pre>
        <p className="text-xs text-gray-500">
          Queued ingest runs on a worker <span className="text-gray-400">thread inside the backend</span>,
          not a separate process. That is deliberate: the vector store is an
          embedded database on a local directory, so a second process would be a
          second writer against one index with no cross-process locking - the
          vector-loss failure this platform already had to repair once. The
          trade is honest: this scales to one box.
        </p>
      </div>
    </div>
  )
}
