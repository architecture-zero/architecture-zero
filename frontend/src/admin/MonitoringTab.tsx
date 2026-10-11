import { useState, useEffect } from 'react'
import { SCRAPE_CONFIG_YAML } from '../scrapeConfig'

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

export function MonitoringTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
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
