import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'
import { PRIMARY_COLOR } from '../theme'

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

export function BackupTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
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
