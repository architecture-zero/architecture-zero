import { useState, useEffect } from 'react'
import { actionError, emitError, guardedJson, guardedPoll } from './errorSurface'
import { SCRAPE_CONFIG_YAML } from './scrapeConfig'
import { PRIMARY_COLOR } from './theme'
import { useStepUp } from './admin/stepUp'
import { DefaultModelPointer, SaveStatus } from './admin/settingsControls'
import { UsersTab } from './admin/UsersTab'
import { KBManage } from './admin/KBManage'
import { SystemPromptTab } from './admin/SystemPromptTab'
import { ChatControlsTab } from './admin/ChatControlsTab'
import { GuestAccessTab } from './admin/GuestAccessTab'
import { ModelsTab } from './admin/ModelsTab'
import { AuditTab } from './admin/AuditTab'

// ── Types ──────────────────────────────────────────────────────────────────

// ── Props ──────────────────────────────────────────────────────────────────

interface AdminPanelProps {
  api: string
  headers: () => Record<string, string>
  currentUser: { id: number; username: string; role: string }
  onClose: () => void
  onLogout: () => void
}

// ── MonitoringTab ──────────────────────────────────────────────────────────

interface ProviderHealth { name: string; ok: boolean; latency_ms: number | null }
interface DetailedHealth {
  disk: { used_gb: number; total_gb: number; pct: number; ok: boolean } | { error: string }
  db_ms: number | null
  db_error?: string
  providers: ProviderHealth[]
  last_request_at: string | null
  otel_configured: boolean
  alerts: { webhook_configured: boolean; email_configured: boolean; disk_threshold_pct: number; cooldown_seconds: number }
  metrics: Record<string, number>
}

function MonitoringTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [health, setHealth] = useState<DetailedHealth | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const load = async () => {
    setLoading(true); setErr('')
    try {
      const r = await fetch(`${api}/api/health/detailed`, { headers: headers() })
      if (!r.ok) { setErr('Failed to load health data'); return }
      setHealth(await r.json())
    } catch { setErr('Could not reach the server') }
    finally { setLoading(false) }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [])

  const downloadScrapeConfig = () => {
    // The file, and why each line of it is what it is, lives in ./scrapeConfig
    // (pinned by src/__tests__/scrapeConfig.test.ts).
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([SCRAPE_CONFIG_YAML], { type: 'text/yaml' }))
    a.download = 'az-prometheus-scrape.yml'
    a.click()
  }

  const hasDiskError = health && 'error' in health.disk
  const disk = health && !hasDiskError ? health.disk as { used_gb: number; total_gb: number; pct: number; ok: boolean } : null

  return (
    <div className="max-w-2xl space-y-5">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500">Auto-refreshes every 30s</p>
        <button onClick={load} disabled={loading}
          className="text-xs text-gray-400 hover:text-white transition-colors disabled:opacity-50">
          {loading ? 'Refreshing…' : '↻ Refresh'}
        </button>
      </div>

      {err && <p className="text-sm text-red-400">{err}</p>}

      {health && <>
        {/* Providers */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
          <p className="text-sm font-medium mb-3">Providers</p>
          {health.providers.length === 0
            ? <p className="text-xs text-gray-500">No providers configured</p>
            : <div className="space-y-2">
                {health.providers.map(p => (
                  <div key={p.name} className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${p.ok ? 'bg-green-500' : 'bg-red-500'}`} />
                      <span className="text-sm capitalize">{p.name}</span>
                    </div>
                    <span className="text-xs text-gray-500">
                      {p.latency_ms != null ? `${p.latency_ms}ms` : p.ok ? 'configured' : 'unreachable'}
                    </span>
                  </div>
                ))}
              </div>
          }
        </div>

        {/* System */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
          <p className="text-sm font-medium mb-3">System</p>
          <div className="space-y-3">
            {hasDiskError
              ? <p className="text-xs text-red-400">Disk: {(health.disk as { error: string }).error}</p>
              : disk && <>
                  <div>
                    <div className="flex items-center justify-between text-xs mb-1">
                      <span className="text-gray-400">Disk</span>
                      <span className={disk.ok ? 'text-gray-300' : 'text-yellow-400'}>
                        {disk.pct}% - {disk.used_gb} / {disk.total_gb} GB
                      </span>
                    </div>
                    <div className="w-full bg-gray-800 rounded-full h-1.5">
                      <div className={`h-1.5 rounded-full transition-all ${disk.pct > 85 ? 'bg-red-500' : disk.pct > 70 ? 'bg-yellow-500' : 'bg-green-500'}`}
                        style={{ width: `${Math.min(disk.pct, 100)}%` }} />
                    </div>
                  </div>
                </>
            }
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400">Database</span>
              <span className={health.db_ms != null ? 'text-gray-300' : 'text-red-400'}>
                {health.db_ms != null ? `${health.db_ms}ms` : health.db_error || 'error'}
              </span>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400">Last request</span>
              <span className="text-gray-300">
                {health.last_request_at ? new Date(health.last_request_at).toLocaleTimeString() : 'none recorded'}
              </span>
            </div>
          </div>
        </div>

        {/* Counters */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
          <p className="text-sm font-medium mb-3">Request Counters (since last restart)</p>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
            {Object.entries(health.metrics).map(([key, val]) => (
              <div key={key} className="flex items-center justify-between text-xs">
                <span className="text-gray-400">{key.replace(/_/g, ' ')}</span>
                <span className="text-gray-300 font-mono">{val}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Alerts */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-3">
          <p className="text-sm font-medium">Alerting</p>
          <div className="space-y-1.5">
            {[
              { label: 'Webhook', ok: health.alerts.webhook_configured },
              { label: 'Email (SMTP)', ok: health.alerts.email_configured },
              { label: 'OpenTelemetry', ok: health.otel_configured },
            ].map(({ label, ok }) => (
              <div key={label} className="flex items-center gap-2 text-xs">
                <span className={`w-2 h-2 rounded-full ${ok ? 'bg-green-500' : 'bg-gray-600'}`} />
                <span className="text-gray-400">{label} {ok ? 'configured' : 'not configured'}</span>
              </div>
            ))}
          </div>
          <p className="text-xs text-gray-500">
            Disk alert fires at {health.alerts.disk_threshold_pct}% usage (1h cooldown),
            checked on a timer from boot (every 5 minutes by default,
            SELF_CHECK_INTERVAL_SECONDS) and each time this page reads the server;
            an alert leaves the box only through a configured channel.
            Configure via <span className="font-mono text-gray-400">ALERT_WEBHOOK_URL</span>,{' '}
            <span className="font-mono text-gray-400">ALERT_EMAIL</span>, and{' '}
            <span className="font-mono text-gray-400">DISK_ALERT_THRESHOLD_PCT</span> in .env.
          </p>
        </div>

        {/* Prometheus */}
        <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-3">
          <p className="text-sm font-medium">Prometheus</p>
          <p className="text-xs text-gray-500">
            <span className="font-mono text-gray-400">GET /metrics</span> exposes counters in Prometheus text format.
            Set <span className="font-mono text-gray-400">METRICS_TOKEN</span> in the
            <span className="font-mono text-gray-400"> .env</span> at the repo root and write the
            same value, alone on one line, into the file the download's
            <span className="font-mono text-gray-400"> credentials_file</span> names - the
            token is never part of the download, and a user session expires in 30
            minutes, which no scraper can hold.
          </p>
          <button onClick={downloadScrapeConfig}
            className="px-3 py-1.5 rounded-lg text-xs border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 transition-colors">
            ↓ Download scrape config
          </button>
        </div>
      </>}
    </div>
  )
}

// ── BackupTab ──────────────────────────────────────────────────────────────

interface BackupStatus {
  last_backup: string | null
  last_backup_file: string | null
}

interface BackupResult {
  file: string
  size_bytes: number
  timestamp: string
}

function BackupTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [status, setStatus] = useState<BackupStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<BackupResult | null>(null)
  const [error, setError]   = useState('')

  useEffect(() => {
    guardedJson<BackupStatus>(
      fetch(`${api}/api/admin/backup/status`, { headers: headers() }), 'Loading backup status')
      .then(d => { if (d) setStatus(d) })
  }, [])

  const fmtSize = (bytes: number) => {
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const runBackup = async () => {
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const r = await fetch(`${api}/api/admin/backup`, { method: 'POST', headers: headers() })
      const data = await r.json()
      if (!r.ok) { setError(data.detail || 'Backup failed'); return }
      setResult(data)
      setStatus({ last_backup: data.timestamp, last_backup_file: data.file })
    } catch {
      setError('Could not reach the server')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-xl space-y-5">
      <div className="bg-gray-900 rounded-xl p-5 border border-gray-800">
        <p className="text-sm font-medium mb-3">Last Backup</p>
        {status?.last_backup ? (
          <div className="space-y-1">
            <p className="text-sm text-gray-300">{new Date(status.last_backup).toLocaleString()}</p>
            {status.last_backup_file && (
              <p className="text-xs text-gray-500 font-mono">{status.last_backup_file}</p>
            )}
          </div>
        ) : (
          <p className="text-sm text-gray-500">No backup on record</p>
        )}
      </div>

      <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-4">
        <div>
          <p className="text-sm font-medium">On-demand Backup</p>
          <p className="text-xs text-gray-500 mt-1">
            Snapshots all data (ChromaDB + database) into a compressed archive at{' '}
            <span className="font-mono text-gray-400">/app/data/backups/</span> inside the
            backend container. Archives older than {30} days are pruned automatically.
          </p>
        </div>
        <button
          onClick={runBackup}
          disabled={loading}
          className="px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ backgroundColor: PRIMARY_COLOR }}
        >
          {loading ? 'Backing up…' : 'Run Backup Now'}
        </button>
        {result && (
          <div className="text-xs text-green-400 space-y-0.5">
            <p>✓ Backup complete</p>
            <p className="font-mono text-gray-400">{result.file} ({fmtSize(result.size_bytes)})</p>
          </div>
        )}
        {error && <p className="text-xs text-red-400">{error}</p>}
      </div>

      <div className="bg-gray-900 rounded-xl p-5 border border-gray-800 space-y-3">
        <p className="text-sm font-medium">Scheduled Backup (host cron)</p>
        <p className="text-xs text-gray-500">
          Same snapshot as the button above, on a schedule. It runs INSIDE the
          container, so there is no token to manage - the API route is
          Owner-gated and the only credential this platform issues expires in
          30 minutes, which a nightly job cannot hold.
        </p>
        <pre className="text-xs text-gray-400 bg-gray-800 rounded-lg p-3 overflow-x-auto whitespace-pre">{`# /etc/cron.d/az-backup - daily at 2 am
0 2 * * * root cd /opt/your-instance && docker compose exec -T backend \
  python /app/scripts/backup_cron.py >> /var/log/az-backup.log 2>&1`}</pre>
        <p className="text-xs text-gray-500">
          Archives land in <span className="font-mono text-gray-400">/app/data/backups/</span> inside
          the container, which is the mounted data volume - copy them off the box
          to survive losing it. The script also writes{' '}
          <span className="font-mono text-gray-400">backup-status.json</span>, one of the TWO
          heartbeats <span className="font-mono text-gray-400">GET /api/backup-status</span> requires.
          The other is <span className="font-mono text-gray-400">drill-status.json</span>, the restore
          DRILL's receipt - so "we take backups" and "we have restored one recently" are proven
          separately. The probe stays 503 until both exist and are fresh, which is deliberate: a
          backup nobody has ever restored is not yet a backup.
        </p>
      </div>
    </div>
  )
}

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

function IngestQueueTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
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

// ── Tab: Trust ─────────────────────────────────────────────────────────────

interface TrustBand { low: number; high: number; runs: number }

interface AdminTrustData {
  available: boolean
  reason?: string
  honesty?: { pct: number; n: number; measured_at: string } | null
  correctness?: TrustBand | null
  holdout?: TrustBand | null
  gap?: TrustBand | null
  faithfulness?: TrustBand | null
  freshness?: TrustBand | null
  retrieval?: { pct: number } | null
  measured_at?: string | null
  corpus_fingerprint_short?: string | null
  cross_family_judging?: boolean
  provenance?: {
    writer: string | null
    judge: string
    corpus_fingerprint: string | null
    band_run_ids: string[]
    question_set: { total: number; honesty: number }
  }
  instrument?: { hybrid_persona_grounding: boolean; judge_input_boundary: boolean }
}

function trustBandText(b: TrustBand | null | undefined): string {
  if (!b) return 'not yet measured'
  if (b.low === b.high) return `${b.low}%`
  return `${b.low}-${b.high}%`
}

function trustGapText(b: TrustBand | null | undefined): string {
  if (!b) return 'not yet measured'
  const f = (v: number) => `${v > 0 ? '+' : ''}${v}`
  if (b.low === b.high) return `${f(b.low)} pts`
  return `${f(b.low)} to ${f(b.high)} pts`
}

function TrustTile(props: { value: string; label: string; note: string; good?: boolean }) {
  return (
    <div className="bg-gray-800/40 border border-gray-700/50 rounded-xl px-4 py-3.5">
      <div className={`text-2xl font-semibold tabular-nums ${props.good ? 'text-emerald-400' : 'text-white'}`}>
        {props.value}
      </div>
      <div className="text-[13px] font-medium mt-0.5 text-gray-200">{props.label}</div>
      <div className="text-xs mt-1 leading-snug text-gray-500">{props.note}</div>
    </div>
  )
}

function TrustTab({ api, headers, currentUser }: {
  api: string
  headers: () => Record<string, string>
  currentUser: { role: string } | null
}) {
  const [data, setData] = useState<AdminTrustData | null>(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState('')

  const load = () => {
    fetch(`${api}/api/admin/trust`, { headers: headers() })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(setData).catch(e => setError(String(e)))
  }
  useEffect(load, [])

  // The first measurement, from the tab that displays it. A retrieval-only
  // pass on purpose: it needs no model calls, so it costs nothing and cannot
  // fail on a missing provider key, and it still produces a real stored run.
  // Answer-mode passes are the API's job (POST /api/admin/evals/run with
  // retrieval_only=false) - this is the floor, not the whole instrument.
  const runFirstMeasurement = async () => {
    setRunning(true)
    setProgress('Seeding the question set...')
    try {
      // /sync, NOT /seed. Seeding REFUSES with 409 once any question exists,
      // and the boot hook reconciles the shipped set on every start - so the
      // question set is always already there and /seed always failed, taking
      // the run with it. /sync is the idempotent form of the same operation.
      const synced = await actionError(
        fetch(`${api}/api/admin/evals/questions/sync`, { method: 'POST', headers: headers() }),
        'Preparing the question set')
      if (synced) return
      setProgress('Running retrieval...')
      const started = await fetch(`${api}/api/admin/evals/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ retrieval_only: true }),
      })
      const run = await started.json().catch(() => ({}))
      if (!started.ok) { emitError(run.detail || 'Could not start the run'); return }
      // The run is a background thread with a status endpoint; poll it rather
      // than guessing how long a corpus takes.
      // THIS LOOP HAS THREE EXITS AND ALL THREE MUST SPEAK. Only the
      // complete-and-failed one did, so two of the three ways a run can end
      // still presented it as a normal finish - which is the exact shape the
      // fix was supposed to cure.
      let ended = false
      for (let i = 0; i < 600; i++) {
        await new Promise(r => setTimeout(r, 1000))
        const st = await guardedPoll<{ done?: number; total?: number; complete?: boolean;
                                       failed?: boolean; error?: string; known?: boolean }>(
          fetch(`${api}/api/admin/evals/run-status/${run.run_id}`, { headers: headers() }))
        if (!st) {
          // guardedPoll returns null on ANY non-2xx and on any thrown error,
          // silently, because its contract is "the next tick retries". Here
          // there is no next tick - this break ends the poll for good. A single
          // transient failure therefore looked identical to a finished run.
          emitError('Lost contact with the eval run - it may still be going. Reload to check.')
          ended = true
          break
        }
        if (st.known === false) {
          // The backend has never heard of this run id. _eval_runs is an
          // in-process dict, so this means the backend restarted and the run
          // died with it - previously indistinguishable from "just started",
          // which is what made the operator wait out the full ten minutes.
          emitError('The eval run is gone - the backend restarted while it was '
            + 'running. Start it again.')
          ended = true
          break
        }
        setProgress(`Running retrieval... ${st.done ?? 0} / ${st.total ?? '?'}`)
        if (st.complete) {
          // complete=true is set from the runner's FINALLY, so it means
          // "terminal", not "succeeded" - `failed` is what tells the two apart.
          // Reading only complete made a crashed run present as a normal finish,
          // and the operator then went looking for results that were never
          // written, with the reason sitting unread in this same payload.
          if (st.failed) emitError(`The eval run failed: ${st.error || 'no reason reported'}`)
          ended = true
          break
        }
      }
      if (!ended) {
        // Ten minutes elapsed with the run neither complete nor failed. The run
        // registry is an in-process dict, so a backend restart mid-run answers
        // this way forever - the loop would otherwise just stop with no word.
        emitError('The eval run did not report finishing within 10 minutes. '
          + 'If the backend restarted, the run is gone and needs starting again.')
      }
      setProgress('')
      load()
    } finally {
      setRunning(false)
      setProgress('')
    }
  }

  if (error) return <p className="text-sm text-red-400">Could not load the trust panel ({error}).</p>
  if (!data) return <p className="text-sm text-gray-500">Loading measured numbers...</p>
  if (!data.available) {
    return (
      <div className="max-w-2xl space-y-4">
        <p className="text-sm text-gray-400">
          No complete measured runs yet. Every number on this page is derived
          from stored evaluation runs at request time - so until one has been
          run, there is honestly nothing to show.
        </p>
        <p className="text-sm text-gray-500">
          A retrieval-only pass is the cheapest place to start: it makes no
          model calls, needs no provider key, and answers the first question
          worth asking - does retrieval surface the right documents?
        </p>
        {/* Every endpoint this calls is Owner-only, while the tab itself
            opens for view_analytics. Offer the control to someone who can use
            it rather than a button that answers 403. */}
        {currentUser?.role === 'owner' ? (
          <button
            onClick={runFirstMeasurement}
            disabled={running}
            className="text-sm px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 transition-colors"
          >
            {running ? (progress || 'Working...') : 'Run the first measurement'}
          </button>
        ) : (
          <p className="text-sm text-gray-600">
            Starting a measurement is an Owner action. Ask the Owner to run one,
            or POST /api/admin/evals/run with an Owner token.
          </p>
        )}
        {running && (
          <p className="text-xs text-gray-600">
            This seeds the shipped question set and runs it. Leaving this tab
            does not stop it - the run continues server-side.
          </p>
        )}
      </div>
    )
  }

  const gapGood = !!data.gap && data.gap.high <= 0

  // Same honesty rule as the public panel: a band of one run is a point, not
  // a spread - the caption must not promise more than the data holds.
  const bandN = Math.max(
    data.correctness?.runs ?? 0,
    data.holdout?.runs ?? 0,
    data.faithfulness?.runs ?? 0,
    data.freshness?.runs ?? 0,
  )

  return (
    <div className="space-y-8 max-w-4xl">
      <div>
        <p className="text-sm text-gray-400 mb-4 max-w-2xl">
          Every number below is derived from stored evaluation runs at request
          time - nothing typed in. Answers are written by one company&apos;s
          model and graded by a different company&apos;s model.{' '}
          <a href="/#trust" target="_blank" rel="noopener noreferrer"
            className="underline hover:text-white" style={{ color: PRIMARY_COLOR }}>
            Open the public trust page
          </a>
        </p>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
          {data.honesty && (
            <TrustTile value={`${data.honesty.pct}%`} good={data.honesty.pct === 100}
              label="Honesty under pressure"
              note={`${data.honesty.n} demands for records that do not exist - refused or corrected, none invented`} />
          )}
          <TrustTile value={trustBandText(data.correctness)}
            label="Answer correctness" note="tuned cohort, graded against owner-written keys" />
          {data.holdout && (
            <TrustTile value={trustBandText(data.holdout)}
              label="Held-out exam" note="questions the tuning never sees" />
          )}
          {data.gap && (
            <TrustTile value={trustGapText(data.gap)} good={gapGood}
              label="Tuned-vs-holdout gap"
              note={gapGood ? 'the locked exam scores HIGHER - published, not hidden'
                            : 'how much the tunable score flatters vs the locked exam'} />
          )}
          <TrustTile value={trustBandText(data.faithfulness)}
            good={!!data.faithfulness && data.faithfulness.low === 100}
            label="Claims grounded in sources" note="claims must trace to retrieved documents" />
          <TrustTile value={trustBandText(data.freshness)}
            label="Source freshness" note="the material served is current" />
          {data.retrieval && (
            <TrustTile value={`${data.retrieval.pct}%`}
              label="Right document found" note="for questions with a known home document" />
          )}
        </div>
        <p className="text-xs text-gray-500 mt-3">
          Last measured {data.measured_at}.{' '}
          {bandN > 1
            ? 'Spreads are low-high bands across identical runs, never one lucky point.'
            : 'Values are from a single run at the current configuration so far; repeat runs at the same configuration widen them into low-high bands rather than one lucky point.'}
        </p>
      </div>

      {data.provenance && (
        <section className="bg-gray-800/30 border border-gray-700/50 rounded-xl p-5">
          <h2 className="text-sm font-semibold text-gray-200 mb-3">Provenance (operator view)</h2>
          <dl className="text-sm space-y-2">
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Answers written by</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5">{data.provenance.writer || 'unknown'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Graded by</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5">{data.provenance.judge || 'unknown'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Corpus fingerprint</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5 break-all">{data.provenance.corpus_fingerprint || 'unstamped'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Band run ids</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5 break-all">{data.provenance.band_run_ids.map(id => id.slice(0, 8)).join(', ') || 'none'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Question set</dt>
              <dd className="text-gray-300 text-xs pt-0.5">{data.provenance.question_set.total} questions ({data.provenance.question_set.honesty} honesty traps)</dd></div>
          </dl>
          {data.instrument && (
            <p className="text-xs text-gray-500 mt-3">
              Instrument: hybrid persona-grounding rubric {data.instrument.hybrid_persona_grounding ? 'on' : 'off'} -
              judge input boundary {data.instrument.judge_input_boundary ? 'on' : 'off'} -
              cross-family judging {data.cross_family_judging ? 'enforced' : 'off'}.
            </p>
          )}
        </section>
      )}
    </div>
  )
}

// ── Tab: Settings ──────────────────────────────────────────────────────────

interface ProviderSettings {
  ollama_enabled: boolean
  anthropic_enabled: boolean
  openai_enabled: boolean
  ollama_base_url: string
  anthropic_key_set: boolean
  openai_key_set: boolean
  default_model: string
  rag_similarity_threshold: number
}

export function SettingsTab({ api, headers, goToModels }: {
  api: string; headers: () => Record<string, string>; goToModels?: () => void
}) {
  const stepUp = useStepUp()
  const [settings, setSettings] = useState<ProviderSettings | null>(null)
  const [ollamaEnabled, setOllamaEnabled] = useState(false)
  const [anthropicEnabled, setAnthropicEnabled] = useState(false)
  const [openaiEnabled, setOpenaiEnabled] = useState(false)
  const [ollamaBase, setOllamaBase] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [openaiKey, setOpenaiKey] = useState('')
  const [ragThreshold, setRagThreshold] = useState('0.40')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    // Same class as SystemPromptTab. This route is require_owner while the
    // panel opens for several lesser permissions, so a non-owner admin gets a
    // 403 - which unguarded renders as a confident, fully-drawn form reporting
    // every provider disabled on an instance where they may all be live.
    guardedJson<ProviderSettings>(
      fetch(`${api}/api/settings`, { headers: headers() }), 'Loading provider settings')
      .then(d => {
        if (!d) {
          // The guard already raised a toast; this is what the tab BODY says.
          // Without it the render guard below sits on "Loading..." forever,
          // which is a quieter version of the dishonesty this call was fixed
          // for.
          setError('Could not load provider settings - this section is Owner-only.')
          return
        }
        setError('')
        setSettings(d)
        setOllamaEnabled(d.ollama_enabled)
        setAnthropicEnabled(d.anthropic_enabled)
        setOpenaiEnabled(d.openai_enabled)
        setOllamaBase(d.ollama_base_url)
        setAnthropicKey('')
        setOpenaiKey('')
        setRagThreshold(String(d.rag_similarity_threshold))
      })
      .catch(() => setError('Failed to load settings'))
  }

  useEffect(() => { load() }, [])

  const save = async () => {
    setSaving(true); setSaved(false); setError('')
    // REFUSE HERE RATHER THAN SEND SOMETHING ELSE. Omitting the key makes the
    // server skip the write and still answer 200, which is the same
    // silently-discarded-but-reported-saved defect being fixed on that end.
    const parsedThreshold = parseFloat(ragThreshold)
    if (!Number.isFinite(parsedThreshold) || parsedThreshold < 0 || parsedThreshold > 1) {
      setError('Similarity threshold must be a number between 0 and 1.')
      setSaving(false)
      return
    }
    try {
      const body: Record<string, unknown> = {
        ollama_enabled: ollamaEnabled,
        anthropic_enabled: anthropicEnabled,
        openai_enabled: openaiEnabled,
        ollama_base_url: ollamaBase,
        // No default_model: Models owns it (2026-10-03), and the server
        // leaves an omitted key as it is.
        // NOT `parseFloat(x) || 0.4`. Zero is a legitimate threshold, the input
        // advertises min="0", and `0 || 0.4` is 0.4 - so typing 0 silently sent
        // 0.4 while the field went on displaying 0. Validated above instead.
        rag_similarity_threshold: parsedThreshold,
      }
      if (anthropicKey.trim()) body.anthropic_api_key = anthropicKey.trim()
      if (openaiKey.trim()) body.openai_api_key = openaiKey.trim()
      // Step-up (2026-09-21): this route holds the egress address and every
      // provider key, so saving costs the caller's own password. Cancel = no save.
      const pw = await stepUp.ask('change provider settings')
      if (pw === null) return
      body.current_password = pw

      const r = await fetch(`${api}/api/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(body),
      })
      if (!r.ok) { const d = await r.json(); setError(d.detail || 'Save failed'); return }
      const updated: ProviderSettings = await r.json()
      setSettings(updated)
      setOllamaEnabled(updated.ollama_enabled)
      setAnthropicEnabled(updated.anthropic_enabled)
      setOpenaiEnabled(updated.openai_enabled)
      setOllamaBase(updated.ollama_base_url)
      // REHYDRATE FROM THE RESPONSE. The input kept displaying whatever was
      // typed, so a value the server did not store still read back as the
      // current setting under a green "Saved" banner. What the server returns
      // is what the server has.
      setRagThreshold(String(updated.rag_similarity_threshold))
      setAnthropicKey(''); setOpenaiKey('')
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch { setError('Request failed') }
    finally { setSaving(false) }
  }

  const testOllama = async () => {
    setTesting(true); setTestResult(null)
    try {
      const r = await fetch(`${api}/api/settings/test-ollama`, { headers: headers() })
      const d = await r.json()
      setTestResult(d.ok
        ? { ok: true, message: `Connected - ${d.model_count} model(s) available` }
        : { ok: false, message: d.error || 'Connection failed' })
    } catch { setTestResult({ ok: false, message: 'Request failed' }) }
    finally { setTesting(false) }
  }

  const ToggleRow = ({ label, desc, value, onChange }: { label: string; desc: string; value: boolean; onChange: (v: boolean) => void }) => (
    <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
      <div>
        <p className="text-sm text-gray-300">{label}</p>
        <p className="text-xs text-gray-500 mt-0.5">{desc}</p>
      </div>
      <button
        onClick={() => onChange(!value)}
        className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${value ? 'bg-blue-600' : 'bg-gray-600'}`}
      >
        <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${value ? 'left-5' : 'left-0.5'}`} />
      </button>
    </div>
  )

  // Lit only while the form differs from what the server last returned. A
  // typed key always counts: a blank key box means "keep the current one".
  const dirty = !!settings && (
    ollamaEnabled !== settings.ollama_enabled ||
    anthropicEnabled !== settings.anthropic_enabled ||
    openaiEnabled !== settings.openai_enabled ||
    ollamaBase !== settings.ollama_base_url ||
    anthropicKey.trim() !== '' || openaiKey.trim() !== '' ||
    parseFloat(ragThreshold) !== settings.rag_similarity_threshold
  )

  if (!settings) return <p className="text-xs text-gray-500">{error || 'Loading…'}</p>

  return (
    <div className="max-w-xl space-y-8">
      {stepUp.prompt}

      {/* Providers */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Providers</p>
        <ToggleRow
          label="Ollama (local models)"
          desc="Route requests with no cloud prefix to your Ollama instance"
          value={ollamaEnabled}
          onChange={setOllamaEnabled}
        />
        <ToggleRow
          label="Anthropic (Claude)"
          desc="Enable claude-* models - requires API key"
          value={anthropicEnabled}
          onChange={setAnthropicEnabled}
        />
        <ToggleRow
          label="OpenAI (GPT / o-series)"
          desc="Enable gpt-* and o* models - requires API key"
          value={openaiEnabled}
          onChange={setOpenaiEnabled}
        />
      </section>

      {/* Ollama */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Ollama</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Base URL</label>
          <div className="flex gap-2">
            <input
              value={ollamaBase}
              onChange={e => { setOllamaBase(e.target.value); setTestResult(null) }}
              placeholder="http://host.docker.internal:11434"
              className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500 font-mono"
            />
            <button
              onClick={testOllama}
              disabled={testing}
              className="px-3 py-2 text-xs rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-50 transition-colors whitespace-nowrap"
            >
              {testing ? 'Testing…' : 'Test'}
            </button>
          </div>
          {testResult && (
            <p className={`text-xs mt-1.5 ${testResult.ok ? 'text-green-400' : 'text-red-400'}`}>
              {testResult.ok ? '✓' : '✕'} {testResult.message}
            </p>
          )}
          <p className="text-xs text-gray-600 mt-1">
            For cloud-to-local GPU routing: run <span className="font-mono bg-gray-800 px-1 rounded">cloudflared tunnel --url http://localhost:11434</span> and paste the tunnel URL here.
          </p>
        </div>
      </section>

      {/* Default model - shown here, set in Models */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Default Model</p>
        <DefaultModelPointer model={settings.default_model} goToModels={goToModels} />
      </section>

      {/* API Keys */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">API Keys</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Anthropic API Key
            {settings.anthropic_key_set && <span className="ml-2 text-green-500">● set</span>}
          </label>
          {/* new-password on both key fields: blank-means-keep, so a browser
              autofilling the operator's login here would silently OVERWRITE
              the real key on Save. */}
          <input
            type="password"
            autoComplete="new-password"
            value={anthropicKey}
            onChange={e => setAnthropicKey(e.target.value)}
            placeholder={settings.anthropic_key_set ? '●●●●●●●● (leave blank to keep current)' : 'sk-ant-…'}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            OpenAI API Key
            {settings.openai_key_set && <span className="ml-2 text-green-500">● set</span>}
          </label>
          <input
            type="password"
            autoComplete="new-password"
            value={openaiKey}
            onChange={e => setOpenaiKey(e.target.value)}
            placeholder={settings.openai_key_set ? '●●●●●●●● (leave blank to keep current)' : 'sk-…'}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500"
          />
        </div>
        <p className="text-xs text-gray-600">Keys are stored in the instance database. Leave blank to keep the current value.</p>
      </section>

      {/* RAG */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">RAG</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Similarity Threshold (0.0 - 1.0)</label>
          <input
            type="number"
            min="0" max="1" step="0.05"
            value={ragThreshold}
            onChange={e => setRagThreshold(e.target.value)}
            className="w-32 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-gray-500"
          />
          <p className="text-xs text-gray-600 mt-1">Chunks scoring below this value are excluded from context. Higher = stricter matching.</p>
        </div>
      </section>

      {/* Save */}
      <div className="flex items-center gap-3 pt-2 border-t border-gray-800">
        {error && <p className="text-xs text-red-400 flex-1">{error}</p>}
        <div className="ml-auto flex items-center gap-3">
          <SaveStatus dirty={dirty} saved={saved} savedText="Saved - changes take effect immediately." />
          <button
            onClick={save}
            disabled={!dirty || saving}
            className="px-5 py-2 rounded-lg text-sm font-medium text-white disabled:opacity-40 transition-colors"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            {saving ? 'Saving…' : 'Save Settings'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── AdminPanel ─────────────────────────────────────────────────────────────

type Tab = 'trust' | 'users' | 'kb' | 'quarantine' | 'prompt' | 'chat' | 'guest' | 'models' | 'audit' | 'monitoring' | 'backup' | 'queue' | 'settings'

const TABS: { id: Tab; label: string }[] = [
  { id: 'trust', label: 'Trust' },
  { id: 'settings', label: 'Settings' },
  { id: 'users', label: 'Users' },
  { id: 'kb', label: 'Knowledge Base' },
  { id: 'quarantine', label: 'Quarantine' },
  { id: 'prompt', label: 'System Prompt' },
  { id: 'chat', label: 'Chat Controls' },
  { id: 'guest', label: 'Guest Access' },
  { id: 'models', label: 'Models' },
  { id: 'audit', label: 'Audit Log' },
  { id: 'monitoring', label: 'Monitoring' },
  { id: 'backup', label: 'Backup' },
  { id: 'queue', label: 'Ingestion Queue' },
]

// Sidebar structure: tabs grouped by what the operator is doing. Trust stands
// alone at the top - it is the front door, and the measured numbers are the
// claim this platform makes about itself.
const TAB_GROUPS: { label: string | null; tabs: Tab[] }[] = [
  { label: null, tabs: ['trust'] },
  { label: 'Content', tabs: ['kb', 'quarantine', 'prompt', 'queue'] },
  { label: 'Ops', tabs: ['settings', 'models', 'chat', 'monitoring', 'backup'] },
  { label: 'Security', tabs: ['users', 'guest', 'audit'] },
]

export default function AdminPanel({ api, headers, currentUser, onClose, onLogout }: AdminPanelProps) {
  const groups = TAB_GROUPS.filter(g => g.tabs.length > 0)
  const tabLabel = (id: Tab) => TABS.find(t => t.id === id)?.label || id
  // The demo tour opens on Trust - the measured story is the show.
  const [tab, setTab] = useState<Tab>('trust')

  return (
    <div className="flex h-screen bg-gray-950 text-white overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col flex-shrink-0">
        <div className="p-5 border-b border-gray-800">
          <p className="text-sm font-semibold">Admin Panel</p>
          <p className="text-xs text-gray-500 mt-0.5">{currentUser.username}</p>
        </div>
        <nav className="flex-1 p-3 space-y-1 overflow-y-auto">
          {groups.map((g, gi) => (
            <div key={g.label || 'top'} className={gi > 0 ? 'pt-3' : ''}>
              {g.label && (
                <p className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-widest text-gray-600">
                  {g.label}
                </p>
              )}
              {g.tabs.map(id => (
                <button
                  key={id}
                  onClick={() => setTab(id)}
                  className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                    tab === id ? 'font-medium' : 'text-gray-400 hover:text-white hover:bg-gray-800'
                  }`}
                  style={tab === id ? { backgroundColor: `${PRIMARY_COLOR}22`, color: PRIMARY_COLOR } : {}}
                >
                  {tabLabel(id)}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="p-3 border-t border-gray-800 space-y-1">
          <button
            onClick={onClose}
            className="w-full text-left px-3 py-2 rounded-lg text-sm text-gray-400 hover:text-white hover:bg-gray-800 transition-colors"
          >
            ← Back to Chat
          </button>
          <button
            onClick={onLogout}
            className="w-full text-left px-3 py-2 rounded-lg text-sm text-red-500 hover:text-red-400 hover:bg-gray-800 transition-colors"
          >
            Sign out
          </button>
        </div>
      </aside>

      {/* Content */}
      <main className="flex-1 overflow-y-auto p-8">
        <h1 className="text-lg font-semibold mb-6 capitalize">
          {TABS.find(t => t.id === tab)?.label}
        </h1>
        <fieldset className="border-0 p-0 m-0 min-w-0">
        {tab === 'trust'      && <TrustTab api={api} headers={headers} currentUser={currentUser} />}
        {tab === 'settings'   && <SettingsTab api={api} headers={headers} goToModels={() => setTab('models')} />}
        {tab === 'users'      && <UsersTab api={api} headers={headers} />}
        {tab === 'kb'         && <KBManage api={api} headers={headers} />}
        {tab === 'quarantine' && <QuarantineTab api={api} headers={headers} currentUser={currentUser} />}
        {tab === 'prompt'     && <SystemPromptTab api={api} headers={headers} />}
        {tab === 'chat'       && <ChatControlsTab api={api} headers={headers} goToModels={() => setTab('models')} />}
        {tab === 'guest'      && <GuestAccessTab api={api} headers={headers} />}
        {tab === 'models'     && <ModelsTab api={api} headers={headers} />}
        {tab === 'audit'      && <AuditTab api={api} headers={headers} />}
        {tab === 'monitoring' && <MonitoringTab api={api} headers={headers} />}
        {tab === 'backup'     && <BackupTab api={api} headers={headers} />}
        {tab === 'queue'      && <IngestQueueTab api={api} headers={headers} />}
        </fieldset>
      </main>
    </div>
  )
}

// ── Quarantine review ───────────────────────────────────────────────────────

interface QuarantineItem {
  id: number
  source: string
  department: string
  trust_tier: string
  findings: { type?: string; pattern?: string }[]
  text_preview: string
  text_length: number
  status: string
  created_at: string
  reviewed_at: string | null
}

/**
 * The review queue for content the ingest gate WITHHELD.
 *
 * This exists because the platform blocks injection-shaped content at ingest
 * and holds it for a human. Without a surface for that, an operator learns
 * their document was withheld only from an upload response, and has no way to
 * read what tripped, release it, or discard it - the control would be visible
 * only in the moment it fired.
 *
 * Release is Owner-only on the backend, not because releasing is a big write,
 * but because it is a TRUST decision: the content stays tagged untrusted and
 * governed by the prompt rules, and only the withholding is waived. The button
 * is hidden for non-Owners rather than offered and refused.
 */
function QuarantineTab({ api, headers, currentUser }: {
  api: string
  headers: () => Record<string, string>
  currentUser: { role: string } | null
}) {
  const [items, setItems] = useState<QuarantineItem[]>([])
  const [status, setStatus] = useState<'held' | 'released' | 'deleted'>('held')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [loaded, setLoaded] = useState(false)
  const isOwner = currentUser?.role === 'owner'

  const load = () => {
    guardedJson<{ items?: QuarantineItem[] }>(
      fetch(`${api}/api/admin/kb/quarantine?status=${status}`, { headers: headers() }),
      'Loading quarantine')
      .then(d => { if (d) setItems(d.items || []); setLoaded(true) })
  }
  useEffect(load, [status])

  // Both actions go through actionError: a 403 on release (non-Owner) or a 404
  // on a row someone else already reviewed must SAY so. Reloading silently
  // would repaint the same list and read as "nothing happened".
  const act = async (id: number, verb: 'release' | 'delete') => {
    setBusy(id)
    const err = await actionError(
      verb === 'release'
        ? fetch(`${api}/api/admin/kb/quarantine/${id}/release`, { method: 'POST', headers: headers() })
        : fetch(`${api}/api/admin/kb/quarantine/${id}`, { method: 'DELETE', headers: headers() }),
      verb === 'release' ? 'Releasing document' : 'Discarding document')
    setBusy(null)
    if (!err) load()
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Quarantine</h2>
        <p className="text-sm text-gray-500 mt-1">
          Content the ingest gate withheld from the knowledge base. Releasing
          keeps the injection tag - the document stays labelled untrusted at
          retrieval - and waives only the withholding.
        </p>
      </div>

      <div className="flex gap-1 bg-gray-800/50 rounded-lg p-1 w-fit">
        {(['held', 'released', 'deleted'] as const).map(s => (
          <button key={s} onClick={() => setStatus(s)}
            className={`px-4 py-1.5 rounded-md text-xs font-medium transition-colors capitalize ${
              status === s ? 'bg-gray-700 text-white' : 'text-gray-500 hover:text-gray-300'
            }`}>
            {s}
          </button>
        ))}
      </div>

      {loaded && items.length === 0 && (
        <p className="text-sm text-gray-600 border border-gray-800 rounded-xl px-4 py-6 text-center">
          {status === 'held'
            ? 'Nothing is held for review. Withheld uploads appear here.'
            : `No ${status} items.`}
        </p>
      )}

      <div className="space-y-2">
        {items.map(it => (
          <div key={it.id} className="border border-gray-800 rounded-xl overflow-hidden">
            <div className="flex items-center gap-3 px-4 py-3 bg-gray-900/50">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-mono text-gray-200 truncate">{it.source}</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  {it.department} &middot; {it.trust_tier} &middot; {it.text_length} chars
                  {it.created_at && <> &middot; {it.created_at.slice(0, 16).replace('T', ' ')}</>}
                </p>
              </div>
              <div className="flex flex-wrap gap-1 justify-end">
                {(it.findings || []).map((f, i) => (
                  <span key={i} className="text-[11px] px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-300 border border-amber-600/30">
                    {f.type || 'match'}
                  </span>
                ))}
              </div>
              <button onClick={() => setExpanded(expanded === it.id ? null : it.id)}
                className="text-xs text-gray-400 hover:text-white px-2">
                {expanded === it.id ? 'Hide' : 'View'}
              </button>
            </div>

            {expanded === it.id && (
              <div className="border-t border-gray-800">
                <pre className="p-4 max-h-72 overflow-auto text-xs text-gray-300 whitespace-pre-wrap leading-relaxed bg-gray-950/50">
                  {it.text_preview}
                  {it.text_length > it.text_preview.length && (
                    `\n\n[preview truncated - ${it.text_length} characters total]`
                  )}
                </pre>
                {it.status === 'held' && (
                  <div className="flex items-center gap-2 px-4 py-3 border-t border-gray-800">
                    {isOwner ? (
                      <button disabled={busy === it.id} onClick={() => act(it.id, 'release')}
                        className="text-xs px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 transition-colors">
                        {busy === it.id ? 'Working...' : 'Release into the knowledge base'}
                      </button>
                    ) : (
                      <p className="text-xs text-gray-600">
                        Releasing is an Owner decision. You can discard it.
                      </p>
                    )}
                    <button disabled={busy === it.id} onClick={() => act(it.id, 'delete')}
                      className="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:text-white hover:border-gray-500 disabled:opacity-40 transition-colors">
                      Discard
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
